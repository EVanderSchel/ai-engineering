"""Run the injection judge (src/injection.py) on one split of the labelled cases, and save its answers. Paid: one
Claude Haiku 5.5 call per case (about $0.0001 each). Needs ANTHROPIC_API_KEY.

    uv run python src/judge_cases.py --split dev          # rerun as often as the prompt is being tuned
    uv run python src/judge_cases.py --split held_out     # once, at the end: refuses to run a second time

Answers go to results/injection-judge-<split>.json with the prompt version, model and cost; measure_guardrails.py
reads them to score the judge next to the pattern screen. The held-out split is scored once so that the prompt is
never tuned on it: the numbers it gives are the honest ones.
"""

import argparse
import asyncio
import json
import sys
from decimal import Decimal

import anthropic

import injection
from paths import RESULTS_DIR
from providers import Usage

# USD per million tokens for claude-haiku-5-5 on the Claude API (prompts up to 100K tokens).
PRICE = {
    "input": Decimal("0.10"),
    "output": Decimal("0.50"),
    "cache_read": Decimal("0.01"),
    "cache_write": Decimal("0.125"),
}


def cost(usage: Usage) -> Decimal:
    return (
        usage.input_tokens * PRICE["input"]
        + usage.output_tokens * PRICE["output"]
        + usage.cache_read_tokens * PRICE["cache_read"]
        + usage.cache_write_tokens * PRICE["cache_write"]
    ) / Decimal(1_000_000)


def results_path(split: str):
    return RESULTS_DIR / f"injection-judge-{split}.json"


async def run(split: str, concurrency: int = 4) -> dict:
    import measure_guardrails  # the cases loader

    cases = [c for c in measure_guardrails.injection_cases() if c["split"] == split]
    judge = injection.Judge(anthropic.AsyncAnthropic(max_retries=2))
    limit = asyncio.Semaphore(concurrency)

    async def one(case):
        async with limit:
            result = await judge.judge(case["text"])
            return {
                "id": case["id"],
                "injection": result.injection,
                "reason": result.reason,
                "seconds": round(result.seconds, 2),
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cost_usd": str(cost(result.usage)),
            }

    answers = await asyncio.gather(*(one(c) for c in cases))
    return {
        "split": split,
        "model": judge.model,
        "effort": judge.effort,
        "prompt_version": injection.JUDGE_PROMPT_VERSION,
        "total_cost_usd": str(sum(Decimal(a["cost_usd"]) for a in answers)),
        "answers": answers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--split", choices=["dev", "held_out"], required=True)
    args = parser.parse_args()
    path = results_path(args.split)
    if args.split == "held_out" and path.exists():
        sys.exit(f"{path.name} exists: the held-out split is judged once. Delete it only to start a new experiment.")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    result = asyncio.run(run(args.split), loop_factory=loop_factory)
    RESULTS_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    flagged = sum(a["injection"] is True for a in result["answers"])
    print(
        f"{args.split}: {len(result['answers'])} judged, {flagged} flagged, ${result['total_cost_usd']}, wrote {path}"
    )


if __name__ == "__main__":
    main()
