"""The NTEE classifier on ONNX Runtime, fp32 vs. int8, on this PC's CPU (step 6 stage 3). Free: everything is local.

    uv run python src/measure_classifier.py                   # both; each in its own process, for a clean memory peak
    uv run python src/measure_classifier.py --report-only     # rebuild results/classifier.md from the JSON

The models come from notebooks/export_ntee_onnx.ipynb (Colab), downloaded into models/ntee/ (gitignored):
fp32/model.onnx (+ model.onnx.data), int8/model.onnx, tokenizer.json, labels.json and export_info.json.

For each precision, in a fresh process with the same ONNX Runtime settings (4 threads, one per physical core):

1. **Check**: the first 64 dev missions must get the top group PyTorch gave them in Colab (export_info.json), which
   confirms this PC's tokenizer and runtime match the export.
2. **Accuracy** on ntee-classification's whole dev split (1,813 missions): top group, top 3, broad category, and how
   often it agrees with the fine-tuned model as that project ran it (PyTorch on a T4) and, for int8, with fp32.
3. **Speed**: every mission one at a time (latency p50/p95, missions per second; this pass also gives the
   accuracy), and the first 256 in batches of 32 (padded to the longest in each batch).
4. **Load and memory**: seconds to open the model; the process's peak memory (before and after loading).

Cost per 1,000 missions uses the same hourly cloud CPU price as the quantization measurement (CLOUD_PRICE).
"""

import argparse
import json
import statistics
import subprocess
import sys
import time

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from measure_quantization import CLOUD_PRICE, cost_per_1k, percentile
from paths import MODELS_DIR, RESULTS_DIR, ROOT

NTEE_DIR = MODELS_DIR / "ntee"
# The Colab export (fp32, int8) and the versions src/quantize_classifier.py makes from fp32.
PRECISIONS = (
    "fp32", "int8", "int8-matmul-uint8-down-fp32", "int8-weightonly8", "int8-weightonly4-gather",
    "int8-weightonly8-level4", "int8-weightonly4-gather-level4",
)  # fmt: skip
DATA = ROOT / "data" / "self_hosted" / "ntee_dev.json"
OUT_DIR = RESULTS_DIR / "classifier"
THREADS = 4  # one per physical core, as llama-server uses
BATCH = 32
BATCHED_MISSIONS = 256  # batching is slower on this CPU, so it is timed on a sample


# --- pure helpers (tested) ---


def make_tokenizer(path, info: dict) -> Tokenizer:
    """The exported tokenizer with training's settings: truncate at max_tokens, pad on the same side with the same
    token (the model reads the last token that isn't padding, so the side matters)."""
    tokenizer = Tokenizer.from_file(str(path))
    tokenizer.enable_truncation(max_length=info["max_tokens"])
    tokenizer.enable_padding(pad_id=info["pad_token_id"], pad_token=info["pad_token"], direction=info["padding_side"])
    return tokenizer


def encode(tokenizer: Tokenizer, missions: list[str]) -> dict[str, np.ndarray]:
    encodings = tokenizer.encode_batch(missions)
    return {
        "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
        "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
    }


def ranked_groups(logits: np.ndarray, labels: list[str], k: int = 3) -> list[list[str]]:
    return [[labels[i] for i in row[:k]] for row in np.argsort(-logits, axis=-1)]


def score(ranked: dict[str, list[str]], rows: list[dict], broad: dict, compare_to: dict[str, str] | None) -> dict:
    """Accuracy over `rows` (top group, top 3, broad category), and agreement of the top group with `compare_to`."""
    group_broad = {g: number for number, b in broad.items() for g in b["groups"]}
    n = len(rows)
    out = {
        "rows": n,
        "accuracy": round(sum(ranked[r["ein"]][0] == r["group"] for r in rows) / n, 4),
        "top3": round(sum(r["group"] in ranked[r["ein"]][:3] for r in rows) / n, 4),
        "broad": round(sum(group_broad[ranked[r["ein"]][0]] == group_broad[r["group"]] for r in rows) / n, 4),
    }
    if compare_to is not None:
        out["agreement"] = round(sum(ranked[r["ein"]][0] == compare_to[r["ein"]] for r in rows) / n, 4)
    return out


