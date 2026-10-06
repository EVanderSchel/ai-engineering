"""Score an extraction run against the gold set's answer keys, and record the score in results/history.csv.

    python src/evaluate.py data/runs/<run>        # score one run and record it
    python src/evaluate.py --all                  # rescore every saved run (after fixing an answer key
                                                  # or a scoring rule; no API calls, nothing spent)

Scoring rules: a field is correct when the extracted value equals the answer key's after normalization.
Normalization only forgives differences that don't change what the value means:
- organization_name and mission: case and whitespace are ignored ("SCHOOLINC" = "SCHOOL INC"; a mission
  wrapped over three lines = the same mission on one line). Spelling and punctuation still count.
- ein: a dash is ignored ("41-1657792" = "411657792"). Every digit still counts.
- numbers and dates: exact. Blank (None) and 0 are different answers: a blank line read as 0 is wrong.

Every wrong field also gets an error type, because "98% correct" hides what to fix:
    blank_as_zero   the line is blank, Claude read 0
    zero_as_blank   the line shows 0, Claude said blank
    missed          the line has a value, Claude said blank
    invented        the line is blank, Claude gave a (non-zero) value
    wrong_value     both have a value, and they differ
"""

import argparse
import csv
import json
import pathlib
import re
import statistics
from collections import Counter

from fields import FIELD_NAMES
from paths import DATA_DIR, GOLD_DIR, ROOT

RUNS_DIR = DATA_DIR / "runs"
HISTORY = ROOT / "results" / "history.csv"
ERROR_TYPES = ["blank_as_zero", "zero_as_blank", "missed", "invented", "wrong_value"]
HISTORY_COLUMNS = [
    "run",
    "split",
    "model",
    "prompt",
    "prompt_sha256",
    "filings",
    "field_accuracy",
    "filings_all_correct",
    *ERROR_TYPES,
    "retries",
    "check_failures",
    "cost_per_filing_usd",
    "mean_seconds",
]
TEXT_FIELDS = {"organization_name", "mission"}


def normalize(name: str, value):
    if value is None:
        return None
    if name in TEXT_FIELDS:
        return re.sub(r"\s+", "", value).casefold()
    if name == "ein":
        return value.replace("-", "")
    return value


def outcome(name: str, expected, got) -> str:
    """ "correct", or the error type."""
    expected, got = normalize(name, expected), normalize(name, got)
    if expected == got:
        return "correct"
    if expected is None:
        return "blank_as_zero" if got == 0 else "invented"
    if got is None:
        return "zero_as_blank" if expected == 0 else "missed"
    return "wrong_value"


def gold() -> dict[str, dict]:
    """Answer keys and their split, by object ID."""
    with (GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
        splits = {row["object_id"]: row["split"] for row in csv.DictReader(f)}
    keys = {}
    for object_id, split in splits.items():
        fields = json.loads((GOLD_DIR / f"{object_id}.json").read_text(encoding="utf-8"))["fields"]
        keys[object_id] = {"split": split, "fields": fields}
    return keys


def score_run(run_dir) -> dict:
    """Per-field and overall results for one run directory (one JSON record per filing)."""
    answer_keys = gold()
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*.json"))]
    records = [r for r in records if "object_id" in r]  # skip summary.json
    if not records:
        raise SystemExit(f"no filing records in {run_dir}")

    per_field = {name: Counter() for name in FIELD_NAMES}
    errors = []
    filings_all_correct = 0
    for record in records:
        key = answer_keys[record["object_id"]]
        answer = record["answer"] or {}  # None: Claude stopped without an answer, every field is missed
        wrong = 0
        for name in FIELD_NAMES:
            result = outcome(name, key["fields"][name], answer.get(name))
            per_field[name][result] += 1
            if result != "correct":
                wrong += 1
                errors.append(
                    {
                        "object_id": record["object_id"],
                        "field": name,
                        "error": result,
                        "expected": key["fields"][name],
                        "got": answer.get(name),
                    }
                )
        filings_all_correct += wrong == 0

    splits = {answer_keys[r["object_id"]]["split"] for r in records}
    totals = Counter(error["error"] for error in errors)
    n_fields = len(records) * len(FIELD_NAMES)
    first = records[0]
    return {
        "run": run_dir.name,
        "split": "+".join(sorted(splits)),
        "model": first["model"],
        "prompt": first["prompt"],
        "prompt_sha256": first["prompt_sha256"],
        "filings": len(records),
        "field_accuracy": round(1 - len(errors) / n_fields, 4),
        "filings_all_correct": filings_all_correct,
        **{error_type: totals[error_type] for error_type in ERROR_TYPES},
        "retries": sum(len(r["attempts"]) - 1 for r in records),
        "check_failures": sum(bool(r["problems"]) for r in records),
        "cost_per_filing_usd": round(sum(r["cost_usd"] for r in records) / len(records), 4),
        "mean_seconds": round(statistics.mean(r["seconds"] for r in records), 1),
        "per_field": per_field,
        "errors": errors,
    }


def record(score: dict) -> None:
    """Add the run's row to results/history.csv, replacing any earlier row for the same run."""
    rows = []
    if HISTORY.exists():
        with HISTORY.open(encoding="utf-8", newline="") as f:
            rows = [row for row in csv.DictReader(f) if row["run"] != score["run"]]
    rows.append({column: score[column] for column in HISTORY_COLUMNS})
    rows.sort(key=lambda row: row["run"])
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def report(score: dict) -> str:
    lines = [
        f"Run {score['run']}: {score['filings']} {score['split']} filings, {score['model']}, {score['prompt']}",
        f"  Field accuracy {score['field_accuracy']:.1%}; all 42 fields right on "
        f"{score['filings_all_correct']} of {score['filings']} filings",
        "  Errors: " + ", ".join(f"{t} {score[t]}" for t in ERROR_TYPES),
        f"  ${score['cost_per_filing_usd']:.4f} and {score['mean_seconds']}s per filing; "
        f"{score['retries']} retries, {score['check_failures']} filings still failing checks",
    ]
    weak = [(name, c) for name, c in score["per_field"].items() if c["correct"] < score["filings"]]
    if weak:
        lines.append("  Fields with errors:")
        lines += [f"    {name:<42} {c['correct']}/{score['filings']}" for name, c in weak]
        lines.append("  Each error:")
        lines += [
            f"    {e['object_id']} {e['field']}: {e['error']} (expected {e['expected']!r}, got {e['got']!r})"
            for e in score["errors"]
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="*", help="run directories to score")
    parser.add_argument("--all", action="store_true", help="rescore every run in data/runs")
    args = parser.parse_args()
    run_dirs = sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()) if args.all else [pathlib.Path(p) for p in args.runs]
    if not run_dirs:
        parser.error("give a run directory, or --all")
    for run_dir in run_dirs:
        score = score_run(run_dir)
        record(score)
        print(report(score), end="\n\n")
    print(f"Recorded in {HISTORY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
