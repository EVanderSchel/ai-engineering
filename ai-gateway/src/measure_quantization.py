"""What each quantization of Qwen2.5-0.5B-Instruct gains and gives up, on this PC's CPU. Free: everything is local.

    uv run python src/measure_quantization.py                 # all three files; restarts the llm service for each
    uv run python src/measure_quantization.py --models q8_0   # one (the others' results are kept)
    uv run python src/measure_quantization.py --report-only   # rebuild results/quantization.md from the JSON

Each model file (Q4_K_M, Q8_0, F16) runs alone on the same llama-server settings (docker-compose.yml: 4 slots,
4 threads), measured against llama-server directly (the gateway adds about a millisecond: step 5 stage 4):

1. **Load and memory**: seconds from starting the container until /health answers 200; llama-server's peak
   resident memory over the whole run (`peak_rss_mb`).
2. **Speed under load**: streamed requests at 1, 4, 8 and 16 at once, each with its own prompt and prompt caching
   off (so no request is answered from another's work), each generating exactly 128 tokens (`ignore_eos`). Time to
   first token, generation speed per request, whole-request latency, and the server's total tokens and requests
   per second.
3. **Fidelity to F16** (the unquantized reference): 16 prompts answered greedily (temperature 0, one at a time,
   up to 128 tokens), twice. How many answers are word for word F16's, and how many words in before they differ.
   The second pass shows whether a model repeats itself exactly, so a difference from F16 isn't just noise.
4. **Accuracy**: 400 ARC-Easy science questions (4 choices, a standard small-model benchmark) and 400 mission
   statements from ntee-classification's dev split sorted into its 9 broad categories, temperature 0, 4 at once.
   Also how often each quantization picks the same answer as F16.

Cost per 1,000 requests uses an hourly price for comparable cloud hardware (CLOUD_PRICE below) and the measured
throughput, since a self-hosted model is paid for by the hour, not by the token.
"""

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter

import httpx2

from paths import RESULTS_DIR, ROOT

MODELS = {  # short name -> file name without .gguf (also the alias llama-server reports)
    "q4_k_m": "qwen2.5-0.5b-instruct-q4_k_m",
    "q8_0": "qwen2.5-0.5b-instruct-q8_0",
    "f16": "qwen2.5-0.5b-instruct-fp16",
}
FILE_MB = {"q4_k_m": 491, "q8_0": 676, "f16": 1270}  # Hugging Face's sizes
MEMORY_LIMIT = {"q4_k_m": "1g", "q8_0": "1g", "f16": "2g"}
REFERENCE = "f16"
URL = "http://127.0.0.1:8081"
LEVELS = (1, 4, 8, 16)
OUTPUT_TOKENS = 128
TASK_CONCURRENCY = 4
# An on-demand c7i.2xlarge in us-east-2 (8 vCPUs, i.e. 4 physical cores, like this PC's i7-6700), AWS Price List
# API, effective 2026-10-01. A newer CPU than this PC's, so the costs here are if anything on the high side.
CLOUD_PRICE = {"instance": "c7i.2xlarge (us-east-2, on demand)", "usd_per_hour": 0.357}
# Runs made elsewhere with this file's code, saved by hand into results/quantization/: stage 4's GPU comparison
# (notebooks/vllm_t4.ipynb). Each file carries its own engine, hardware and hourly price.
GPU_RUNS = ("vllm-t4",)
CPU_RUN = {"engine": "llama.cpp build 12033", "hardware": "i7-6700, 4 threads"}

DATA_DIR = ROOT / "data" / "self_hosted"
OUT_DIR = RESULTS_DIR / "quantization"
TOPICS = [
    "how bees make honey", "why the sky is blue", "how a bicycle stays upright", "the water cycle",
    "how vaccines work", "why leaves change color", "how bread rises", "what causes tides",
    "how a library is organized", "why cats purr", "how solar panels work", "what a food bank does",
    "how volcanoes form", "why we need sleep", "how a bill becomes law", "how compost is made",
]  # fmt: skip


