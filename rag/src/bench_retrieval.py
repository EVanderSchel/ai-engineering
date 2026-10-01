"""Benchmark retrieval latency per strategy, so performance changes are measured rather than guessed.

    python src/bench_retrieval.py                        # uses data/eval_set_current_events.json
    python src/bench_retrieval.py --rounds 5

Runs every question in the eval file through each strategy. The first pass is a warm-up (model
loading, first-query setup) and isn't counted; the reported numbers are steady-state latencies.
"""

import argparse
import json
import pathlib
import statistics
import time

from retrieval import retrieve

ROOT = pathlib.Path(__file__).resolve().parent.parent
STRATEGIES = {
    "vector": {},
    "hybrid": {"hybrid": True},
    "rerank": {"use_reranker": True},
}


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval-file", type=pathlib.Path, default=ROOT / "data" / "eval_set_current_events.json")
    parser.add_argument("--rounds", type=int, default=3, help="Timed passes over all questions per strategy")
    args = parser.parse_args()

    questions = [case["question"] for case in json.loads(args.eval_file.read_text(encoding="utf-8"))]
    print(f"{len(questions)} questions x {args.rounds} rounds per strategy (after one warm-up pass)\n")
    print(f"{'strategy':<8} {'p50 ms':>8} {'p95 ms':>8} {'mean ms':>8}")
    for name, options in STRATEGIES.items():
        for q in questions:  # warm-up
            retrieve(q, **options)
        timings = []
        for _ in range(args.rounds):
            for q in questions:
                start = time.perf_counter()
                retrieve(q, **options)
                timings.append((time.perf_counter() - start) * 1000)
        p50, p95, mean = percentile(timings, 50), percentile(timings, 95), statistics.mean(timings)
        print(f"{name:<8} {p50:>8.1f} {p95:>8.1f} {mean:>8.1f}")


if __name__ == "__main__":
    main()
