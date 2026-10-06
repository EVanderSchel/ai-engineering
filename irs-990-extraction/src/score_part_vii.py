"""Score a Part VII run against the answer keys, and record the score in results/part_vii_history.csv.

    python src/score_part_vii.py data/part_vii_runs/<run>
    python src/score_part_vii.py --all      # rescore every finished run (no API calls)

Scoring a list takes one step more than scoring fields: first decide which extracted row is which
answer-key row, then compare them. Rows are matched by name:
1. the same name once case, spaces, and punctuation are ignored ("Dr. Alan Benson" = "DR ALAN BENSON");
   duplicates pair up in order;
2. then, among the rest, the most similar names (at least 80% alike), so a misspelled name still counts
   as that person, with the name field marked wrong.
An answer-key row left over is a *missed* person; an extracted row left over is an *invented* one.

Matched rows are compared field by field with the same rules and error types as Part I (evaluate.py):
names and titles ignore case and spaces, everything else is exact, and a blank is never 0. The line 1d
totals and the line 2 count are scored as fields too.
"""

import argparse
import csv
import difflib
import json
import pathlib
import re
import statistics
from collections import Counter

import evaluate
from fields import PART_VII_COLUMNS, PART_VII_TOTALS
from paths import DATA_DIR, GOLD_DIR, ROOT

RUNS_DIR = DATA_DIR / "part_vii_runs"
HISTORY = ROOT / "results" / "part_vii_history.csv"
TEXT_COLUMNS = {"name", "title"}
SIMILAR_NAMES = 0.8
HISTORY_COLUMNS = [
    "run",
    "model",
    "prompt",
    "prompt_sha256",
    "scan",
    "returns",
    "people",
    "missed_people",
    "invented_people",
    "field_accuracy",
    "totals_correct",
    "returns_all_correct",
    *evaluate.ERROR_TYPES,
    "retries",
    "check_failures",
    "cost_per_return_usd",
    "mean_seconds",
]


def name_key(name: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (name or "").casefold())