def load_data() -> tuple[dict, dict]:
    read = lambda name: json.loads((DATA_DIR / name).read_text(encoding="utf-8"))  # noqa: E731
    return read("arc_easy_sample.json"), read("ntee_dev_sample.json")


# --- pure helpers (tested) ---


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[round(q * (len(ordered) - 1))]


def summarize_speed(requests: list[dict], wall_seconds: float) -> dict:
    """One concurrency level: each request's {ttft, total, tokens} and the level's wall-clock time."""
    ttft = [r["ttft"] for r in requests]
    total = [r["total"] for r in requests]
    per_request_tps = [(r["tokens"] - 1) / (r["total"] - r["ttft"]) for r in requests if r["total"] > r["ttft"]]
    return {
        "requests": len(requests),
        "ttft_p50_ms": round(1000 * statistics.median(ttft)),
        "ttft_p95_ms": round(1000 * percentile(ttft, 0.95)),
        "latency_p50_ms": round(1000 * statistics.median(total)),
        "latency_p95_ms": round(1000 * percentile(total, 0.95)),
        "tokens_per_s_per_request": round(statistics.median(per_request_tps), 1),
        "tokens_per_s_total": round(sum(r["tokens"] for r in requests) / wall_seconds, 1),
        "requests_per_s": round(len(requests) / wall_seconds, 3),
    }


def quiz_prompt(row: dict) -> str:
    choices = "\n".join(f"{letter}. {text}" for letter, text in zip("ABCD", row["choices"], strict=True))
    return f"Question: {row['question']}\n{choices}\n\nAnswer with the letter of the correct choice only."


def broad_prompt(mission: str, broad: dict) -> str:
    categories = "\n".join(f"{number}: {b['name']}" for number, b in broad.items())
    return (
        f'Mission statement of a US nonprofit:\n"{mission}"\n\n'
        f"Which category fits it best?\n{categories}\n\nAnswer with the number only."
    )


def parse_label(answer: str, labels: str) -> str | None:
    """The label a short answer starts with ("B", "B.", "**B**", "3: Health"), if it's one of `labels`."""
    match = re.match(r"\W*(\w)\b", answer.strip())
    return match.group(1) if match and match.group(1) in labels else None


def broad_of(group: str, broad: dict) -> str:
    return next(number for number, b in broad.items() if group in b["groups"])


def score(predictions: dict[str, str | None], gold: dict[str, str], reference: dict | None) -> dict:
    out = {
        "rows": len(gold),
        "accuracy": round(sum(predictions.get(k) == g for k, g in gold.items()) / len(gold), 4),
        "unparseable": sum(predictions.get(k) is None for k in gold),
    }
    if reference is not None:
        out["same_as_reference"] = round(sum(predictions.get(k) == reference.get(k) for k in gold) / len(gold), 4)
    return out


def words_in_common(a: str, b: str) -> int:
    """How many words two answers share from the start before they first differ."""
    count = 0
    for x, y in zip(a.split(), b.split(), strict=False):
        if x != y:
            break
        count += 1
    return count


def fidelity(texts: list[str], reference: list[str], repeat: list[str]) -> dict:
    return {
        "prompts": len(texts),
        "identical_to_reference": sum(t == r for t, r in zip(texts, reference, strict=True)),
        "words_in_common_mean": round(
            statistics.mean(words_in_common(t, r) for t, r in zip(texts, reference, strict=True)), 1
        ),
        "reference_words_mean": round(statistics.mean(len(r.split()) for r in reference), 1),
        "repeats_itself": sum(t == r for t, r in zip(texts, repeat, strict=True)),
    }


def cost_per_1k(requests_per_s: float, usd_per_hour: float = CLOUD_PRICE["usd_per_hour"]) -> float:
    return round(usd_per_hour / (requests_per_s * 3600) * 1000, 4)


# --- the server ---


