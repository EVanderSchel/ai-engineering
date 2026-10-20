"""draft_letter: the decision letter, written from the decision and its reasons, never from the narrative.

The reasons come from fixed sources: an automatic decline cites the fund's rules word for word (fund_rules.RULES);
a reviewer's decision gives the reviewer's reason. The model writes the letter around them, and code checks the
result (`letter_problems`): every reason appears verbatim, the decision stated is the real one, and no dollar
amount appears that isn't the request or a figure from a cited rule. The organization's name and project title
are applicant-written, so they go in as unverified_text; the narrative isn't sent at all.

    $env:AWS_PROFILE = "agent-toolkit"; uv run python src/draft_letter.py --model sonnet|haiku   # dev packets
"""

import argparse
import datetime as dt
import json
import re
import sys
from typing import Literal

from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

import bedrock
import fund_rules
import paths
from state import ApplicationForm, ApplicationState, Eligibility, ReviewerDecision

PROMPT_VERSION = "v2"
SYSTEM = (paths.ROOT / "prompts" / "draft_letter" / f"{PROMPT_VERSION}.txt").read_text(encoding="utf-8").strip()
MAX_TOKENS = 800
DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?!\d|,\d)")


class Decision(BaseModel):
    """What the letter must say: the decision, the reasons to quote, and the facts to address it with."""

    outcome: Literal["approved", "declined"]
    reasons: list[str]  # quoted word for word in the letter
    organization_name: str
    project_title: str | None
    amount_requested: int


class LetterAnswer(BaseModel):
    """The decision letter."""

    subject: str = Field(description="The letter's subject line")
    body: str = Field(description="The letter, from greeting to signature")
    outcome_stated: Literal["approved", "declined"] = Field(description="The decision the letter communicates")


def decision_for(state: ApplicationState) -> Decision:
    """The decision to communicate, or ValueError if there isn't one yet (waiting for the reviewer)."""
    common = {
        "organization_name": state.form.organization_name,
        "project_title": state.form.project_title,
        "amount_requested": state.form.amount_requested,
    }
    if state.review is not None and state.review.decision is not None:
        outcome = "approved" if state.review.decision == "approve" else "declined"
        return Decision(outcome=outcome, reasons=[state.review.reason], **common)
    if state.route == "decline_ineligible" and state.eligibility is not None:
        reasons = [fund_rules.RULES[rule] for rule in state.eligibility.failed_rules]
        return Decision(outcome="declined", reasons=reasons, **common)
    raise ValueError("no decision to communicate yet: the application is waiting for its reviewer")


def _amounts(text: str) -> set[int]:
    return {int(m.replace(",", "")) for m in DOLLARS.findall(text)}


def letter_problems(answer: LetterAnswer, decision: Decision) -> list[str]:
    """What the letter says that it shouldn't, or leaves out (empty: it says exactly the decision)."""
    text = f"{answer.subject}\n{answer.body}"
    flat = " ".join(text.split()).casefold()  # a quote may start lower-case to fit the sentence; words can't change
    problems = []
    if answer.outcome_stated != decision.outcome:
        problems.append(f"the letter says {answer.outcome_stated}, the decision is {decision.outcome}")
    problems += [
        f"reason not quoted word for word: {r!r}"
        for r in decision.reasons
        if " ".join(r.split()).casefold() not in flat
    ]
    allowed = {decision.amount_requested} | {a for r in decision.reasons for a in _amounts(r)}
    stray = sorted(_amounts(text) - allowed)
    if stray:
        problems.append(f"dollar amounts that aren't the request or in a reason: {stray}")
    if decision.outcome == "approved" and decision.amount_requested not in _amounts(text):
        problems.append(f"the approval doesn't state the amount, ${decision.amount_requested:,}")
    return problems


def draft_letter(client, state: ApplicationState, model: str = "haiku") -> tuple[str, list[str], bedrock.CallRecord]:
    """Returns (the letter, what's still wrong with it, the call record).

    A draft the code check finds problems with is sent back once, with the problems listed, to be corrected.
    Whatever is still wrong after that is returned, so a person fixes it before the letter goes out."""
    decision = decision_for(state)
    facts = {
        "decision": decision.outcome,
        "reasons": decision.reasons,
        "organization_name": {"unverified_text": decision.organization_name},
        "project_title": {"unverified_text": decision.project_title},
        "amount_requested": f"${decision.amount_requested:,}",
    }
    content = [{"text": json.dumps(facts, indent=2)}, {"text": "Write the decision letter."}]
    answer, record = bedrock.structured(
        client,
        step="draft_letter",
        model=model,
        system=SYSTEM,
        content=content,
        schema=LetterAnswer,
        max_tokens=MAX_TOKENS,
    )
    problems = letter_problems(answer, decision)
    if problems:
        draft = json.dumps(answer.model_dump(), indent=2)
        revise = content + [
            {"text": f"Your draft:\n{draft}\n\nA check found these problems; write the letter again with them fixed:"},
            {"text": "\n".join(f"- {p}" for p in problems)},
        ]
        answer, revision = bedrock.structured(
            client,
            step="draft_letter",
            model=model,
            system=SYSTEM,
            content=revise,
            schema=LetterAnswer,
            max_tokens=MAX_TOKENS,
        )
        record = record.plus(revision)
        problems = letter_problems(answer, decision)
    letter = f"Subject: {answer.subject}\n\n{answer.body}"
    return letter, problems, record


