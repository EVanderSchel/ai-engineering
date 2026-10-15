"""Step 5: the three approaches side by side on accuracy, speed and cost, from their saved runs.

    python src/compare.py              # dev split: results/comparison_dev.md and results/comparison_dev.png
    python src/compare.py --split test # once every approach has its one held-out test score

Reads results/<approach>_<split>.json (written by src/evaluate.py), so it calls no model and costs nothing.

Costs are per 1,000 predictions, on the same terms the approaches were run: Claude at standard API prices
(the Batch API would halve them, shown separately), the fine-tuned model as on-demand T4 time with
predictions batched, the baseline as ~$0 (milliseconds of CPU). One-time costs (training) are separate, with
how many predictions it takes for a fine-tuned model's training to pay for itself against Claude.
"""

import argparse
import json

import matplotlib

matplotlib.use("Agg")  # files only, no window
import matplotlib.pyplot as plt

from paths import ROOT

RESULTS_DIR = ROOT / "results"
# The best run of each kind, chosen on dev in steps 2-4, with a short label for the table and chart.
APPROACHES = {
    "baseline": "TF-IDF baseline",
    "lora_qwen2.5-0.5b": "LoRA Qwen2.5-0.5B",
    "lora_qwen2.5-1.5b": "LoRA Qwen2.5-1.5B",
    "claude_haiku_fewshot": "Claude Haiku 4.5 few-shot",
    "claude_sonnet_guided": "Claude Sonnet 5.5 guided",
}
KIND_COLORS = {"baseline": "tab:gray", "lora": "tab:blue", "claude": "tab:orange"}
FREE = 1e-5  # where a ~$0 cost sits on the chart's log scale


def kind(approach: str) -> str:
    return approach.split("_")[0]


def load(approach: str, split: str) -> dict:
    path = RESULTS_DIR / f"{approach}_{split}.json"
    if not path.exists():
        raise FileNotFoundError(f"no {split} run for {approach}: expected {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def row(approach: str, run: dict) -> dict:
    details = run.get("details") or {}
    return {
        "approach": approach,
        "label": APPROACHES.get(approach, approach),
        "kind": kind(approach),
        **{k: run["metrics"][k] for k in ("accuracy", "top3", "macro_f1", "broad_accuracy")},
        "latency_ms": run["latency_ms"],
        "cost_per_1k_usd": run["cost_per_1k_usd"],
        "batch_cost_per_1k_usd": details.get("batch_cost_per_1k_usd"),
        "train_cost_usd": details.get("train_cost_usd", 0.0),
    }


def payback_predictions(train_cost_usd: float, cost_per_1k: float, rival_cost_per_1k: float) -> float:
    """How many predictions before training cost plus cheaper predictions beats paying the rival per prediction."""
    saving_per_prediction = (rival_cost_per_1k - cost_per_1k) / 1000
    return train_cost_usd / saving_per_prediction if saving_per_prediction > 0 else float("inf")


def money(usd: float) -> str:
    if usd == 0:
        return "~$0"
    return f"${usd:.4f}" if usd < 0.01 else f"${usd:.2f}"


def table(rows: list[dict]) -> str:
    lines = [
        "| Approach | Accuracy | Top-3 | Macro-F1 | Broad | One prediction | Per 1,000 | Per 1,000 (batch API) "
        "| Training |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        batch = money(r["batch_cost_per_1k_usd"]) if r["batch_cost_per_1k_usd"] is not None else ""
        lines.append(
            f"| {r['label']} | {r['accuracy']:.1%} | {r['top3']:.1%} | {r['macro_f1']:.3f} | {r['broad_accuracy']:.1%} "
            f"| {r['latency_ms']:,.0f} ms | {money(r['cost_per_1k_usd'])} | {batch} | {money(r['train_cost_usd'])} |"
        )
    return "\n".join(lines)


def chart(rows: list[dict], path, split: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=True)
    for ax, field, xlabel in (
        (axes[0], "cost_per_1k_usd", "Cost per 1,000 predictions (US$, log scale)"),
        (axes[1], "latency_ms", "Time for one prediction (ms, log scale)"),
    ):
        for r in rows:
            x = max(r[field], FREE)
            ax.scatter(x, r["accuracy"] * 100, s=70, color=KIND_COLORS[r["kind"]], zorder=3)
            ax.annotate(r["label"], (x, r["accuracy"] * 100), textcoords="offset points", xytext=(6, 5), fontsize=8)
        ax.set_xscale("log")
        ax.set_xlim(right=ax.get_xlim()[1] * 15)  # room for the labels of the rightmost points
        ax.set_xlabel(xlabel)
        ax.grid(True, which="major", alpha=0.3)
    axes[0].set_ylabel(f"Accuracy on {split} (%)")
    axes[0].annotate(r"baseline: ~\$0, plotted at \$0.00001", (0.02, 0.02), xycoords="axes fraction", fontsize=7)
    fig.suptitle(f"NTEE classification: accuracy vs cost and speed ({split}, n={rows[0]['n']:,})")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    args = parser.parse_args()

    runs = {a: load(a, args.split) for a in APPROACHES}
    rows = [row(a, run) | {"n": run["metrics"]["n"]} for a, run in runs.items()]
    text = table(rows)
    rival = min((r for r in rows if r["kind"] == "claude"), key=lambda r: r["cost_per_1k_usd"])
    paybacks = [
        f"- {r['label']}: training ({money(r['train_cost_usd'])}) pays for itself against {rival['label']} after "
        f"{payback_predictions(r['train_cost_usd'], r['cost_per_1k_usd'], rival['cost_per_1k_usd']):,.0f} predictions."
        for r in rows
        if r["kind"] == "lora"
    ]
    text += "\n\n" + "\n".join(paybacks) + "\n"
    print(text)
    (RESULTS_DIR / f"comparison_{args.split}.md").write_text(text, encoding="utf-8")
    chart(rows, RESULTS_DIR / f"comparison_{args.split}.png", args.split)
    print(f"wrote results/comparison_{args.split}.md and .png")


if __name__ == "__main__":
    main()