def start_server(short: str) -> float:
    """(Re)start the llm service on one model file; seconds until it can serve."""
    env = {**os.environ, "LOCAL_MODEL": MODELS[short], "LOCAL_MEM": MEMORY_LIMIT[short]}
    started = time.perf_counter()
    subprocess.run(
        ["docker", "compose", "--profile", "local", "up", "-d", "--force-recreate", "llm"],
        cwd=ROOT, env=env, check=True, capture_output=True,
    )  # fmt: skip
    while True:
        try:
            if httpx2.get(f"{URL}/health", timeout=2).status_code == 200:
                return round(time.perf_counter() - started, 1)
        except httpx2.HTTPError:
            pass
        if time.perf_counter() - started > 180:
            raise RuntimeError(f"llama-server didn't become ready with {MODELS[short]}")
        time.sleep(0.25)


def peak_rss_mb(container: str = "ai-gateway-llm-1") -> int:
    """llama-server's peak resident memory since it started (VmHWM): its own buffers (context cache, compute) plus
    the pages of the model file it has read. Not `docker stats`, which also counts the file cache of reading the
    model and so reports up to the container's limit."""
    out = subprocess.run(
        ["docker", "exec", container, "grep", "VmHWM", "/proc/1/status"], capture_output=True, text=True, check=True
    ).stdout
    return round(int(re.search(r"(\d+) kB", out).group(1)) / 1024)


# --- the measurements ---


async def run_pool(items: list, concurrency: int, work) -> list:
    """`work` on every item, `concurrency` at a time; the results in the order they finished."""
    queue: asyncio.Queue = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)
    results: list = []

    async def worker():
        while not queue.empty():
            results.append(await work(queue.get_nowait()))

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return results


RETRIES: list[str] = []  # each request retried after its connection dropped (reported with the results)


async def with_retry(attempt):
    """One more try if the connection drops before an answer is complete: it happened about once in 150 requests on
    this PC and didn't reproduce on demand. The gateway does the same (a dropped connection is `unavailable`, which
    the router retries once). The request is timed again from the start."""
    try:
        return await attempt()
    except httpx2.TransportError as error:
        RETRIES.append(type(error).__name__)
        return await attempt()


async def one_stream(client: httpx2.AsyncClient, model: str, topic: str) -> dict:
    return await with_retry(lambda: _one_stream(client, model, topic))


async def _one_stream(client: httpx2.AsyncClient, model: str, topic: str) -> dict:
    body = {
        "model": model, "max_tokens": OUTPUT_TOKENS, "stream": True, "ignore_eos": True, "cache_prompt": False,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": f"Write a short explanation of {topic} for a curious teenager."}],
    }  # fmt: skip
    started, ttft, tokens = time.perf_counter(), None, 0
    async with client.stream("POST", "/v1/chat/completions", json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            chunk = json.loads(line[5:])
            if any((c.get("delta") or {}).get("content") for c in chunk.get("choices") or []) and ttft is None:
                ttft = time.perf_counter() - started
            if chunk.get("usage"):
                tokens = chunk["usage"]["completion_tokens"]
    return {"ttft": ttft, "total": time.perf_counter() - started, "tokens": tokens}


async def speed(client: httpx2.AsyncClient, model: str) -> dict:
    await one_stream(client, model, "warming up")
    out = {}
    for level in LEVELS:
        count = max(8, 2 * level)
        topics = [TOPICS[i % len(TOPICS)] + ("" if i < len(TOPICS) else f" (part {i // len(TOPICS) + 1})")
                  for i in range(count)]  # fmt: skip
        started = time.perf_counter()
        results = await run_pool(topics, level, lambda topic: one_stream(client, model, topic))
        out[str(level)] = summarize_speed(results, time.perf_counter() - started)
        print(f"  {level:>2} at once: {out[str(level)]}")
    return out


async def ask(client: httpx2.AsyncClient, model: str, prompt: str, max_tokens: int) -> tuple[str, float]:
    """One greedy answer, and how long it took."""
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0, "seed": 0,
            "messages": [{"role": "user", "content": prompt}]}  # fmt: skip

    async def attempt() -> tuple[str, float]:
        started = time.perf_counter()
        response = await client.post("/v1/chat/completions", json=body)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"] or "", time.perf_counter() - started

    return await with_retry(attempt)