def match_rows(expected: list[dict], got: list[dict]) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Pairs of (expected index, extracted index), then unmatched expected and unmatched extracted indexes."""
    pairs, left_expected, left_got = [], list(range(len(expected))), list(range(len(got)))
    for i in list(left_expected):  # 1. same name, in order
        for j in left_got:
            if name_key(expected[i]["name"]) == name_key(got[j]["name"]):
                pairs.append((i, j))
                left_expected.remove(i)
                left_got.remove(j)
                break
    candidates = sorted(  # 2. most similar remaining names first
        (
            (difflib.SequenceMatcher(None, name_key(expected[i]["name"]), name_key(got[j]["name"])).ratio(), i, j)
            for i in left_expected
            for j in left_got
        ),
        reverse=True,
    )
    for ratio, i, j in candidates:
        if ratio >= SIMILAR_NAMES and i in left_expected and j in left_got:
            pairs.append((i, j))
            left_expected.remove(i)
            left_got.remove(j)
    return sorted(pairs), left_expected, left_got


def _normalize(column: str, value):
    if column in TEXT_COLUMNS and value is not None:
        return re.sub(r"\s+", "", value).casefold()
    return value


def score_return(expected: dict, got: dict | None) -> dict:
    """Missed and invented people, and every wrong field, for one return."""
    got = got or {"rows": [], "totals": dict.fromkeys(f.name for f in PART_VII_TOTALS)}
    pairs, missed, invented = match_rows(expected["rows"], got["rows"])
    errors = []
    for i, j in pairs:
        for column in (f.name for f in PART_VII_COLUMNS):
            e, g = expected["rows"][i][column], got["rows"][j][column]
            result = evaluate.classify(_normalize(column, e), _normalize(column, g))
            if result != "correct":
                errors.append(
                    {"row": expected["rows"][i]["name"], "field": column, "error": result, "expected": e, "got": g}
                )
    for total in (f.name for f in PART_VII_TOTALS):
        e, g = expected["totals"][total], got["totals"][total]
        result = evaluate.classify(e, g)
        if result != "correct":
            errors.append({"row": "(totals)", "field": total, "error": result, "expected": e, "got": g})
    return {
        "people": len(expected["rows"]),
        "matched": len(pairs),
        "missed": [expected["rows"][i]["name"] for i in missed],
        "invented": [got["rows"][j]["name"] for j in invented],
        "errors": errors,
    }


def score_run(run_dir) -> dict:
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*.json"))]
    records = [r for r in records if "object_id" in r]  # skip summary.json
    if not records:
        raise SystemExit(f"no return records in {run_dir}")

    per_return = {}
    for record in records:
        key = json.loads((GOLD_DIR / f"{record['object_id']}.json").read_text(encoding="utf-8"))["part_vii"]
        per_return[record["object_id"]] = score_return(key, record["answer"])

    row_errors = [e for s in per_return.values() for e in s["errors"] if e["row"] != "(totals)"]
    total_errors = [e for s in per_return.values() for e in s["errors"] if e["row"] == "(totals)"]
    matched_fields = sum(s["matched"] for s in per_return.values()) * len(PART_VII_COLUMNS)
    n_totals = len(records) * len(PART_VII_TOTALS)
    types = Counter(e["error"] for s in per_return.values() for e in s["errors"])
    first = records[0]
    return {
        "run": run_dir.name,
        "model": first["model"],
        "prompt": first["prompt"],
        "prompt_sha256": first["prompt_sha256"],
        "scan": first.get("scan") or "none",
        "returns": len(records),
        "people": sum(s["people"] for s in per_return.values()),
        "missed_people": sum(len(s["missed"]) for s in per_return.values()),
        "invented_people": sum(len(s["invented"]) for s in per_return.values()),
        "field_accuracy": round(1 - len(row_errors) / matched_fields, 4) if matched_fields else 0.0,
        "totals_correct": f"{n_totals - len(total_errors)}/{n_totals}",
        "returns_all_correct": sum(
            not s["missed"] and not s["invented"] and not s["errors"] for s in per_return.values()
        ),
        **{t: types[t] for t in evaluate.ERROR_TYPES},
        "retries": sum(len(r["attempts"]) - 1 for r in records),
        "check_failures": sum(bool(r["problems"]) for r in records),
        "cost_per_return_usd": round(sum(r["cost_usd"] for r in records) / len(records), 4),
        "mean_seconds": round(statistics.mean(r["seconds"] for r in records), 1),
        "per_return": per_return,
    }


def record(score: dict) -> None:
    """Add the run's row to results/part_vii_history.csv, replacing any earlier row for the same run."""
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
        f"Run {score['run']}: {score['returns']} returns, {score['people']} people, "
        f"{score['model']}, {score['prompt']}",
        f"  People: {score['missed_people']} missed, {score['invented_people']} invented; "
        f"fields of matched rows {score['field_accuracy']:.1%} correct; totals {score['totals_correct']} correct; "
        f"{score['returns_all_correct']} of {score['returns']} returns entirely right",
        "  Errors: " + ", ".join(f"{t} {score[t]}" for t in evaluate.ERROR_TYPES),
        f"  ${score['cost_per_return_usd']:.4f} and {score['mean_seconds']}s per return; "
        f"{score['retries']} retries, {score['check_failures']} returns still failing checks",
    ]
    for object_id, s in score["per_return"].items():
        if s["missed"] or s["invented"] or s["errors"]:
            lines.append(f"  {object_id} ({s['people']} people):")
            if s["missed"]:
                lines.append(f"    missed: {s['missed']}")
            if s["invented"]:
                lines.append(f"    invented: {s['invented']}")
            lines += [
                f"    {e['row']} {e['field']}: {e['error']} (expected {e['expected']!r}, got {e['got']!r})"
                for e in s["errors"]
            ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="*", help="run directories to score")
    parser.add_argument("--all", action="store_true", help="rescore every run in data/part_vii_runs")
    args = parser.parse_args()
    run_dirs = sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()) if args.all else [pathlib.Path(p) for p in args.runs]
    if not run_dirs:
        parser.error("give a run directory, or --all")
    for run_dir in run_dirs:
        if not evaluate.is_complete(run_dir):
            print(f"Skipped {run_dir.name}: the run didn't finish (see its summary.json)", end="\n\n")
            continue
        score = score_run(run_dir)
        record(score)
        print(report(score), end="\n\n")
    print(f"Recorded in {HISTORY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
