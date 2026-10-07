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
    "input",
    "cache",
    "scan",
    "filings",
    "field_accuracy",
    "filings_all_correct",
    *ERROR_TYPES,
    "unsure_flagged",
    "unsure_caught",
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
    return classify(normalize(name, expected), normalize(name, got))


def classify(expected, got) -> str:
    """ "correct", or the error type, for two already-normalized values."""
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


def is_complete(run_dir) -> bool:
    """Whether a run got through all its filings. A run that stopped early (no credit, an API outage)
    covers an easier or harder subset, so scoring it alongside full runs would mislead."""
    summary = run_dir / "summary.json"
    if not summary.exists():
        return False  # it crashed before writing one
    return json.loads(summary.read_text(encoding="utf-8")).get("complete", True)  # older runs: complete


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

    # Claude's own doubts (runs made with --confidence): which fields it flagged as unsure, and how many
    # of the real errors are among them. A filing that got no answer is flagged whole: it plainly needs
    # a person. Runs made without asking have no doubts to score ("" in the history).
    asked = any(r.get("unsure") is not None for r in records)
    flagged = set()
    if asked:
        for r in records:
            fields = FIELD_NAMES if not r["answer"] else r.get("unsure") or []
            flagged |= {(r["object_id"], name) for name in fields}
    for error in errors:
        error["flagged"] = (error["object_id"], error["field"]) in flagged

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
        "input": first.get("input", "image-1568"),  # runs before step 4 all sent 1568 px images
        "cache": first.get("cache", False),
        "scan": first.get("scan") or "none",  # a simulated scan level (step 5b), or the IRS PDF
        "filings": len(records),
        "field_accuracy": round(1 - len(errors) / n_fields, 4),
        "filings_all_correct": filings_all_correct,
        **{error_type: totals[error_type] for error_type in ERROR_TYPES},
        "unsure_flagged": len(flagged) if asked else "",
        "unsure_caught": sum(e["flagged"] for e in errors) if asked else "",
        "retries": sum(len(r["attempts"]) - 1 for r in records),
        "check_failures": sum(bool(r["problems"]) for r in records),
        "cost_per_filing_usd": round(sum(r["cost_usd"] for r in records) / len(records), 4),
        # batch runs have no time per filing (they run together): "" in the history
        "mean_seconds": round(statistics.mean(seconds), 1)
        if (seconds := [r["seconds"] for r in records if r["seconds"] is not None])
        else "",
        "per_field": per_field,
        "errors": errors,
    }


def forget_missing(history, runs_dir) -> None:
    """Drop history rows whose run directory no longer exists (a deleted test or simulated run), so
    rescoring every run (--all) rebuilds the history from what is actually on disk."""
    if not history.exists():
        return
    with history.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        columns, rows = reader.fieldnames, [row for row in reader if (runs_dir / row["run"]).is_dir()]
    with history.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


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
        f"Run {score['run']}: {score['filings']} {score['split']} filings, "
        f"{score['model']}, {score['prompt']}, input {score['input']}{', cached' if score['cache'] else ''}, "
        f"scan {score['scan']}",
        f"  Field accuracy {score['field_accuracy']:.1%}; all 42 fields right on "
        f"{score['filings_all_correct']} of {score['filings']} filings",
        "  Errors: " + ", ".join(f"{t} {score[t]}" for t in ERROR_TYPES),
        f"  ${score['cost_per_filing_usd']:.4f} per filing"
        + (f", {score['mean_seconds']}s each; " if score["mean_seconds"] != "" else " (batch: no time per filing); ")
        + f"{score['retries']} retries, {score['check_failures']} filings still failing checks",
    ]
    if score["unsure_flagged"] != "":
        n_errors = len(score["errors"])
        n_fields = score["filings"] * len(FIELD_NAMES)
        lines.append(
            f"  Claude's doubts: flagged {score['unsure_flagged']} of {n_fields} fields "
            f"({score['unsure_flagged'] / n_fields:.1%}); {score['unsure_caught']} of {n_errors} errors were among them"
        )
    weak = [(name, c) for name, c in score["per_field"].items() if c["correct"] < score["filings"]]
    if weak:
        lines.append("  Fields with errors:")
        lines += [f"    {name:<42} {c['correct']}/{score['filings']}" for name, c in weak]
        lines.append("  Each error:")
        lines += [
            f"    {e['object_id']} {e['field']}: {e['error']} (expected {e['expected']!r}, got {e['got']!r})"
            + (" [flagged unsure]" if e.get("flagged") else "")
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
    if args.all:
        forget_missing(HISTORY, RUNS_DIR)
    for run_dir in run_dirs:
        if not is_complete(run_dir):
            print(f"Skipped {run_dir.name}: the run didn't finish (see its summary.json)", end="\n\n")
            continue
        score = score_run(run_dir)
        record(score)
        print(report(score), end="\n\n")
    print(f"Recorded in {HISTORY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
