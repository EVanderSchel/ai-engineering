"""Step 5's data-size curve: how many labeled missions does a trained model need to catch up with prompting?

    python src/data_size.py     # writes results/data_size_dev.md and results/data_size_dev.png

The fine-tuned points are the Colab notebook's runs with TRAIN_FRACTION 0.1, 0.25, 0.5 and 1.0 (Qwen2.5-0.5B,
scored by src/finetuned.py). The baseline is retrained here, in seconds, on stratified subsamples of the same
sizes with step 2's chosen settings (word+char features, C=1, balanced). Claude needs no training missions:
its runs are drawn as flat lines. Few-shot Haiku does see 100 examples in its prompt.

Both trained models keep the same number of epochs at every size, so a smaller training set also means fewer
weight updates: part of the drop at 10% is under-training, not only too few examples.
"""

import json
import random

import matplotlib

matplotlib.use("Agg")  # files only, no window
import matplotlib.pyplot as plt

import baseline
import data
import evaluate
from paths import ROOT

RESULTS_DIR = ROOT / "results"
FRACTIONS = (0.1, 0.25, 0.5, 1.0)
LORA_RUNS = {f: f"lora_qwen2.5-0.5b{'' if f == 1.0 else f'-frac{f}'}" for f in FRACTIONS}
BASELINE_SETTINGS = {"features": "word+char", "C": 1.0, "class_weight": "balanced"}  # chosen on dev in step 2
REFERENCES = {"claude_sonnet_guided": "Claude Sonnet 5.5 guided", "claude_haiku_fewshot": "Claude Haiku 4.5 few-shot"}
METRICS = {"accuracy": "Accuracy", "macro_f1": "Macro-F1"}
SEED = 2026
CURVES = (("baseline", "TF-IDF baseline", "tab:gray"), ("lora", "LoRA Qwen2.5-0.5B", "tab:blue"))


def stratified_sample(rows: list[dict], fraction: float, seed: int = SEED) -> list[dict]:
    """`fraction` of each group's rows (at least one per group), so every group stays represented."""
    if fraction >= 1:
        return rows
    rng, by_group = random.Random(seed), {}
    for row in rows:
        by_group.setdefault(row["group"], []).append(row)
    sample = []
    for group in sorted(by_group):
        pool = by_group[group]
        sample += rng.sample(pool, max(1, round(len(pool) * fraction)))
    return sample


def baseline_point(train: list[dict], dev: list[dict], fraction: float) -> dict:
    subset = stratified_sample(train, fraction)
    model = baseline.make_model(**BASELINE_SETTINGS).fit([r["mission"] for r in subset], [r["group"] for r in subset])
    metrics = evaluate.score(dev, baseline.ranked_predictions(model, [r["mission"] for r in dev]))
    return {"fraction": fraction, "train_examples": len(subset)} | {k: metrics[k] for k in METRICS}


def lora_point(fraction: float) -> dict:
    run = json.loads((RESULTS_DIR / f"{LORA_RUNS[fraction]}_dev.json").read_text(encoding="utf-8"))
    examples = run["details"]["settings"]["train_examples"]
    return {"fraction": fraction, "train_examples": examples} | {k: run["metrics"][k] for k in METRICS}


def table(curves: dict[str, list[dict]], references: dict[str, dict]) -> str:
    lines = [
        "| Training missions | Baseline accuracy | LoRA 0.5B accuracy | Baseline macro-F1 | LoRA 0.5B macro-F1 |",
        "|---|---|---|---|---|",
    ]
    for b, lo in zip(curves["baseline"], curves["lora"], strict=True):
        lines.append(
            f"| {lo['train_examples']:,} ({lo['fraction']:.0%}) | {b['accuracy']:.1%} | {lo['accuracy']:.1%} "
            f"| {b['macro_f1']:.3f} | {lo['macro_f1']:.3f} |"
        )
    for label, m in references.items():
        lines.append(f"| none: {label} | {m['accuracy']:.1%} | | {m['macro_f1']:.3f} | |")
    return "\n".join(lines)


def chart(curves: dict[str, list[dict]], references: dict[str, dict], path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for ax, (metric, name) in zip(axes, METRICS.items(), strict=True):
        for key, label, color in CURVES:
            points = curves[key]
            ax.plot([p["train_examples"] for p in points], [p[metric] for p in points], "o-", color=color, label=label)
        for (label, m), style in zip(references.items(), ("--", ":"), strict=True):
            ax.axhline(m[metric], color="tab:orange", linestyle=style, label=f"{label} (no training)")
        ax.set_xscale("log")
        sizes = [p["train_examples"] for p in curves["lora"]]
        ax.set_xticks(sizes, [f"{n:,}" for n in sizes])
        ax.minorticks_off()
        ax.set_xlabel("Labeled training missions (log scale)")
        ax.set_ylabel(f"{name} on dev")
        ax.grid(True, alpha=0.3)
    axes[0].legend(fontsize=8, loc="lower right")
    fig.suptitle("How many labeled examples does training need to match prompting? (dev, n=1,813)")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    train, dev = data.load("train"), data.load("dev")
    curves = {
        "baseline": [baseline_point(train, dev, f) for f in FRACTIONS],
        "lora": [lora_point(f) for f in FRACTIONS],
    }
    references = {}
    for approach, label in REFERENCES.items():
        metrics = json.loads((RESULTS_DIR / f"{approach}_dev.json").read_text(encoding="utf-8"))["metrics"]
        references[label] = {k: metrics[k] for k in METRICS}
    text = table(curves, references) + "\n"
    print(text)
    (RESULTS_DIR / "data_size_dev.md").write_text(text, encoding="utf-8")
    (RESULTS_DIR / "data_size_dev.json").write_text(json.dumps(curves | {"references": references}, indent=1))
    chart(curves, references, RESULTS_DIR / "data_size_dev.png")
    print("wrote results/data_size_dev.md, .json and .png")


if __name__ == "__main__":
    main()
