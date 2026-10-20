"""research: what the public record (ProPublica, from IRS data) says about the EIN on the application.

A hybrid, with a model only where judgment is needed (design decision 5):

1. Lookup (code): the EIN's registered name, which years have figures, and the latest revenue and expenses.
   Always the same calls, so no model decides them.
2. Name check: if the form's name and the registered name are the same after normalizing (case, punctuation,
   "Inc"), they match, with no model call. Otherwise a small structured call (Haiku) judges whether they're
   the same organization ("Hagerstown Goodwill Industries" and its trade name may be).
3. Investigation (agent): only when the names belong to different organizations, a Bedrock tool-use loop
   searches by the form's name to tell the reviewer whose name it is.

    $env:AWS_PROFILE = "agent-toolkit"; uv run python src/research.py    # the 15 dev packets
"""

import json
import re
import sys

from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

import bedrock
import paths
import propublica_tools
from state import ApplicationForm, Research

JUDGE_MODEL = "haiku"
AGENT_MODEL = "sonnet"
SUFFIXES = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LLC", "LTD"}


def normalize_name(name: str) -> str:
    """Upper case, '&' as AND, punctuation dropped, a leading THE and trailing corporate suffixes removed."""
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND ")).split()
    if words[:1] == ["THE"]:
        words = words[1:]
    while words and words[-1] in SUFFIXES:
        words = words[:-1]
    return " ".join(words)


class NameJudgment(BaseModel):
    """Whether two names refer to the same organization."""

    same_organization: bool = Field(description="True if both names refer to the same organization")
    reason: str = Field(description="One sentence a grant reviewer can check")


class NameInvestigation(BaseModel):
    """What you found about the organization name written on the application form."""

    form_name_belongs_to: str | None = Field(
        description="EIN (12-3456789) of a registered organization with the form's name, if you found one; else null"
    )
    explanation: str = Field(description="One or two sentences for the grant reviewer, citing what the tools showed")


PROMPTS = paths.ROOT / "prompts" / "research"
JUDGE_SYSTEM = (PROMPTS / "judge_v1.txt").read_text(encoding="utf-8").strip()
AGENT_SYSTEM = (PROMPTS / "agent_v1.txt").read_text(encoding="utf-8").strip()


def _names(form_name: str, registered_name: str, ein: str) -> list[dict]:
    payload = {
        "name_on_application_form": {"unverified_text": form_name},
        "name_registered_with_irs_for_ein": {"unverified_text": registered_name},
        "ein": propublica_tools.format_ein(ein),
    }
    return [{"text": json.dumps(payload, indent=2)}]


def research(client, form: ApplicationForm) -> tuple[Research, list[bedrock.CallRecord], list[dict]]:
    """Returns (Research, the model calls it made, the agent's tool calls if it ran)."""
    record = propublica_tools.lookup(form.ein)
    if record is None:
        return Research(found=False), [], []
    found = Research(found=True, **record)
    if normalize_name(form.organization_name) == normalize_name(record["registered_name"]):
        return found.model_copy(update={"name_matches_form": True}), [], []

    names = _names(form.organization_name, record["registered_name"], form.ein)
    judgment, judge_call = bedrock.structured(
        client,
        step="research",
        model=JUDGE_MODEL,
        system=JUDGE_SYSTEM,
        content=names,
        schema=NameJudgment,
        max_tokens=300,
    )
    if judgment.same_organization:
        return (
            found.model_copy(update={"name_matches_form": True, "name_explanation": judgment.reason}),
            [judge_call],
            [],
        )

    investigation, agent_calls, trajectory = bedrock.tool_loop(
        client,
        step="research",
        model=AGENT_MODEL,
        system=AGENT_SYSTEM,
        content=names,
        tools=propublica_tools.TOOLS,
        tool_specs=propublica_tools.TOOL_SPECS,
        finish=NameInvestigation,
        max_tokens=1000,
        tool_errors=(propublica_tools.ToolError,),
    )
    update = {
        "name_matches_form": False,
        "name_explanation": investigation.explanation,
        "form_name_belongs_to": investigation.form_name_belongs_to,
    }
    return found.model_copy(update=update), [judge_call, *agent_calls], trajectory


def main() -> int:
    """Run research on the 15 dev packets and show what it found and what it cost."""
    client = bedrock.make_client()
    total = 0.0
    try:
        for folder in sorted(paths.GOLD_DIR.glob("gp-*")):
            label = json.loads((folder / "label.json").read_text(encoding="utf-8"))
            if label["split"] != "dev":
                continue
            form = ApplicationForm.model_validate_json((folder / "form.json").read_text(encoding="utf-8"))
            result, calls, trajectory = research(client, form)
            cost = sum(c.cost_usd for c in calls)
            total += cost
            how = "same after normalizing" if not calls else f"{len(calls)} model call(s), ${cost:.4f}"
            print(
                f"{folder.name}: found={result.found} match={result.name_matches_form} ({how}) | "
                f"latest {result.latest_year}: revenue {result.latest_revenue}, expenses {result.latest_expenses}"
            )
            if result.name_explanation:
                print(f"    {form.organization_name!r} vs {result.registered_name!r}: {result.name_explanation}")
    except ClientError as e:
        print(f"Bedrock error: {e}", file=sys.stderr)
        return 1
    print(f"\nTotal model cost: ${total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
