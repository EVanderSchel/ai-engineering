"""Writes notebooks/vllm_t4.ipynb (run: uv run python notebooks/make_vllm_notebook.py).

Step 6 stage 4: the GPU comparison point. The same chat model as stage 2 (Qwen2.5-0.5B-Instruct, at fp16, the
precision llama.cpp's F16 file holds) served by vLLM on a Colab T4, measured with the same code and requests as
stage 2 (src/measure_quantization.py, fetched from the repo's main branch), plus higher concurrency levels, where a
GPU's batching should pull ahead. The notebook downloads one JSON file for results/quantization/.

Generated from this file so its code lives in reviewable Python strings and can be syntax-checked by the tests
(tests/test_export_notebook.py). Edit here, then regenerate.
"""

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

CELLS = [
    (
        "markdown",
        """\
# Step 6, stage 4: Qwen2.5-0.5B-Instruct on vLLM, Colab T4 GPU

Stage 2 measured this model on the PC's CPU with llama.cpp. This notebook serves it with **vLLM** (a GPU inference
server built around continuous batching and PagedAttention, which packs the attention cache in small pages so many
requests fit at once) on a T4, and runs the same measurement code against it: speed at 1 to 64 requests at once,
fidelity, ARC-Easy and NTEE accuracy. Free on Colab's T4.

**Run:** Runtime > Change runtime type > **T4 GPU**, then Runtime > Run all. About 15 minutes, most of it installing
vLLM. The last cell downloads `vllm-t4.json`: put it in `ai-gateway/results/quantization/`.""",
    ),
    (
        "code",
        """\
# 1. Settings.
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"  # the official fp16 weights (the GGUF files in stage 2 come from the same model)
NAME = "qwen2.5-0.5b-instruct-fp16-vllm"  # the name vLLM serves it under, and the results' label
PORT = 8000
LEVELS = (1, 4, 8, 16, 32, 64)   # stage 2's levels plus two more, where batching on a GPU should pay off
GPU_USD_PER_HOUR = 0.35          # an on-demand T4's price (the figure ntee-classification used)
REPO = "https://raw.githubusercontent.com/EVanderSchel/ai-engineering/main/ai-gateway\"""",
    ),
    (
        "code",
        """\
# 2. The GPU, and vLLM (a large install: a few minutes). httpx2 is what the measurement code speaks HTTP with.
!nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
!pip install -q vllm httpx2
import importlib.metadata as md
VERSIONS = {p: md.version(p) for p in ("vllm", "torch", "transformers", "httpx2")}
print(VERSIONS)""",
    ),
    (
        "code",
        """\
# 3. Stage 2's measurement code and data, from the repo's main branch, laid out as in the repo.
import pathlib, sys, urllib.request
ROOT = pathlib.Path("/content/ai-gateway")
for path in ("src/paths.py", "src/measure_quantization.py",
             "data/self_hosted/arc_easy_sample.json", "data/self_hosted/ntee_dev_sample.json"):
    (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(f"{REPO}/{path}", ROOT / path)
sys.path.insert(0, str(ROOT / "src"))
import measure_quantization as mq
mq.LEVELS = LEVELS
print("fetched; output tokens per speed request:", mq.OUTPUT_TOKENS)""",
    ),
    (
        "code",
        """\
# 4. Start vLLM's OpenAI-compatible server in the background. fp16 ("half"): T4s have no bfloat16. The same
#    2,048-token context as llama-server's slots; up to 64 requests batched at once; prefix caching off, as stage 2
#    measured with prompt caching off. Ready when /health answers 200.
#    --generation-config vllm: without it vLLM takes default sampling settings from the model's
#    generation_config.json, which for Qwen2.5-Instruct include a repetition penalty. Requests here set only
#    temperature, so the penalty stayed on (it punishes words already in the prompt: "the sky appears blue",
#    category names instead of numbers). llama-server has no penalty by default; this makes the two match.
import subprocess, time, httpx2
COMMAND = ["vllm", "serve", MODEL, "--served-model-name", NAME, "--dtype", "half", "--max-model-len", "2048",
           "--max-num-seqs", "64", "--no-enable-prefix-caching", "--generation-config", "vllm", "--port", str(PORT)]
log = open("/content/vllm.log", "w")
server = subprocess.Popen(COMMAND, stdout=log, stderr=subprocess.STDOUT)
started = time.perf_counter()
while True:
    try:
        if httpx2.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
            break
    except httpx2.HTTPError:
        pass
    if server.poll() is not None or time.perf_counter() - started > 900:
        print(open("/content/vllm.log").read()[-4000:])
        raise RuntimeError("vLLM didn't start: the end of its log is above")
    time.sleep(1)
LOAD_SECONDS = round(time.perf_counter() - started, 1)
print(f"ready in {LOAD_SECONDS} s")""",
    ),
    (
        "code",
        """\
# 5. The measurement: stage 2's functions, unchanged, pointed at vLLM. Speed at every level, then fidelity (16
#    greedy explanations, twice), then 400 ARC-Easy questions and 400 NTEE missions (4 at once, as on the CPU).
mq.URL = f"http://127.0.0.1:{PORT}"
mq.RETRIES.clear()

async def measure():
    quiz, ntee = mq.load_data()
    async with httpx2.AsyncClient(base_url=mq.URL, timeout=300) as client:
        print("speed")
        speed = await mq.speed(client, NAME)
        print("fidelity")
        prompts = [f"Write a short explanation of {topic} for a curious teenager." for topic in mq.TOPICS]
        passes = [[(await mq.ask(client, NAME, p, mq.OUTPUT_TOKENS))[0] for p in prompts] for _ in range(2)]
        print("ARC-Easy and NTEE")
        quiz_run = await mq.answer_all(client, NAME, {r["id"]: mq.quiz_prompt(r) for r in quiz["rows"]}, 3)
        ntee_run = await mq.answer_all(
            client, NAME, {r["ein"]: mq.broad_prompt(r["mission"], ntee["broad"]) for r in ntee["rows"]}, 3)
    return speed, passes, quiz_run, ntee_run

speed, passes, quiz_run, ntee_run = await measure()""",
    ),
    (
        "code",
        """\
# 6. GPU memory (vLLM reserves most of the card up front for its attention cache, so this is what it holds, not
#    what the model strictly needs), then the results file, in the same shape as stage 2's per-model files.
import json
from google.colab import files
gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
                     capture_output=True, text=True).stdout.strip().split(", ")
result = {
    "model": NAME, "engine": f"vLLM {VERSIONS['vllm']}", "hardware": f"{gpu[0]} (Colab)",
    "usd_per_hour": GPU_USD_PER_HOUR, "file_mb": 988, "load_seconds": LOAD_SECONDS,
    "peak_memory_mb": int(gpu[1]), "gpu_memory_total_mb": int(gpu[2]),
    "speed": speed, "fidelity_answers": passes, "quiz": quiz_run, "ntee": ntee_run,
    "transport_retries": list(mq.RETRIES), "versions": VERSIONS, "command": " ".join(COMMAND[1:]),
    # vLLM's own log lines about default sampling settings, if any: there should be none with the flag above.
    "sampling_log": [line.strip() for line in open("/content/vllm.log") if "sampling param" in line.lower()],
}
pathlib.Path("/content/vllm-t4.json").write_text(json.dumps(result, indent=1) + "\\n")
print({level: s["tokens_per_s_total"] for level, s in speed.items()}, "tokens/s in total")
files.download("/content/vllm-t4.json")
server.terminate()""",
    ),
]


def notebook() -> dict:
    cells = []
    for kind, source in CELLS:
        cell = {"cell_type": kind, "metadata": {}, "source": source.splitlines(keepends=True)}
        if kind == "code":
            cell |= {"execution_count": None, "outputs": []}
        cells.append(cell)
    return {
        "cells": cells,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"provenance": [], "gpuType": "T4"},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }


if __name__ == "__main__":
    path = HERE / "vllm_t4.ipynb"
    path.write_text(json.dumps(notebook(), indent=1) + "\n", encoding="utf-8")
    print("wrote", path)
