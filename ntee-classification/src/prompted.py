"""Approach 2, a prompted frontier model: Claude classifies each mission with no training.

    python src/prompted.py --model haiku --prompt guided --estimate      # free: token count and cost estimate
    python src/prompted.py --model haiku --prompt guided --limit 300     # pilot on 300 dev missions
    python src/prompted.py --model sonnet --prompt fewshot               # all 1,813 dev missions
    python src/prompted.py --model sonnet --prompt fewshot --final       # also the held-out test split, once

Each mission is one request: the system prompt (src/prompts.py: names, guided or fewshot) and the mission;
the answer is forced into a schema of three ranked guesses, each one of the 25 group letters, so a reply
can't be malformed or invent a group. Thinking is off: this is quick labeling, not reasoning (Claude Sonnet
5.5's lowest setting is thinking={"type": "between_tools"}; Claude Haiku 4.5 doesn't think unless asked).
The system prompt is marked for caching; it only caches above the model's minimum (512 tokens on Sonnet
5.5, 4,096 on Haiku 4.5), which is why the long few-shot prompt can be cheaper per call than a short one.

Sonnet 5.5 requests enable the server-side fallback (fallbacks="default"), which retries certain safety
declines on Claude Sonnet 5; the model that actually answered is recorded. A refusal or empty answer
counts as a wrong prediction and is counted.

Answers are saved per mission as they arrive (results/claude/<approach>_<split>.jsonl), so an interrupted run
resumes and a pilot's answers are reused by the full run. Requests run 8 at a time.
"""

import argparse
import json
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

import anthropic
from pydantic import BaseModel

import data
import evaluate
import ntee
import prompts
from paths import ROOT

MODELS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5-5"}
# US dollars per million tokens (input, output); cache writes cost 1.25x input, cache reads 0.1x.
PRICES = {"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-sonnet-5": (2.00, 10.00)}
BATCH_DISCOUNT = 0.5  # the Message Batches API halves every token's price (not used here; reported for scale)
CACHE_DIR = ROOT / "results" / "claude"
MAX_TOKENS = 256  # three letters as JSON; thinking is off
FALLBACK_BETA = "server-side-fallback-2026-07-01"

Group = Literal[tuple(ntee.GROUPS)]


class Ranking(BaseModel):
    """The model's three most likely NTEE major groups, most likely first."""

    first: Group
    second: Group
    third: Group


def user_message(mission: str) -> str:
    return f"<mission>\n{mission}\n</mission>"


def request(model_key: str, system: str, mission: str) -> dict:
    """The request body (without the client call) for one mission."""
    body = {
        "model": MODELS[model_key],
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_message(mission)}],
    }
    if model_key == "sonnet":
        body |= {"thinking": {"type": "between_tools"}, "betas": [FALLBACK_BETA], "fallbacks": "default"}
    return body


def cost_usd(model: str, usage: dict) -> float:
    input_price, output_price = PRICES[model]
    return (
        usage.get("input_tokens", 0) * input_price
        + usage.get("cache_creation_input_tokens", 0) * input_price * 1.25
        + usage.get("cache_read_input_tokens", 0) * input_price * 0.10
        + usage.get("output_tokens", 0) * output_price
    ) / 1_000_000


def classify(client, model_key: str, system: str, mission: str) -> dict:
    """One mission: the ranked groups (empty on a refusal or unusable answer), the model that answered,
    stop reason, token usage, cost and seconds."""
    body = request(model_key, system, mission)
    parse = client.beta.messages.parse if model_key == "sonnet" else client.messages.parse
    start = time.perf_counter()
    response = parse(**body, output_format=Ranking)
    seconds = time.perf_counter() - start
    ranking = response.parsed_output if response.stop_reason != "refusal" else None
    ranked = list(dict.fromkeys([ranking.first, ranking.second, ranking.third])) if ranking else []
    usage = {
        k: getattr(response.usage, k, 0) or 0
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    }
    return {
        "ranked": ranked,
        "model": response.model,
        "stop_reason": response.stop_reason,
        "usage": usage,
        "cost_usd": cost_usd(response.model if response.model in PRICES else body["model"], usage),
        "seconds": round(seconds, 3),
    }


def subset(rows: list[dict], limit: int | None) -> list[dict]:
    """A fixed random sample of `limit` rows (all rows if limit is None), in a stable order."""
    if limit is None or limit >= len(rows):
        return rows
    return sorted(random.Random(7).sample(rows, limit), key=lambda r: r["ein"])


