"""The shared scorer: every approach (baseline, prompted Claude, fine-tuned model) is measured the same way.

An approach hands over, for each mission, a ranked list of NTEE groups (its best guess first; at least three
when it can rank, so top-3 accuracy is defined), plus how long predictions took and what they cost.

Metrics, and why each is here:
- accuracy: the predicted group equals the IRS group. Limited by label ambiguity: in the user's check of 50
  labels, 74% were clear, 20% ambiguous (several groups fit) and 6% looked wrong, so expect a ceiling of
  roughly 75-90%, not 100%.
- top3: the IRS group is among the three best guesses; fairer to genuinely multi-purpose missions.
- macro_f1: F1 averaged over groups with equal weight, so small groups (V has 41 examples) count as much as
  large ones. Accuracy alone would hide a model that ignores rare groups.
- broad_accuracy: right broad category (e.g. E Health Care and F Mental Health are both under Health):
  mixing up related groups is a smaller error than unrelated ones.
- per-group precision/recall/F1 and the most common confusions, for error analysis.
- latency_ms (one prediction) and cost_per_1k_usd (1,000 predictions).

Every run is saved in results/<approach>_<split>.json with all its predictions, and summarized in
results/history.csv.
"""

import csv
import datetime
import json
from collections import Counter

from sklearn.metrics import f1_score, precision_recall_fscore_support

import ntee
from paths import ROOT

RESULTS_DIR = ROOT / "results"
HISTORY_PATH = RESULTS_DIR / "history.csv"
HISTORY_FIELDS = [
    "time", "approach", "split", "n", "accuracy", "top3", "macro_f1", "broad_accuracy",
    "latency_ms", "cost_per_1k_usd", "notes",
]  # fmt: skip


def score(rows: list[dict], ranked: list[list[str]]) -> dict:
    """Metrics for predictions `ranked` (one ranked list of groups per row, in the same order as rows)."""
    if len(rows) != len(ranked):
        raise ValueError(f"{len(rows)} rows but {len(ranked)} predictions")
    gold = [r["group"] for r in rows]
    top1 = [preds[0] if preds else "" for preds in ranked]  # an empty list counts as a wrong answer
    labels = sorted(set(gold))  # groups present in this split
    precision, recall, f1, support = precision_recall_fscore_support(gold, top1, labels=labels, zero_division=0)
    confusions = Counter((g, p) for g, p in zip(gold, top1, strict=True) if g != p)
    n = len(rows)
    return {
        "n": n,
        "accuracy": sum(g == p for g, p in zip(gold, top1, strict=True)) / n,
        "top3": sum(g in preds[:3] for g, preds in zip(gold, ranked, strict=True)) / n,
        "macro_f1": f1_score(gold, top1, labels=labels, average="macro", zero_division=0),
        "broad_accuracy": sum(
            p in ntee.GROUPS and ntee.broad_category(p) == ntee.broad_category(g)
            for g, p in zip(gold, top1, strict=True)
        )
        / n,
        "per_group": {
            label: {"precision": p, "recall": r, "f1": f, "support": int(s)}
            for label, p, r, f, s in zip(labels, precision, recall, f1, support, strict=True)
        },
        "top_confusions": [{"gold": g, "predicted": p, "count": c} for (g, p), c in confusions.most_common(15)],
    }


def record(approach: str, split: str, rows: list[dict], ranked: list[list[str]], *, latency_ms: float,
           cost_per_1k_usd: float, notes: str = "", details: dict | None = None) -> dict:  # fmt: skip
    """Score, save the full run (with every prediction) and append a summary line to results/history.csv."""
    metrics = score(rows, ranked)
    RESULTS_DIR.mkdir(exist_ok=True)
    run = {
        "approach": approach,
        "split": split,
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "latency_ms": latency_ms,
        "cost_per_1k_usd": cost_per_1k_usd,
        "notes": notes,
        "details": details or {},
        "predictions": [
            {"ein": r["ein"], "gold": r["group"], "ranked": preds[:5]} for r, preds in zip(rows, ranked, strict=True)
        ],
    }
    (RESULTS_DIR / f"{approach}_{split}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    new_file = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(
            {
                "time": run["time"],
                "approach": approach,
                "split": split,
                "n": metrics["n"],
                "accuracy": round(metrics["accuracy"], 4),
                "top3": round(metrics["top3"], 4),
                "macro_f1": round(metrics["macro_f1"], 4),
                "broad_accuracy": round(metrics["broad_accuracy"], 4),
                "latency_ms": round(latency_ms, 3),
                "cost_per_1k_usd": round(cost_per_1k_usd, 4),
                "notes": notes,
            }  # fmt: skip
        )
    return metrics


def summary(metrics: dict) -> str:
    return (
        f"accuracy {metrics['accuracy']:.1%} | top-3 {metrics['top3']:.1%} | macro-F1 {metrics['macro_f1']:.3f} | "
        f"broad {metrics['broad_accuracy']:.1%} (n={metrics['n']})"
    )