async def answer_all(client: httpx2.AsyncClient, model: str, prompts: dict[str, str], max_tokens: int) -> dict:
    """Each prompt answered greedily, TASK_CONCURRENCY at a time: the answers by key, and the timing."""

    async def one(key: str) -> tuple[str, str, float]:
        return key, *await ask(client, model, prompts[key], max_tokens)

    started = time.perf_counter()
    results = await run_pool(list(prompts), TASK_CONCURRENCY, one)
    wall = time.perf_counter() - started
    seconds = [took for _, _, took in results]
    return {
        "answers": {key: answer for key, answer, _ in results},
        "latency_p50_ms": round(1000 * statistics.median(seconds)),
        "latency_p95_ms": round(1000 * percentile(seconds, 0.95)),
        "requests_per_s": round(len(prompts) / wall, 2),
    }


async def memory_only(short: str) -> int:
    """Re-measure peak memory alone: start the model, keep all 4 slots busy once, read the peak."""
    start_server(short)
    async with httpx2.AsyncClient(base_url=URL, timeout=300) as client:
        await run_pool(TOPICS[:8], 4, lambda topic: one_stream(client, MODELS[short], topic))
    return peak_rss_mb()


async def measure(short: str, quiz: dict, ntee: dict) -> dict:
    model = MODELS[short]
    RETRIES.clear()
    print(f"{model}: starting")
    load = start_server(short)
    async with httpx2.AsyncClient(base_url=URL, timeout=300) as client:
        print(f"  ready in {load} s; speed")
        speed_result = await speed(client, model)
        print("  fidelity (twice, one at a time)")
        prompts = [f"Write a short explanation of {topic} for a curious teenager." for topic in TOPICS]
        passes = [[(await ask(client, model, p, OUTPUT_TOKENS))[0] for p in prompts] for _ in range(2)]
        print("  ARC-Easy")
        quiz_run = await answer_all(client, model, {r["id"]: quiz_prompt(r) for r in quiz["rows"]}, 3)
        print("  NTEE broad categories")
        ntee_run = await answer_all(
            client, model, {r["ein"]: broad_prompt(r["mission"], ntee["broad"]) for r in ntee["rows"]}, 3
        )
    return {
        "model": model, "file_mb": FILE_MB[short], "load_seconds": load, "peak_memory_mb": peak_rss_mb(),
        "speed": speed_result, "fidelity_answers": passes, "quiz": quiz_run, "ntee": ntee_run,
        "transport_retries": list(RETRIES),
    }  # fmt: skip


# --- the report ---


