"""Trajectory eval: run the agent on every question in evals/cases.jsonl, several times each, and grade it.

    python src/run_eval.py --variant baseline --reps 3                 # the full eval
    python src/run_eval.py --variant pilot --cases q01,q05,q13 --reps 1
    python src/run_eval.py --variant baseline --regrade                # re-grade saved runs (judge cost only)

Each (question, rep) runs the real agent (agent.run) against a frozen copy of the ProPublica API responses
in evals/snapshot/, so the expected answers stay valid after ProPublica adds new data. A request that isn't
in the snapshot is fetched live and added to it.

Output, in evals/runs/<variant>/:
- results.jsonl: one row per (question, rep), written as soon as it's graded. Rerunning skips rows already
  there, so a crashed run resumes where it stopped.
- traces/<id>_rep<k>.json: the full conversation (system prompt, question, tool calls and results, answer).
- errors.jsonl: attempts that failed for reasons other than the model (API errors, a judge with no
  verdict, the wrong model serving). They are never scored as wrong answers; rerunning retries them.
"""

import argparse
import json
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

import agent
import grade
import propublica
from paths import ROOT

EVALS_DIR = ROOT / "evals"
CASES_PATH = EVALS_DIR / "cases.jsonl"
SNAPSHOT_DIR = EVALS_DIR / "snapshot"
RUNS_DIR = EVALS_DIR / "runs"

_write_lock = threading.Lock()


def load_cases(ids: list[str] | None = None) -> list[dict]:
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids:
        unknown = set(ids) - {c["id"] for c in cases}
        if unknown:
            raise SystemExit(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c["id"] in ids]
    return cases


