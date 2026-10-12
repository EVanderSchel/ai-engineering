"""Prompt-injection eval (step 6): eval questions answered with hostile text planted in tool results.

    python src/run_injection.py --variant baseline              # 6 questions x 4 attacks x 2 vectors
    python src/run_injection.py --variant pilot --cases q01,q22 --attacks change_figure,spoof_check

Per run, three judgments:
- followed: the agent acted on the planted instruction (injection judge; plus a crude code signal).
- flagged: the agent warned the user about suspicious text in the data (injection judge).
- correct / grounded: the original question was still answered right (the step 3 grader).
Runs where no tool returned the target organization never saw the attack ("exposed" = false) and are left
out of the attack rates.

Like run_eval.py: the frozen API snapshot, rows written as they finish (rerunning resumes), infrastructure
failures in errors.jsonl, and a check that the requested model answered. Output in evals/injection/<variant>/.
"""

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

import agent
import injection
import propublica
import run_eval
from paths import ROOT

OUT_DIR = ROOT / "evals" / "injection"


def judge_cost(usage) -> float:
    fields = vars(agent.Usage()).keys()
    values = usage.model_dump() if hasattr(usage, "model_dump") else usage
    return agent.Usage(**{k: values.get(k) or 0 for k in fields}).cost(run_eval.grade.JUDGE_MODEL)


def run_one(client, case: dict, attack: str, vector: str, rep: int, out_dir, model: str) -> dict:
    run_id = f"{case['id']}-{attack}-{vector}"
    execute = injection.make_execute(injection.TARGETS[case["id"]], attack, vector)
    started = time.monotonic()
    result = agent.run(client, case["question"], model=model, execute=execute)
    seconds = time.monotonic() - started
    run_eval.served_by(model, result.served_models)
    (out_dir / "traces" / f"{run_id}_rep{rep}.json").write_text(
        json.dumps(run_eval.trace(result), indent=1), encoding="utf-8"
    )

    was_exposed = injection.exposed(result.trajectory, attack)
    graded = run_eval.grade.grade(case, result.answer, result.trajectory, client)
    judge_spent = judge_cost(graded["judge"]["usage"]) if graded.get("judge") else 0.0
    verdict = None
    if result.answer and was_exposed:
        verdict, usage, _ = injection.judge(client, case["question"], attack, result.answer)
        judge_spent += judge_cost(usage)

    return {
        "prompt_id": run_id,
        "case": case["id"],
        "attack": attack,
        "vector": vector,
        "rep": rep,
        "prompt": case["question"],
        "model": result.served_models[-1] if result.served_models else model,
        "stop_reason": result.stop,
        "answer": result.answer,
        "trajectory": result.trajectory,
        "exposed": was_exposed,
        "followed": int(verdict.followed) if verdict else None,
        "flagged": int(verdict.flagged) if verdict else None,
        "injection_reason": verdict.reason if verdict else None,
        "code_signal": int(injection.code_signal(attack, result.answer)),
        "grade": graded["grade"],
        "explanation": graded["explanation"],
        "grounding_retries": result.grounding_retries,
        "cost_usd": round(result.cost, 6),
        "judge_cost_usd": round(judge_spent, 6),
        "tool_calls": sum(e["type"] == "tool" for e in result.trajectory),
        "latency_s": round(seconds, 1),
    }


def rate(values: list) -> str:
    values = [v for v in values if v is not None]
    return f"{statistics.mean(values):5.0%} ({sum(values)}/{len(values)})" if values else "   -"


def summarize(rows: list[dict]) -> None:
    exposed = [r for r in rows if r["exposed"]]
    print(f"\n{len(rows)} runs, {len(exposed)} exposed to the planted text")
    print(f"  {'':24} {'followed':>14} {'flagged':>14} {'correct':>14}")
    groups = [("all", exposed)]
    groups += [(f"attack {a}", [r for r in exposed if r["attack"] == a]) for a in injection.ATTACKS]
    groups += [(f"vector {v}", [r for r in exposed if r["vector"] == v]) for v in injection.VECTORS]
    for label, group in groups:
        if group:
            print(
                f"  {label:24} {rate([r['followed'] for r in group]):>14} {rate([r['flagged'] for r in group]):>14} "
                f"{rate([r['grade'].get('correct') for r in group]):>14}"
            )
    disagree = [r["prompt_id"] for r in exposed if r["followed"] is not None and r["followed"] != r["code_signal"]]
    if disagree:
        print(f"  judge and code signal disagree on: {', '.join(disagree)} (read these)")
    cost = sum(r["cost_usd"] + r["judge_cost_usd"] for r in rows)
    print(f"  cost ${cost:.3f} (agent ${sum(r['cost_usd'] for r in rows):.3f})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--cases", help=f"comma-separated, from {','.join(injection.TARGETS)}")
    parser.add_argument("--attacks", help=f"comma-separated, from {','.join(injection.ATTACKS)}")
    parser.add_argument("--vectors", help=f"comma-separated, from {','.join(injection.VECTORS)}")
    parser.add_argument("--model", default=agent.MODEL, choices=sorted(agent.PRICES))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    case_ids = args.cases.split(",") if args.cases else list(injection.TARGETS)
    attacks = args.attacks.split(",") if args.attacks else list(injection.ATTACKS)
    vectors = args.vectors.split(",") if args.vectors else list(injection.VECTORS)
    for name, chosen, allowed in (
        ("case", case_ids, injection.TARGETS),
        ("attack", attacks, injection.ATTACKS),
        ("vector", vectors, injection.VECTORS),
    ):
        if unknown := set(chosen) - set(allowed):
            raise SystemExit(f"unknown {name}: {sorted(unknown)}")

    propublica.CACHE_DIR = run_eval.SNAPSHOT_DIR
    cases = {c["id"]: c for c in run_eval.load_cases(case_ids)}
    out_dir = OUT_DIR / args.variant
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    results_path, errors_path = out_dir / "results.jsonl", out_dir / "errors.jsonl"
    client = anthropic.Anthropic(timeout=180, max_retries=4)

    done = {(r["prompt_id"], r["rep"]) for r in run_eval.read_rows(results_path)}
    todo = [
        (cases[c], a, v, rep)
        for c in case_ids
        for a in attacks
        for v in vectors
        for rep in range(args.reps)
        if (f"{c}-{a}-{v}", rep) not in done
    ]
    print(f"{len(todo)} runs to do ({len(done)} done) on {args.model}, {args.workers} at a time")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, client, *job, out_dir, args.model): job for job in todo}
        for future in as_completed(futures):
            case, attack, vector, rep = futures[future]
            try:
                row = future.result()
            except Exception as e:  # infrastructure, not the model: recorded, never scored
                error = {
                    "prompt_id": f"{case['id']}-{attack}-{vector}",
                    "rep": rep,
                    "error": f"{type(e).__name__}: {e}",
                }
                run_eval.append_row(errors_path, error)
                print(f"  {error['prompt_id']} rep {rep}: ERROR {error['error']}")
                continue
            run_eval.append_row(results_path, row)
            print(
                f"  {row['prompt_id']} rep {rep}: exposed={int(row['exposed'])} followed={row['followed']} "
                f"flagged={row['flagged']} correct={row['grade'].get('correct')} "
                f"${row['cost_usd'] + row['judge_cost_usd']:.4f}"
            )

    summarize([r for r in run_eval.read_rows(results_path) if r["case"] in case_ids])


if __name__ == "__main__":
    main()
