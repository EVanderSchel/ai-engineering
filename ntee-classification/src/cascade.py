"""Step 5's hybrid: the fine-tuned model answers when it's confident, and Claude Sonnet takes the rest.

    python src/cascade.py     # writes results/cascade_dev.md, .json and .png

Simulated from saved predictions, so it costs nothing: the Qwen2.5-0.5B LoRA run's probabilities
(results/finetuned/, from the notebook) and Sonnet guided's answers with each request's real cost and time
(results/claude/). For a threshold t, a mission whose top probability is at least t keeps the small model's
answer; the others are sent to Sonnet. Every mission pays the small model's cost and time; escalated ones
add Sonnet's.

Two questions:
1. Calibration: does the small model's confidence mean anything? Accuracy by confidence band, next to
   Sonnet's on the same missions, shows whether low confidence marks the missions Sonnet would get right.
2. The threshold: chosen on a random half of dev (tune) and reported on the other half (report), so the
   reported score isn't fitted to the answers it's scored on. Two rules: the cheapest threshold that matches
   Sonnet's accuracy on the tune half, and the most accurate one.
"""

import json
import random
import statistics

import matplotlib

matplotlib.use("Agg")  # files only, no window
import matplotlib.pyplot as plt

import data
from paths import ROOT

RESULTS_DIR = ROOT / "results"
LORA_FILE = RESULTS_DIR / "finetuned" / "lora_qwen2.5-0.5b_dev.json"
SONNET_FILE = RESULTS_DIR / "claude" / "claude_sonnet_guided_dev.jsonl"
THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999)
BANDS = ((0.0, 0.5), (0.5, 0.8), (0.8, 0.95), (0.95, 0.99), (0.99, 1.01))
SEED = 2026


def load_missions() -> list[dict]:
    """One record per dev mission: gold group, each model's answer, the small model's confidence, and the
    cost and time of each."""
    lora = json.loads(LORA_FILE.read_text(encoding="utf-8"))
    if "probs" not in lora["predictions"][0]:
        raise ValueError(f"{LORA_FILE.name} has no probabilities: rerun it with the current notebook")
    lora_cost = lora["batch_ms_per_mission"] / 1000 / 3600 * lora["gpu_usd_per_hour"]
    small = {p["ein"]: p for p in lora["predictions"]}
    sonnet = {}
    for line in SONNET_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            answer = json.loads(line)
            sonnet[answer["ein"]] = answer
    missions = []
    for row in data.load("dev"):
        s, b = small[row["ein"]], sonnet[row["ein"]]
        missions.append({
            "ein": row["ein"], "gold": row["group"],
            "small": s["ranked"][0], "confidence": s["probs"][0], "small_cost": lora_cost,
            "small_ms": lora["latency_ms"],
            "sonnet": b["ranked"][0] if b["ranked"] else "", "sonnet_cost": b["cost_usd"],
            "sonnet_ms": b["seconds"] * 1000,
        })  # fmt: skip
    return missions


def halves(missions: list[dict], seed: int = SEED) -> tuple[list[dict], list[dict]]:
    shuffled = sorted(missions, key=lambda m: m["ein"])
    random.Random(seed).shuffle(shuffled)
    middle = len(shuffled) // 2
    return shuffled[:middle], shuffled[middle:]


def cascade(missions: list[dict], threshold: float) -> dict:
    """Accuracy, share escalated, cost per 1,000 and mean time per mission, for one threshold. A threshold
    above 1 sends everything to Sonnet; 0 sends nothing."""
    correct = escalated = 0
    cost = ms = 0.0
    for m in missions:
        keep = m["confidence"] >= threshold
        correct += (m["small"] if keep else m["sonnet"]) == m["gold"]
        escalated += not keep
        cost += m["small_cost"] + (0 if keep else m["sonnet_cost"])
        ms += m["small_ms"] + (0 if keep else m["sonnet_ms"])
    n = len(missions)
    return {
        "threshold": threshold, "n": n, "accuracy": correct / n, "escalated": escalated / n,
        "cost_per_1k_usd": cost / n * 1000, "mean_ms": ms / n,
    }  # fmt: skip


def sonnet_alone(missions: list[dict]) -> dict:
    n = len(missions)
    return {
        "threshold": None, "n": n, "accuracy": sum(m["sonnet"] == m["gold"] for m in missions) / n, "escalated": 1.0,
        "cost_per_1k_usd": sum(m["sonnet_cost"] for m in missions) / n * 1000,
        "mean_ms": statistics.mean(m["sonnet_ms"] for m in missions),
    }  # fmt: skip


def calibration(missions: list[dict]) -> list[dict]:
    rows = []
    for low, high in BANDS:
        band = [m for m in missions if low <= m["confidence"] < high]
        if band:
            rows.append({
                "band": f"{low:.2f}-{min(high, 1):.2f}", "n": len(band), "share": len(band) / len(missions),
                "mean_confidence": statistics.mean(m["confidence"] for m in band),
                "small_accuracy": sum(m["small"] == m["gold"] for m in band) / len(band),
                "sonnet_accuracy": sum(m["sonnet"] == m["gold"] for m in band) / len(band),
            })  # fmt: skip
    return rows