def combine(quiz: dict, ntee: dict) -> dict:
    runs = {s: json.loads((OUT_DIR / f"{s}.json").read_text(encoding="utf-8")) for s in (*MODELS, *GPU_RUNS)
            if (OUT_DIR / f"{s}.json").exists()}  # fmt: skip
    quiz_gold = {r["id"]: r["answer"] for r in quiz["rows"]}
    ntee_gold = {r["ein"]: broad_of(r["group"], ntee["broad"]) for r in ntee["rows"]}
    parsed = {
        s: {
            "quiz": {k: parse_label(a, "ABCD") for k, a in run["quiz"]["answers"].items()},
            "ntee": {k: parse_label(a, "".join(ntee["broad"])) for k, a in run["ntee"]["answers"].items()},
        }
        for s, run in runs.items()
    }
    ref = parsed.get(REFERENCE)
    ref_texts = runs[REFERENCE]["fidelity_answers"][0] if REFERENCE in runs else None
    summary = {}
    for short, run in runs.items():
        is_ref = short == REFERENCE
        best_level, best = max(run["speed"].items(), key=lambda item: item[1]["requests_per_s"])
        first, second = run["fidelity_answers"]
        usd_per_hour = run.get("usd_per_hour", CLOUD_PRICE["usd_per_hour"])
        summary[short] = {
            "model": run["model"], "engine": run.get("engine", CPU_RUN["engine"]),
            "hardware": run.get("hardware", CPU_RUN["hardware"]), "usd_per_hour": usd_per_hour,
            "file_mb": run["file_mb"], "load_seconds": run["load_seconds"],
            "peak_memory_mb": run["peak_memory_mb"], "single": run["speed"]["1"], "best": best,
            "best_level": int(best_level),
            "transport_retries": len(run.get("transport_retries", [])),
            "fidelity": fidelity(first, ref_texts, second) if ref_texts else None,
            "quiz": score(parsed[short]["quiz"], quiz_gold, None if is_ref or not ref else ref["quiz"]),
            "ntee": score(parsed[short]["ntee"], ntee_gold, None if is_ref or not ref else ref["ntee"]),
            "task_timing": {k: {m: run[k][m] for m in ("latency_p50_ms", "latency_p95_ms", "requests_per_s")}
                            for k in ("quiz", "ntee")},
            "cost_per_1k_usd": {
                "generation_128_tokens": cost_per_1k(best["requests_per_s"], usd_per_hour),
                "classification": cost_per_1k(run["ntee"]["requests_per_s"], usd_per_hour),
            },
        }  # fmt: skip
    majority = Counter(ntee_gold.values()).most_common(1)[0]
    return {
        "cloud_price": CLOUD_PRICE,
        "ntee_majority": {
            "category": ntee["broad"][majority[0]]["name"],
            "share": round(majority[1] / len(ntee_gold), 4),
        },
        "ntee_reference": ntee["reference_on_these_rows"],
        "models": summary,
    }


