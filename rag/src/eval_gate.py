"""Regression gate: run every retrieval configuration in the baseline file and fail if any score drops.

    python src/eval_gate.py            # compare against data/eval_baseline.json; exit 1 on regression
    python src/eval_gate.py --update   # accept current scores as the new baseline (commit the result)

Each corpus is ingested into its own throwaway Chroma folder, so the gate never touches chroma_db/
and corpora can't overwrite each other. CI runs this on every pull request that changes rag/.
"""

import argparse
import json
import os
import pathlib
import sys
import tempfile

import retrieval
from eval import run_eval
from ingest import build_collection

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = ROOT / "data" / "eval_baseline.json"
METRICS = ("hit_at_1", "hit_at_k", "mrr")
STRATEGIES = {
    "vector": {"hybrid": False, "use_reranker": False},
    "hybrid": {"hybrid": True, "use_reranker": False},
    "rerank": {"hybrid": False, "use_reranker": True},
}
# Scores are compared with a tiny tolerance for floating-point noise only. With 13 questions, one
# question moving from rank 1 to rank 2 costs 7.7% Hit@1, so any real change clears it.
EPSILON = 1e-9


def run_all(baseline: dict) -> list[dict]:
    settings = baseline["settings"]
    results = []
    os.environ.pop("CHROMA_HOST", None)  # always use a local throwaway folder, never a server

    for corpus_name, corpus in baseline["corpora"].items():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.environ["CHROMA_PATH"] = tmp
            retrieval.clear_caches()
            build_collection(
                ROOT / corpus["docs_dir"], settings["chunk_size"], settings["overlap"], settings["embedding_model"]
            )
            for run in baseline["runs"]:
                if run["corpus"] != corpus_name:
                    continue
                scores = run_eval(
                    ROOT / corpus["eval_file"],
                    settings["k"],
                    verbose=False,
                    embedding_model=settings["embedding_model"],
                    **STRATEGIES[run["strategy"]],
                )
                results.append({"run": run, "scores": scores})
    os.environ.pop("CHROMA_PATH", None)
    return results


def compare(results: list[dict]) -> tuple[list[str], bool, bool]:
    """Return report lines (Markdown table), whether anything regressed, and whether anything improved."""
    lines = ["| corpus | strategy | " + " | ".join(METRICS) + " | result |", "|---" * (len(METRICS) + 3) + "|"]
    regressed = improved = False
    for r in results:
        run, scores = r["run"], r["scores"]
        cells, status = [], "pass"
        for m in METRICS:
            now, floor = scores[m], run["min"][m]
            if now < floor - EPSILON:
                cells.append(f"**{now:.3f}** (was {floor:.3f})")
                status, regressed = "REGRESSED", True
            elif now > floor + EPSILON:
                cells.append(f"{now:.3f} (up from {floor:.3f})")
                improved = True
            else:
                cells.append(f"{now:.3f}")
        lines.append(f"| {run['corpus']} | {run['strategy']} | " + " | ".join(cells) + f" | {status} |")
    return lines, regressed, improved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", type=pathlib.Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update", action="store_true", help="Overwrite the baseline with the current scores")
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    results = run_all(baseline)

    if args.update:
        for r in results:
            r["run"]["min"] = {m: round(r["scores"][m], 4) for m in METRICS}
        args.baseline.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"Baseline updated: {args.baseline}")
        return 0

    lines, regressed, improved = compare(results)
    if regressed:
        verdict = "Retrieval quality regressed: at least one score is below the baseline."
    elif improved:
        verdict = "No regressions, and some scores improved. Run `python src/eval_gate.py --update` and commit the new baseline to lock them in."
    else:
        verdict = "No regressions: every score matches the baseline."
    report = "\n".join(["## Retrieval eval gate", "", *lines, "", verdict])
    print("\n" + report)

    # On GitHub Actions, also show the table on the workflow run's summary page.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report + "\n")

    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