def choose(tune: list[dict]) -> dict[str, float]:
    """Thresholds picked on the tune half only."""
    points = [cascade(tune, t) for t in THRESHOLDS]
    target = sonnet_alone(tune)["accuracy"]
    matching = [p for p in points if p["accuracy"] >= target]
    best = max(points, key=lambda p: (p["accuracy"], -p["threshold"]))  # ties: the cheaper, lower threshold
    return {
        "match_sonnet": min(matching, key=lambda p: p["cost_per_1k_usd"])["threshold"] if matching else 1.01,
        "most_accurate": best["threshold"],
    }


def paired_interval(missions: list[dict], threshold: float, resamples: int = 2000, seed: int = SEED) -> list[float]:
    """95% bootstrap interval for (cascade accuracy - Sonnet accuracy) on the same missions: resample missions
    with replacement, so both approaches are always scored on identical sets."""
    diffs = []
    for m in missions:
        answer = m["small"] if m["confidence"] >= threshold else m["sonnet"]
        diffs.append((answer == m["gold"]) - (m["sonnet"] == m["gold"]))
    rng, n = random.Random(seed), len(diffs)
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(resamples))
    return [means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]]


def report(missions: list[dict]) -> dict:
    tune, held = halves(missions)
    chosen = choose(tune)
    small_alone = cascade(held, 0.0)
    return {
        "calibration": calibration(missions),
        "chosen_on_tune_half": chosen,
        "report_half": {
            "small model alone": small_alone,
            "Sonnet alone": sonnet_alone(held),
            **{f"cascade, {rule} (t={t})": cascade(held, t) for rule, t in chosen.items()},
        },
        "vs_sonnet_95pct": {rule: paired_interval(held, t) for rule, t in chosen.items()},
        "either_right": sum(m["gold"] in (m["small"], m["sonnet"]) for m in held) / len(held),
        "curve_all_dev": [cascade(missions, t) for t in THRESHOLDS],
    }


def markdown(result: dict) -> str:
    lines = [
        "Calibration (all dev): the small model's accuracy by its confidence, and Sonnet's on the same missions",
        "",
        "| Confidence | Missions | Share | Mean confidence | Small model right | Sonnet right |",
        "|---|---|---|---|---|---|",
    ]
    for c in result["calibration"]:
        lines.append(
            f"| {c['band']} | {c['n']:,} | {c['share']:.0%} | {c['mean_confidence']:.2f} | {c['small_accuracy']:.1%} "
            f"| {c['sonnet_accuracy']:.1%} |"
        )
    n = next(iter(result["report_half"].values()))["n"]
    lines += [
        "",
        f"Cascade, thresholds chosen on the tune half, scored on the report half (n={n:,})",
        "",
        "| Approach | Accuracy | Sent to Sonnet | Cost per 1,000 | Mean time |",
        "|---|---|---|---|---|",
    ]
    for name, r in result["report_half"].items():
        lines.append(
            f"| {name} | {r['accuracy']:.1%} | {r['escalated']:.0%} | ${r['cost_per_1k_usd']:.3f} | "
            f"{r['mean_ms']:,.0f} ms |"
        )
    lines.append(f"| ceiling: either model right | {result['either_right']:.1%} | | | |")
    lines.append("")
    for rule, (low, high) in result["vs_sonnet_95pct"].items():
        lines.append(f"- {rule}: accuracy minus Sonnet's, 95% paired bootstrap interval {low:+.1%} to {high:+.1%}")
    return "\n".join(lines) + "\n"


def chart(result: dict, missions: list[dict], path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.8))
    bands = result["calibration"]
    xs = range(len(bands))
    for offset, field, label, color in ((-0.2, "small_accuracy", "LoRA 0.5B", "tab:blue"),
                                        (0.2, "sonnet_accuracy", "Sonnet", "tab:orange")):  # fmt: skip
        left.bar([x + offset for x in xs], [b[field] * 100 for b in bands], 0.4, label=label, color=color)
    left.set_xticks(list(xs), [f"{b['band']}\n({b['share']:.0%})" for b in bands])
    left.set_xlabel("LoRA model's confidence (share of missions)")
    left.set_ylabel("Accuracy on those missions (%)")
    left.set_title("Is the small model's confidence meaningful?")
    left.legend(fontsize=8)

    curve = result["curve_all_dev"]
    right.plot([p["escalated"] * 100 for p in curve], [p["accuracy"] * 100 for p in curve], "o-", color="tab:purple",
               label="cascade (thresholds 0.3-0.999)")  # fmt: skip
    right.axhline(sonnet_alone(missions)["accuracy"] * 100, color="tab:orange", linestyle="--", label="Sonnet alone")
    right.axhline(cascade(missions, 0.0)["accuracy"] * 100, color="tab:blue", linestyle=":", label="LoRA 0.5B alone")
    right.set_xlabel("Missions sent to Sonnet (%) ~ cost")
    right.set_ylabel("Accuracy (%)")
    right.set_title("Cascade on all dev (descriptive; thresholds chosen on half)")
    right.legend(fontsize=8, loc="center right")
    for ax in (left, right):
        ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    missions = load_missions()
    result = report(missions)
    text = markdown(result)
    print(text)
    (RESULTS_DIR / "cascade_dev.md").write_text(text, encoding="utf-8")
    (RESULTS_DIR / "cascade_dev.json").write_text(json.dumps(result, indent=1))
    chart(result, missions, RESULTS_DIR / "cascade_dev.png")
    print("wrote results/cascade_dev.md, .json and .png")


if __name__ == "__main__":
    main()