def report(result: dict) -> str:
    models = result["models"]
    lines = [
        "# Quantization: Qwen2.5-0.5B-Instruct on llama-server (CPU) and vLLM (GPU)",
        "",
        "Generated by `src/measure_quantization.py`. The CPU rows ran on this PC (Intel i7-6700, 4 cores, Docker on "
        "WSL2), llama.cpp build 12033, 4 slots and 4 threads, each model file alone. The GPU row ran the same code "
        "against vLLM on a Colab T4 (notebooks/vllm_t4.ipynb), up to 64 requests batched at once. Free: no API calls.",
        "",
        "## Size, memory and one request at a time",
        "",
        "| Model | Engine, hardware | File | Peak memory | Ready in | First token | Generation | 128 tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in models.values():
        s = m["single"]
        lines.append(
            f"| {m['model']} | {m['engine']}, {m['hardware']} | {m['file_mb']} MB | {m['peak_memory_mb']:,} MB "
            f"| {m['load_seconds']} s | {s['ttft_p50_ms']} ms | {s['tokens_per_s_per_request']} tokens/s "
            f"| {s['latency_p50_ms'] / 1000:.1f} s |"
        )
    retried = ", ".join(f"{m['model']} {m['transport_retries']}" for m in models.values())
    lines += [
        "",
        "Peak memory is the server process's resident memory on the CPU, and GPU memory in use on the T4: vLLM "
        "reserves 90% of the card up front for its attention cache, so that figure is what it holds, not what the "
        "model needs. The F16 file and vLLM load the same fp16 weights.",
        "",
        f"Requests retried once after their connection dropped (see `with_retry`): {retried}.",
        "",
        f"## Under load (streams of {OUTPUT_TOKENS} tokens, prompt caching off)",
        "",
        "| Model | At once | First token p50 / p95 | Per request | Server total | Latency p50 / p95 |",
        "|---|---|---|---|---|---|",
    ]
    for short, m in models.items():
        run = json.loads((OUT_DIR / f"{short}.json").read_text(encoding="utf-8"))
        for level, s in run["speed"].items():
            lines.append(
                f"| {m['model']} | {level} | {s['ttft_p50_ms']} / {s['ttft_p95_ms']} ms "
                f"| {s['tokens_per_s_per_request']} tokens/s | {s['tokens_per_s_total']} tokens/s "
                f"| {s['latency_p50_ms'] / 1000:.1f} / {s['latency_p95_ms'] / 1000:.1f} s |"
            )
    lines += [
        "",
        "## Fidelity to F16: 16 explanations, greedy, up to 128 tokens",
        "",
        "| Model | Word for word F16's | Words before the first difference (mean) | Repeats itself exactly |",
        "|---|---|---|---|",
    ]
    for m in models.values():
        if f := m["fidelity"]:
            lines.append(
                f"| {m['model']} | {f['identical_to_reference']} of {f['prompts']} "
                f"| {f['words_in_common_mean']} of {f['reference_words_mean']} "
                f"| {f['repeats_itself']} of {f['prompts']} |"
            )
    lines += [
        "",
        "## Accuracy (400 each, temperature 0, 4 at once)",
        "",
        "| Model | ARC-Easy | Same as F16 | NTEE, 9 broad categories | Same as F16 | Unparseable (ARC / NTEE) |",
        "|---|---|---|---|---|---|",
    ]
    for m in models.values():
        q, n = m["quiz"], m["ntee"]
        same = lambda s: f"{s['same_as_reference']:.1%}" if "same_as_reference" in s else "(reference)"  # noqa: E731
        lines.append(
            f"| {m['model']} | {q['accuracy']:.1%} | {same(q)} | {n['accuracy']:.1%} | {same(n)} "
            f"| {q['unparseable']} / {n['unparseable']} |"
        )
    majority = result["ntee_majority"]
    lines += [
        "",
        f"ARC-Easy has 4 choices (guessing: 25%). For NTEE, always answering the commonest category "
        f"({majority['category']}) would score {majority['share']:.1%}. The same 400 NTEE rows, from "
        "ntee-classification's own runs (top group, and the broad category it falls in):",
        "",
    ]
    for name, ref in result["ntee_reference"].items():
        if isinstance(ref, dict):
            lines.append(f"- {name}: {ref['broad']:.1%} broad ({ref['groups']:.1%} of 25 groups)")
    price = result["cloud_price"]
    lines += [
        "",
        "## Cost per 1,000 requests",
        "",
        f"Priced by the hour at the measured throughput: the CPU rows at ${price['usd_per_hour']} "
        f"({price['instance']}), the GPU row at its run's own price (an on-demand T4, the figure "
        "ntee-classification used).",
        "",
        "| Model | Per hour | 128-token answers (best concurrency) | NTEE classifications (4 at once) |",
        "|---|---|---|---|",
    ]
    for m in models.values():
        c = m["cost_per_1k_usd"]
        lines.append(
            f"| {m['model']} | ${m['usd_per_hour']} | ${c['generation_128_tokens']} (at {m['best_level']} at once) "
            f"| ${c['classification']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", default=",".join(MODELS), help=f"comma-separated, from {', '.join(MODELS)}")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--memory-only", action="store_true", help="re-measure peak memory into the saved results")
    args = parser.parse_args()
    quiz, ntee = load_data()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    if args.memory_only:
        for short in args.models.split(","):
            path = OUT_DIR / f"{short}.json"
            run = json.loads(path.read_text(encoding="utf-8"))
            run["peak_memory_mb"] = asyncio.run(memory_only(short), loop_factory=loop_factory)
            path.write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
            print(short, run["peak_memory_mb"], "MB")
        start_server("q8_0")
    elif not args.report_only:
        for short in args.models.split(","):
            result = asyncio.run(measure(short, quiz, ntee), loop_factory=loop_factory)
            (OUT_DIR / f"{short}.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        start_server("q8_0")  # leave the server on the route's model
    result = combine(quiz, ntee)
    (RESULTS_DIR / "quantization.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "quantization.md").write_text(report(result), encoding="utf-8")
    print(report(result))


if __name__ == "__main__":
    main()
