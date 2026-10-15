"""Approach 3, a small open model fine-tuned with LoRA: score the predictions the Colab notebook produced.

    python src/finetuned.py results/finetuned/lora_qwen2.5-0.5b_dev.json

The notebook (notebooks/finetune_lora.ipynb) trains on Colab's GPU and writes a predictions file: a ranked list
of the 25 groups per mission, plus training time and speed. This script checks it covers exactly the split's
missions and records it with the shared scorer (src/evaluate.py), like the baseline and Claude.

Cost per 1,000 predictions is GPU time at an on-demand price (the notebook's gpu_usd_per_hour, $0.35 for a T4):
batched inference time per mission x 1,000 / 3,600 x the hourly price. Training cost is reported separately,
since it's paid once.
"""

import argparse
import json
import pathlib

import data
import evaluate


def load_predictions(path: pathlib.Path) -> dict:
    run = json.loads(path.read_text(encoding="utf-8"))
    missing = {
        "approach",
        "split",
        "predictions",
        "latency_ms",
        "batch_ms_per_mission",
        "gpu_usd_per_hour",
    } - run.keys()
    if missing:
        raise ValueError(f"{path.name} is missing {sorted(missing)}: was it written by notebooks/finetune_lora.ipynb?")
    return run


def cost_per_1k(batch_ms_per_mission: float, gpu_usd_per_hour: float) -> float:
    """GPU time for 1,000 batched predictions, priced by the hour."""
    return batch_ms_per_mission * 1000 / 1000 / 3600 * gpu_usd_per_hour


def score_file(path: pathlib.Path) -> dict:
    run = load_predictions(path)
    rows = data.load(run["split"], final=run["split"] == "test")
    ranked_by_ein = {p["ein"]: p["ranked"] for p in run["predictions"]}
    if set(ranked_by_ein) != {r["ein"] for r in rows}:
        raise ValueError(f"{path.name} doesn't cover exactly the {run['split']} split's {len(rows):,} missions")
    train_minutes = run.get("train_minutes", 0)
    details = {
        "model": run.get("model"),
        "settings": run.get("settings", {}),
        "trainable_parameters": run.get("trainable_parameters"),
        "gpu": run.get("gpu"),
        "train_minutes": train_minutes,
        "train_cost_usd": round(train_minutes / 60 * run["gpu_usd_per_hour"], 4),
        "batch_ms_per_mission": run["batch_ms_per_mission"],
        "versions": run.get("versions", {}),
    }
    return evaluate.record(
        run["approach"],
        run["split"],
        rows,
        [ranked_by_ein[r["ein"]] for r in rows],
        latency_ms=run["latency_ms"],
        cost_per_1k_usd=cost_per_1k(run["batch_ms_per_mission"], run["gpu_usd_per_hour"]),
        notes=f"{run.get('model')} LoRA r={run.get('settings', {}).get('lora_rank')} "
        f"train_fraction={run.get('settings', {}).get('train_fraction')}",
        details=details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=pathlib.Path)
    args = parser.parse_args()
    for path in args.files:
        run = load_predictions(path)
        metrics = score_file(path)
        print(f"{run['approach']} ({run['split']}): {evaluate.summary(metrics)}")
        print(
            f"  {run['latency_ms']:.1f} ms for one mission, {run['batch_ms_per_mission']:.2f} ms per mission in "
            f"batches | ${cost_per_1k(run['batch_ms_per_mission'], run['gpu_usd_per_hour']):.4f} per 1,000 | "
            f"trained in {run.get('train_minutes', 0):.1f} min on {run.get('gpu')}"
        )


if __name__ == "__main__":
    main()