def run(client, model_key: str, variant: str, split: str, rows: list[dict], workers: int = 8) -> dict[str, dict]:
    """Classify every row not already saved; returns ein -> answer for all rows. Failed requests (after
    the SDK's retries) are reported and left for the next run."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"claude_{model_key}_{variant}_{split}.jsonl"
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                answer = json.loads(line)
                done[answer["ein"]] = answer
    todo = [r for r in rows if r["ein"] not in done]
    print(f"{len(todo)} to classify ({len(rows) - len(todo)} already saved), {workers} at a time")
    system, lock, failures = prompts.system_prompt(variant), threading.Lock(), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(classify, client, model_key, system, r["mission"]): r for r in todo}
        for i, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                answer = {"ein": row["ein"]} | future.result()
            except anthropic.APIError as e:
                failures += 1
                print(f"  {row['ein']}: {type(e).__name__}: {e}")
                continue
            with lock, path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(answer) + "\n")
            done[row["ein"]] = answer
            if i % 200 == 0:
                print(f"  {i}/{len(todo)}")
    if failures:
        print(f"{failures} requests failed; run again to retry them")
    return done


def estimate(client, model_key: str, variant: str, rows: list[dict]) -> None:
    """Price a full run from the free token-counting endpoint: the prompt's size, whether it can cache,
    and the expected cost of classifying `rows`."""
    system = prompts.system_prompt(variant)
    body = request(model_key, system, rows[0]["mission"])
    counted = client.messages.count_tokens(
        model=body["model"], system=system, messages=[{"role": "user", "content": user_message(rows[0]["mission"])}]
    ).input_tokens
    mission_tokens = statistics.median(len(r["mission"]) for r in rows) / 4  # about 4 characters per token
    minimum = 512 if model_key == "sonnet" else 4096
    caches = counted >= minimum
    per_call = {
        "input_tokens": 0 if caches else counted,
        "cache_read_input_tokens": counted if caches else 0,
        "output_tokens": 40,
    }
    total = cost_usd(body["model"], per_call) * len(rows)
    print(
        f"{variant} prompt on {body['model']}: {counted:,} tokens per request (with a typical mission ~"
        f"{mission_tokens:.0f}); {'caches' if caches else f'too short to cache (minimum {minimum:,})'}"
    )
    print(f"estimated cost for {len(rows):,} missions: ${total:.2f} (${total / len(rows) * 1000:.2f} per 1,000)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=sorted(MODELS), required=True)
    parser.add_argument("--prompt", choices=prompts.VARIANTS, required=True)
    parser.add_argument("--limit", type=int, help="a fixed random sample of dev missions (a pilot)")
    parser.add_argument("--estimate", action="store_true", help="count tokens and estimate cost only (free)")
    parser.add_argument("--final", action="store_true", help="also score the held-out test split (once)")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    client = anthropic.Anthropic(max_retries=4)
    dev = subset(data.load("dev"), args.limit)
    if args.estimate:
        estimate(client, args.model, args.prompt, dev)
        return
    splits = [("dev", dev)] + ([("test", data.load("test", final=True))] if args.final else [])
    approach = f"claude_{args.model}_{args.prompt}" + (f"_pilot{args.limit}" if args.limit else "")
    for split, rows in splits:
        answers = run(client, args.model, args.prompt, split, rows, args.workers)
        scored = [r for r in rows if r["ein"] in answers]
        if len(scored) < len(rows):
            print(f"{split}: {len(rows) - len(scored)} missions still unanswered; not scoring until all are done")
            continue
        results = [answers[r["ein"]] for r in scored]
        total_cost = sum(a["cost_usd"] for a in results)
        served = {m: sum(a["model"] == m for a in results) for m in {a["model"] for a in results}}
        details = {
            "model": MODELS[args.model],
            "served_by": served,
            "refusals": sum(a["stop_reason"] == "refusal" for a in results),
            "empty_answers": sum(not a["ranked"] for a in results),
            "total_cost_usd": round(total_cost, 4),
            "batch_cost_per_1k_usd": round(total_cost / len(results) * 1000 * BATCH_DISCOUNT, 4),
            "cache_read_share": round(
                sum(a["usage"]["cache_read_input_tokens"] for a in results)
                / max(1, sum(sum(a["usage"][k] for k in a["usage"] if "input" in k) for a in results)),
                3,
            ),
            "p90_latency_ms": round(sorted(a["seconds"] for a in results)[len(results) * 9 // 10] * 1000, 1),
        }
        metrics = evaluate.record(
            approach,
            split,
            scored,
            [a["ranked"] for a in results],
            latency_ms=statistics.median(a["seconds"] for a in results) * 1000,
            cost_per_1k_usd=total_cost / len(results) * 1000,
            notes=f"{MODELS[args.model]} {args.prompt}",
            details=details,
        )
        print(f"{split}: {evaluate.summary(metrics)}")
        print(
            f"  cost ${total_cost:.3f} (${total_cost / len(results) * 1000:.2f} per 1,000; "
            f"${details['batch_cost_per_1k_usd']:.2f} with batches) | median "
            f"{statistics.median(a['seconds'] for a in results) * 1000:.0f} ms | served by {served} | "
            f"refusals {details['refusals']} | cache reads {details['cache_read_share']:.0%} of input"
        )


if __name__ == "__main__":
    main()
