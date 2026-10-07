"""Step 7.3: Part I extraction with several requests in flight at once, for when answers are needed now.

extract.py sends one request at a time; batch.py hands everything over and waits up to hours for half
the price. This sits in between: full price, but WORKERS requests running together, so 21 filings take
about as long as the slowest few instead of all of them added up.

    python src/parallel.py                 # every dev filing, 8 at a time
    python src/parallel.py --workers 16

How it works:
- Threads, not asyncio: each request is mostly waiting on the network, and a thread pool lets every
  worker run extract.py's own, tested extract() unchanged (the Anthropic client is thread-safe).
- Rate limits: when the API answers 429 (too many requests) or is overloaded, the SDK waits and retries
  by itself, following the server's retry-after hint; here it's allowed more retries than usual,
  because more requests at once means more chances to hit the limit.
- Caching: requests that start together all miss the cache and each writes it (at 1.25x). So the
  first filing goes alone, filling the cache with the prompt and schema, and the rest fan out to read it.
- An error the SDK can't retry away (no credit, a bad key) would hit every request: the remaining work
  is canceled and the run is marked incomplete, as in extract.py.

Results are saved in the usual run format, so evaluate.py scores them.
"""

import argparse
import csv
import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

import anthropic

import evaluate
import prompts
import scans
from extract import INPUT, MODEL, PRICES, RUNS_DIR, Usage, check_input, extract_filing
from paths import GOLD_DIR

WORKERS = 8
MAX_RETRIES = 6  # the SDK's default is 2; parallel requests meet rate limits more often


def run(client, rows: list[dict], run_dir, *, workers: int, model: str, input_kind: str, scan: str | None) -> dict:
    """Extract every row, `workers` at a time after a first one alone. Returns the summary it saves."""
    started_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    started = time.monotonic()
    total, done, error = Usage(), [], None

    def one(row):
        return extract_filing(client, row["object_id"], row["band"], input_kind=input_kind, scan=scan, model=model)

    def save(row, record, result):
        total.add(result.usage)
        done.append(row["object_id"])
        (run_dir / f"{row['object_id']}.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(
            f"{row['object_id']}: {len(result.attempts)} attempt(s), "
            f"checks {'passed' if not result.problems else 'FAILED'}, {record['seconds']}s"
        )

    try:
        if rows:
            save(rows[0], *one(rows[0]))  # alone: fills the cache for the rest
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, row): row for row in rows[1:]}
            try:
                for future in as_completed(futures):
                    save(futures[future], *future.result())
            except anthropic.APIStatusError, anthropic.APIConnectionError:
                for future in futures:
                    future.cancel()  # requests not started yet never start
                raise
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        error = f"{type(e).__name__}: {e}"

    summary = {
        "run": run_dir.name,
        "complete": error is None and len(done) == len(rows),
        "filings": len(done),
        "of": len(rows),
        "error": error,
        "workers": workers,
        "started_at": started_at,
        "wall_seconds": round(time.monotonic() - started, 1),
        "cost_usd": total.cost(model),
        "usage": asdict(total),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--limit", type=int, help="only the first N filings of the split (default: all)")
    parser.add_argument("--workers", type=int, default=WORKERS, help="requests in flight at once")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    parser.add_argument("--input", default=INPUT, help="how to send page 1: image-<pixels> or pdf")
    parser.add_argument("--scan", choices=sorted(scans.LEVELS), help="read a simulated scan")
    parser.add_argument("--final", action="store_true", help="required for --split test")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        parser.error("the test split is held out for final scores; add --final if this is one")
    try:
        check_input(args.input)
    except ValueError as e:
        parser.error(str(e))

    with (GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
        rows = [row for row in csv.DictReader(f) if row["split"] == args.split][: args.limit]
    version = prompts.load("extract").version
    run_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{args.model}_{version}_{args.input}_cache"
    run_id += (f"_scan-{args.scan}" if args.scan else "") + f"_parallel-{args.workers}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)

    client = anthropic.Anthropic(max_retries=MAX_RETRIES)
    summary = run(client, rows, run_dir, workers=args.workers, model=args.model, input_kind=args.input, scan=args.scan)
    print(f"\n{summary['filings']} of {len(rows)} filings in {summary['wall_seconds']}s, ${summary['cost_usd']:.4f}")
    if not summary["complete"]:
        raise SystemExit(f"Stopped: {summary['error']}")
    score = evaluate.score_run(run_dir)
    evaluate.record(score)
    print(evaluate.report(score))


if __name__ == "__main__":
    main()
