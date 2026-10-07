"""Step 6.3: decide what a person should check, and measure what that buys.

A review rule turns signals into a set of fields to send to a person:
- whole-document signals (send every field): Claude gave no answer; the form's arithmetic still failed,
  or only passed on a retry; Claude wrote more than `max_output_tokens` (output is mostly thinking, and
  hard pages make it think much longer);
- field signals: the fields Claude said it wasn't sure of (prompt v3, --confidence); the fields where a
  second, independent run disagrees (it doubles the API cost).

A rule is scored on a run against the answer keys, assuming the reviewer fixes everything they're shown:
how many errors it sends to review (and so fixes), how many fields a person has to look at, and how
accurate the data is afterwards.

    python src/review.py data/runs/<run> [--second data/runs/<another run of the same pages>]
"""

import argparse
import json
import pathlib
from dataclasses import dataclass

import evaluate
from fields import FIELD_NAMES


@dataclass(frozen=True)
class Rule:
    name: str
    max_output_tokens: int | None = None  # review the whole document above this; None: don't use
    checks: bool = False  # review the whole document if the checks failed or needed a retry
    doubts: bool = False  # review the fields Claude listed as unsure
    second_run: bool = False  # review the fields where a second run disagrees


def _difficult(record: dict, rule: Rule) -> bool:
    """Whether the whole document goes to review."""
    if not record["answer"]:
        return True  # no answer at all: nothing to trust, under any rule
    if rule.checks and (record["problems"] or len(record["attempts"]) > 1):
        return True
    return rule.max_output_tokens is not None and record["usage"]["output_tokens"] > rule.max_output_tokens


def flagged_fields(record: dict, rule: Rule, second: dict | None = None) -> set[str]:
    """The fields of one filing that this rule sends to a person."""
    if _difficult(record, rule):
        return set(FIELD_NAMES)
    flags = set()
    if rule.doubts:
        flags |= set(record.get("unsure") or [])
    if rule.second_run and second is not None:
        other = second["answer"] or {}
        flags |= {
            name
            for name in FIELD_NAMES
            if evaluate.normalize(name, record["answer"][name]) != evaluate.normalize(name, other.get(name))
        }
    return flags


def score_rule(records: list[dict], rule: Rule, seconds: dict[str, dict] | None = None) -> dict:
    keys = evaluate.gold()
    errors = caught = reviewed = whole = 0
    for record in records:
        key = keys[record["object_id"]]["fields"]
        flags = flagged_fields(record, rule, (seconds or {}).get(record["object_id"]))
        reviewed += len(flags)
        whole += len(flags) == len(FIELD_NAMES)
        answer = record["answer"] or {}
        for name in FIELD_NAMES:
            if evaluate.outcome(name, key[name], answer.get(name)) != "correct":
                errors += 1
                caught += name in flags
    n_fields = len(records) * len(FIELD_NAMES)
    return {
        "rule": rule.name,
        "errors": errors,
        "caught": caught,
        "fields_reviewed": reviewed,
        "review_share": reviewed / n_fields,
        "documents_reviewed_whole": whole,
        "accuracy_before": 1 - errors / n_fields,
        "accuracy_after": 1 - (errors - caught) / n_fields,
    }


def load(run_dir: pathlib.Path) -> list[dict]:
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*.json"))]
    return [r for r in records if "object_id" in r]


RULES = [
    Rule("no review (only filings with no answer)"),
    Rule("checks", checks=True),
    Rule("checks + long thinking (> 3,000 tokens)", max_output_tokens=3000, checks=True),
    Rule("checks + long thinking (> 2,000 tokens)", max_output_tokens=2000, checks=True),
    Rule("Claude's doubts", doubts=True),
    Rule("checks + long thinking (> 2,000) + doubts", max_output_tokens=2000, checks=True, doubts=True),
    Rule("second run disagrees", second_run=True),
    Rule("checks + long thinking (> 2,000) + second run", max_output_tokens=2000, checks=True, second_run=True),
    Rule(
        "everything",
        max_output_tokens=2000,
        checks=True,
        doubts=True,
        second_run=True,
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=pathlib.Path)
    parser.add_argument("--second", type=pathlib.Path, help="another run of the same pages, for disagreement")
    args = parser.parse_args()

    records = load(args.run)
    seconds = {r["object_id"]: r for r in load(args.second)} if args.second else None
    print(f"{args.run.name}: {len(records)} filings" + (f", second run {args.second.name}" if args.second else ""))
    print(f"  {'rule':<48} {'errors sent to review':>22} {'fields reviewed':>16} {'accuracy after':>15}")
    for rule in RULES:
        if (rule.second_run and seconds is None) or (rule.doubts and all(r.get("unsure") is None for r in records)):
            continue
        s = score_rule(records, rule, seconds)
        print(
            f"  {s['rule']:<48} {s['caught']:>9} of {s['errors']:<4} ({s['caught'] / max(s['errors'], 1):>4.0%})"
            f" {s['review_share']:>15.1%} {s['accuracy_after']:>14.1%}"
        )


if __name__ == "__main__":
    main()