def peak_memory_mb() -> int:
    """This process's peak memory so far (Windows: peak working set; elsewhere: max resident set size)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                             "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                             "PeakPagefileUsage")
            ]  # fmt: skip

        counters = Counters(cb=ctypes.sizeof(Counters))
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return round(counters.PeakWorkingSetSize / 2**20)
    import resource

    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


# --- one precision (run in its own process) ---


def load_inputs() -> tuple[dict, dict, list[str]]:
    info = json.loads((NTEE_DIR / "export_info.json").read_text(encoding="utf-8"))
    labels = json.loads((NTEE_DIR / "labels.json").read_text(encoding="utf-8"))
    return info, json.loads(DATA.read_text(encoding="utf-8")), labels


def measure_one(precision: str) -> dict:
    info, data, labels = load_inputs()
    tokenizer = make_tokenizer(NTEE_DIR / "tokenizer.json", info)
    rows = data["rows"]
    missions = [r["mission"] for r in rows]
    memory_before = peak_memory_mb()

    options = ort.SessionOptions()
    options.intra_op_num_threads = THREADS
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    started = time.perf_counter()
    path = NTEE_DIR / precision / "model.onnx"
    session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
    load_seconds = round(time.perf_counter() - started, 2)
    run = lambda texts: session.run(None, encode(tokenizer, texts))[0]  # noqa: E731

    check_rows = info["check_rows"]
    check_ranked = ranked_groups(run(missions[: len(check_rows)]), labels)
    check = sum(ranked[0] == c["top1"] for ranked, c in zip(check_ranked, check_rows, strict=True))
    print(f"{precision}: loaded in {load_seconds} s; check rows {check} of {len(check_rows)} match Colab")

    run(missions[:BATCH])  # warm up
    started = time.perf_counter()
    for i in range(0, BATCHED_MISSIONS, BATCH):
        run(missions[i : i + BATCH])
    batch_seconds = time.perf_counter() - started

    single, logits = [], []
    for mission in missions:
        started = time.perf_counter()
        logits.append(run([mission])[0])
        single.append(time.perf_counter() - started)
    logits = np.stack(logits)

    ranked = dict(zip((r["ein"] for r in rows), ranked_groups(logits, labels), strict=True))
    return {
        "precision": precision,
        "file_mb": round(sum(p.stat().st_size for p in (NTEE_DIR / precision).iterdir()) / 1e6, 1),
        "load_seconds": load_seconds,
        "memory_before_load_mb": memory_before,
        "peak_memory_mb": peak_memory_mb(),
        "check": {"matching": check, "rows": len(check_rows)},
        "single": {
            "missions": len(missions),
            "latency_p50_ms": round(1000 * statistics.median(single), 1),
            "latency_p95_ms": round(1000 * percentile(single, 0.95), 1),
            "missions_per_s": round(len(missions) / sum(single), 1),
        },
        "batched": {
            "batch_size": BATCH,
            "missions": BATCHED_MISSIONS,
            "missions_per_s": round(BATCHED_MISSIONS / batch_seconds, 1),
        },
        "versions": {"onnxruntime": ort.__version__, "threads": THREADS},
        "ranked": ranked,
    }


# --- the report ---


def combine() -> dict:
    _, data, _ = load_inputs()
    rows, broad = data["rows"], data["broad"]
    runs = {p: json.loads((OUT_DIR / f"{p}.json").read_text(encoding="utf-8")) for p in PRECISIONS
            if (OUT_DIR / f"{p}.json").exists()}  # fmt: skip
    lora = {r["ein"]: r["lora_top1"] for r in rows}
    fp32_top1 = {e: r[0] for e, r in runs["fp32"]["ranked"].items()} if "fp32" in runs else None
    models = {}
    for precision, run in runs.items():
        models[precision] = {
            **{k: run[k] for k in ("file_mb", "load_seconds", "memory_before_load_mb", "peak_memory_mb", "check",
                                   "single", "batched", "versions")},
            "quality": score(run["ranked"], rows, broad, None),
            "same_as_colab": score(run["ranked"], rows, broad, lora)["agreement"],
            "same_as_fp32": score(run["ranked"], rows, broad, fp32_top1)["agreement"] if fp32_top1 else None,
            "cost_per_1k_usd": {
                "batched": cost_per_1k(run["batched"]["missions_per_s"]),
                "one_at_a_time": cost_per_1k(run["single"]["missions_per_s"]),
            },
        }  # fmt: skip
    return {"cloud_price": CLOUD_PRICE, "reference": data["reference"], "models": models}


def report(result: dict) -> str:
    models = result["models"]
    lines = [
        "# The NTEE classifier on ONNX Runtime: fp32 vs. int8, CPU",
        "",
        "Generated by `src/measure_classifier.py` on this PC (Intel i7-6700, 4 cores, Windows, no Docker), ONNX "
        f"Runtime {next(iter(models.values()))['versions']['onnxruntime']} with {THREADS} threads, each precision "
        "in its own process. The model is ntee-classification's LoRA fine-tune of Qwen2.5-0.5B, merged and "
        "exported in Colab (notebooks/export_ntee_onnx.ipynb). Accuracy is on that project's whole dev split "
        "(1,813 missions); its test split isn't used.",
        "",
        "| | " + " | ".join(models) + " |",
        "|---|" + "---|" * len(models),
    ]

    def row(label, cell):
        lines.append(f"| {label} | " + " | ".join(cell(m) for m in models.values()) + " |")

    row("File", lambda m: f"{m['file_mb']:,} MB")
    row("Peak memory (process)", lambda m: f"{m['peak_memory_mb']:,} MB")
    row("Opens in", lambda m: f"{m['load_seconds']} s")
    row("Check rows matching Colab", lambda m: f"{m['check']['matching']} of {m['check']['rows']}")
    row("One at a time, p50 / p95", lambda m: f"{m['single']['latency_p50_ms']} / {m['single']['latency_p95_ms']} ms")
    row("One at a time, throughput", lambda m: f"{m['single']['missions_per_s']} missions/s")
    row(f"Batches of {BATCH} (first {BATCHED_MISSIONS})", lambda m: f"{m['batched']['missions_per_s']} missions/s")
    row("Accuracy (25 groups)", lambda m: f"{m['quality']['accuracy']:.1%}")
    row("Top 3", lambda m: f"{m['quality']['top3']:.1%}")
    row("Broad category", lambda m: f"{m['quality']['broad']:.1%}")
    row("Same top group as Colab (PyTorch, T4)", lambda m: f"{m['same_as_colab']:.1%}")
    row("Same top group as fp32", lambda m: "(itself)" if m["same_as_fp32"] == 1 else f"{m['same_as_fp32']:.1%}")
    price = result["cloud_price"]
    row(f"Cost per 1,000, batched (${price['usd_per_hour']}/h)", lambda m: f"${m['cost_per_1k_usd']['batched']}")
    row("Cost per 1,000, one at a time", lambda m: f"${m['cost_per_1k_usd']['one_at_a_time']}")
    lines += ["", "The same rows, from ntee-classification's own runs:", ""]
    for name, ref in result["reference"].items():
        if isinstance(ref, dict):
            cost = f", ${ref['cost_per_1k_usd']:.4f} per 1,000" if ref.get("cost_per_1k_usd") is not None else ""
            lines.append(
                f"- {name}: {ref['accuracy']:.1%} (top 3 {ref['top3']:.1%}, broad {ref['broad']:.1%}), "
                f"{ref['latency_ms']:,.0f} ms one at a time{cost}"
            )
    lines += ["", f"Costs priced at {price['instance']}, ${price['usd_per_hour']} an hour, at the measured speed."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--one", choices=PRECISIONS, help=argparse.SUPPRESS)  # the per-precision child process
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--precisions", default=",".join(PRECISIONS), help="measure only these")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.one:
        result = measure_one(args.one)
        (OUT_DIR / f"{args.one}.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        return
    if not args.report_only:
        for precision in args.precisions.split(","):
            subprocess.run([sys.executable, __file__, "--one", precision], check=True)
    result = combine()
    (RESULTS_DIR / "classifier.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "classifier.md").write_text(report(result), encoding="utf-8")
    print(report(result))


if __name__ == "__main__":
    main()