# --- Drafting for the dev packets ------------------------------------------------------------------------

# Reasons a reviewer might give for the planted problems, to draft letters for applications that need a person.
REVIEW_REASONS = {
    "budget_mismatch": "The annual budget stated in the proposal does not match the expenses on your latest Form 990.",
    "staff_mismatch": "The staff count stated in the proposal does not match the employees on your latest Form 990.",
    "name_mismatch": "The organization name on the application does not match the IRS record for the EIN provided.",
    "out_of_focus": "The project does not fund direct services in one of the Fund's focus areas.",
}
APPROVAL_REASON = "The project offers clear, measurable services in one of the Fund's focus areas."


def dev_state(packet_id: str) -> ApplicationState:
    """A dev packet at its decision point: auto-declined by the rules, or decided by a reviewer as its label
    suggests (declined for a planted problem, approved otherwise)."""
    folder = paths.GOLD_DIR / packet_id
    label = json.loads((folder / "label.json").read_text(encoding="utf-8"))
    state = ApplicationState(
        application_id=packet_id,
        received_at=dt.datetime(2025, 6, 1, tzinfo=dt.UTC),
        form=ApplicationForm.model_validate_json((folder / "form.json").read_text(encoding="utf-8")),
        narrative="",
        return_pdf="",
        eligibility=Eligibility(failed_rules=label["failed_rules"]),
        route=label["route"],
    )
    if label["route"] == "review":
        problems = [f for f in label["flags"] if f in REVIEW_REASONS]
        decision = "decline" if problems else "approve"
        reason = REVIEW_REASONS[problems[0]] if problems else APPROVAL_REASON
        now = dt.datetime(2025, 7, 1, tzinfo=dt.UTC)
        state = state.model_copy(
            update={"review": ReviewerDecision(decision=decision, reason=reason, reviewer="dev", decided_at=now)}
        )
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Draft letters for the 15 dev packets and check them.")
    parser.add_argument("--model", choices=sorted(bedrock.MODELS), default="haiku")
    args = parser.parse_args()
    client = bedrock.make_client()
    rows, letters = [], []
    try:
        for folder in sorted(paths.GOLD_DIR.glob("gp-*")):
            if json.loads((folder / "label.json").read_text(encoding="utf-8"))["split"] != "dev":
                continue
            state = dev_state(folder.name)
            letter, problems, record = draft_letter(client, state, args.model)
            outcome = decision_for(state).outcome
            source = "reviewer" if state.review else "rules"
            rows.append(
                {
                    "packet_id": folder.name,
                    "outcome": outcome,
                    "source": source,
                    "problems": problems,
                    "words": len(letter.split()),
                    "cost_usd": record.cost_usd,
                    "attempts": record.attempts,
                }
            )
            letters.append(f"## {folder.name} ({outcome}, by the {source})\n\n{letter}\n")
            print(
                f"{folder.name}: {outcome} by {source}: {'ok' if not problems else '; '.join(problems)} "
                f"(${record.cost_usd:.4f})"
            )
    except ClientError as e:
        print(f"Bedrock error: {e}", file=sys.stderr)
        return 1
    out = paths.ROOT / "results" / f"draft-letter-dev-{args.model}-{PROMPT_VERSION}.json"
    summary = {
        "model": args.model,
        "prompt": PROMPT_VERSION,
        "date": dt.date.today().isoformat(),
        "letters": len(rows),
        "letters_with_problems": sum(bool(r["problems"]) for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "rows": rows,
    }
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text("\n".join(letters), encoding="utf-8")
    print(
        f"\n{args.model}: {summary['letters_with_problems']} of {len(rows)} letters with problems, "
        f"${summary['cost_usd']:.4f}. "
        f"Letters in {out.with_suffix('.md').relative_to(paths.ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