def read_rows(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_row(path, row: dict) -> None:
    with _write_lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def trace(result: agent.Result) -> list[dict]:
    """The conversation in the eval report's transcript format."""
    turns = [{"role": "system", "content": agent.SYSTEM_PROMPT}, {"role": "user", "content": result.question}]
    for event in result.trajectory:
        if event["type"] == "text":
            turns.append({"role": "assistant", "content": event["text"]})
        elif event["type"] == "guardrail":
            turns.append({"role": "user", "content": event["text"]})
        else:
            turns.append({"role": "tool_call", "name": event["name"], "content": json.dumps(event["input"], indent=2)})
            turns.append({"role": "tool_result", "content": event["output"], "is_error": event["is_error"]})
    return turns


def served_by(requested: str, served: list[str | None]) -> None:
    """Fail loudly if any call was answered by a different model than requested (an alias may resolve to
    a dated version of the same model, e.g. claude-x -> claude-x-20260101)."""
    wrong = {m for m in served if not (m or "").startswith(requested)}
    if wrong:
        raise RuntimeError(f"requested {requested} but served by {sorted(wrong, key=str)}")


def run_case(client, case: dict, rep: int, variant_dir, model: str) -> dict:
    started = time.monotonic()
    result = agent.run(client, case["question"], model=model)
    seconds = time.monotonic() - started
    served_by(model, result.served_models)
    traces = variant_dir / "traces"
    (traces / f"{case['id']}_rep{rep}.json").write_text(json.dumps(trace(result), indent=1), encoding="utf-8")
    return {
        "prompt_id": case["id"],
        "rep": rep,
        "prompt": case["question"],
        "tags": case["tags"],
        "model": result.served_models[-1] if result.served_models else model,
        "stop_reason": result.stop,
        # A clipped answer isn't scored as wrong: it's counted and left out of the means.
        "status": "truncated" if result.stop == "max_tokens" else "ok",
        "answer": result.answer,
        "trajectory": result.trajectory,
        "usage": {k: v for k, v in vars(result.usage).items()},
        "cost_usd": round(result.cost, 6),
        "model_calls": result.steps,
        "tool_calls": sum(e["type"] == "tool" for e in result.trajectory),
        "tool_errors": sum(e["type"] == "tool" and e["is_error"] for e in result.trajectory),
        "latency_s": round(seconds, 1),
        "grounding_retries": result.grounding_retries,
        "ungrounded_flagged": result.ungrounded,  # left in the final answer after the guardrail (if any)
    }


def grade_row(client, case: dict, row: dict) -> dict:
    # judge_cost_usd is the latest grading; judge_spent_usd adds up every grading of this row, re-grades
    # included, so the reported total is what was actually spent.
    spent = row.get("judge_spent_usd", row.get("judge_cost_usd", 0))
    if row["status"] != "ok":
        return row | {"grade": {}, "explanation": {}, "judge_model": None, "judge_usage": None, "judge_cost_usd": 0}
    graded = grade.grade(case, row["answer"], row["trajectory"], client)
    judge = graded.get("judge")
    judge_cost = 0.0
    if judge:
        fields = vars(agent.Usage()).keys()
        judge_cost = agent.Usage(**{k: judge["usage"].get(k) or 0 for k in fields}).cost(grade.JUDGE_MODEL)
    return row | {
        "grade": graded["grade"],
        "explanation": graded["explanation"],
        "judge_model": judge["model"] if judge else None,
        "judge_usage": judge["usage"] if judge else None,
        "judge_cost_usd": round(judge_cost, 6),
        "judge_spent_usd": round(spent + judge_cost, 6),
    }


def regrade(client, rows: list[dict], cases: dict) -> list[dict]:
    """Grade the saved answers of the selected cases again (e.g. after a rubric change); every other row is
    kept as it was, in order."""
    return [grade_row(client, cases[r["prompt_id"]], r) if r["prompt_id"] in cases else r for r in rows]


def summarize(rows: list[dict]) -> None:
    ok = [r for r in rows if r["status"] == "ok"]
    print(f"\n{len(rows)} runs ({len(rows) - len(ok)} truncated, not scored)")
    for metric in ("correct", "grounded"):
        scores = [r["grade"][metric] for r in ok]
        if scores:
            p = statistics.mean(scores)
            half = 1.96 * math.sqrt(p * (1 - p) / len(scores))  # 95% interval; noise, not just the point estimate
            print(f"  {metric:9} {p:6.1%}  (+/- {half:.1%}, n={len(scores)})")
    if ok:
        agent_cost = sum(r["cost_usd"] for r in rows)
        judge_cost = sum(r.get("judge_spent_usd", r.get("judge_cost_usd", 0)) for r in rows)  # re-grades included
        print(f"  cost      agent ${agent_cost:.3f} + judge ${judge_cost:.3f} = ${agent_cost + judge_cost:.3f}")
        print(
            f"  per run   ${statistics.median(r['cost_usd'] for r in ok):.4f} median agent cost, "
            f"{statistics.median(r['latency_s'] for r in ok):.0f} s median, "
            f"{statistics.mean(r['tool_calls'] for r in ok):.1f} tool calls mean"
        )
        sent_back = sum(r.get("grounding_retries", 0) > 0 for r in ok)
        flagged = sum(bool(r.get("ungrounded_flagged")) for r in ok)
        print(f"  guardrail sent back {sent_back} drafts; {flagged} final answers still flagged")
    by_case: dict[str, list[int]] = {}
    for r in ok:
        by_case.setdefault(r["prompt_id"], []).append(r["grade"]["correct"])
    failing = {cid: f"{sum(s)}/{len(s)}" for cid, s in sorted(by_case.items()) if sum(s) < len(s)}
    if failing:
        print("  not always correct:", ", ".join(f"{cid} {score}" for cid, score in failing.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", required=True, help="output folder under evals/runs, e.g. baseline")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--model", default=agent.MODEL, choices=sorted(agent.PRICES))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--regrade", action="store_true", help="grade saved runs again instead of running")
    args = parser.parse_args()

    propublica.CACHE_DIR = SNAPSHOT_DIR
    cases = {c["id"]: c for c in load_cases(args.cases.split(",") if args.cases else None)}
    variant_dir = RUNS_DIR / args.variant
    (variant_dir / "traces").mkdir(parents=True, exist_ok=True)
    results_path, errors_path = variant_dir / "results.jsonl", variant_dir / "errors.jsonl"
    client = anthropic.Anthropic(timeout=180, max_retries=4)  # the SDK retries 429/5xx with backoff

    if args.regrade:
        rows = regrade(client, read_rows(results_path), cases)
        results_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        summarize(rows)
        return

    done = {(r["prompt_id"], r["rep"]) for r in read_rows(results_path)}
    todo = [(c, rep) for c in cases.values() for rep in range(args.reps) if (c["id"], rep) not in done]
    print(f"{len(todo)} runs to do ({len(done)} already done) on {args.model}, {args.workers} at a time")

    def attempt(case, rep):
        row = run_case(client, case, rep, variant_dir, args.model)
        return grade_row(client, case, row)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(attempt, case, rep): (case, rep) for case, rep in todo}
        for future in as_completed(futures):
            case, rep = futures[future]
            try:
                row = future.result()
            except Exception as e:  # infrastructure, not the model: recorded, never scored
                append_row(errors_path, {"prompt_id": case["id"], "rep": rep, "error": f"{type(e).__name__}: {e}"})
                print(f"  {case['id']} rep {rep}: ERROR {type(e).__name__}: {e}")
                continue
            append_row(results_path, row)
            g = row["grade"]
            print(
                f"  {case['id']} rep {rep}: correct={g.get('correct')} grounded={g.get('grounded')} "
                f"stop={row['stop_reason']} tools={row['tool_calls']} ${row['cost_usd'] + row['judge_cost_usd']:.4f}"
            )

    summarize([r for r in read_rows(results_path) if r["prompt_id"] in cases])


if __name__ == "__main__":
    main()
