"""Step 6: error analysis. Where do the approaches go wrong, and is it the models, the taxonomy or the labels?

    python src/errors.py             # results/errors_dev.md, .json and .png
    python src/errors.py --sheet     # also writes the blind grading sheet data/error_check.md (once)
    python src/errors.py --graded    # reads your answers in data/error_check.md and adds them to the report

Reads the saved dev predictions of the five final approaches, so it calls no model and costs nothing:

1. Agreement: on how many missions are all five wrong, all right, or split?
2. Confusable groups: which pairs of groups does every approach mix up? Shared confusions point at the
   taxonomy (overlapping groups), not at one model.
3. Mission length: accuracy by number of words. Very short, generic missions may be unanswerable.
4. The cascade's routing (threshold 0.7, step 5): which groups and lengths get sent to Sonnet, and how
   Sonnet does on them.
5. Label noise: a blind sheet mixing missions all five approaches got wrong with control missions; you grade
   the IRS label without knowing which is which. If the hard missions' labels are disputed much more often,
   part of the remaining error is the labels, not the models.
"""

import argparse
import json
import random
import re
import statistics
from collections import Counter

import matplotlib

matplotlib.use("Agg")  # files only, no window
import matplotlib.pyplot as plt

import data
import ntee
from paths import DATA_DIR, ROOT

RESULTS_DIR = ROOT / "results"
APPROACHES = {
    "baseline": "TF-IDF",
    "claude_haiku_fewshot": "Haiku",
    "claude_sonnet_guided": "Sonnet",
    "lora_qwen2.5-0.5b": "LoRA 0.5B",
    "lora_qwen2.5-1.5b": "LoRA 1.5B",
}
LENGTH_BUCKETS = ((1, 5), (6, 10), (11, 20), (21, 40), (41, 10_000))
CASCADE_THRESHOLD = 0.7  # chosen on the tune half of dev in step 5
SHEET_PATH = DATA_DIR / "error_check.md"
KEY_PATH = DATA_DIR / "error_check_key.json"
HARD, CONTROL = 25, 15  # missions on the grading sheet: all five wrong, and others
SEED = 2026


def words(text: str) -> int:
    return len(text.split())


def load_predictions() -> dict[str, dict[str, str]]:
    """approach -> ein -> top-1 group."""
    out = {}
    for approach in APPROACHES:
        run = json.loads((RESULTS_DIR / f"{approach}_dev.json").read_text(encoding="utf-8"))
        out[approach] = {p["ein"]: (p["ranked"][0] if p["ranked"] else "") for p in run["predictions"]}
    return out


def agreement(rows: list[dict], preds: dict) -> dict:
    right = Counter(sum(preds[a][r["ein"]] == r["group"] for a in APPROACHES) for r in rows)
    n = len(rows)
    return {f"{k} of {len(APPROACHES)} right": right[k] / n for k in range(len(APPROACHES), -1, -1)}


def shared_confusions(rows: list[dict], preds: dict, top: int = 12) -> list[dict]:
    """Group pairs (either direction) by how often each approach confuses them; ranked by the approach that
    confuses them least, so a pair near the top is one every approach gets wrong."""
    counts = {a: Counter() for a in APPROACHES}
    for r in rows:
        for a in APPROACHES:
            p = preds[a][r["ein"]]
            if p and p != r["group"]:
                counts[a][tuple(sorted((r["group"], p)))] += 1
    pairs = set().union(*counts.values())
    table = [{"pair": "-".join(pair), **{APPROACHES[a]: counts[a][pair] for a in APPROACHES}} for pair in pairs]
    labels = list(APPROACHES.values())
    table.sort(key=lambda t: (min(t[x] for x in labels), sum(t[x] for x in labels)), reverse=True)
    for t in table:
        a, b = t["pair"].split("-")
        t["names"] = f"{ntee.GROUPS[a]} / {ntee.GROUPS[b]}"
    return table[:top]


def by_length(rows: list[dict], preds: dict) -> list[dict]:
    out = []
    for low, high in LENGTH_BUCKETS:
        bucket = [r for r in rows if low <= words(r["mission"]) <= high]
        out.append({
            "words": f"{low}-{high}" if high < 10_000 else f"{low}+", "n": len(bucket),
            **{APPROACHES[a]: sum(preds[a][r["ein"]] == r["group"] for r in bucket) / len(bucket) for a in APPROACHES},
        })  # fmt: skip
    return out


def cascade_routing(rows: list[dict], preds: dict) -> dict:
    lora = json.loads((RESULTS_DIR / "finetuned" / "lora_qwen2.5-0.5b_dev.json").read_text(encoding="utf-8"))
    confidence = {p["ein"]: p["probs"][0] for p in lora["predictions"]}
    sent = [r for r in rows if confidence[r["ein"]] < CASCADE_THRESHOLD]
    kept = [r for r in rows if confidence[r["ein"]] >= CASCADE_THRESHOLD]
    by_group = []
    for group in ntee.GROUPS:
        members = [r for r in rows if r["group"] == group]
        routed = [r for r in members if confidence[r["ein"]] < CASCADE_THRESHOLD]
        by_group.append({"group": group, "n": len(members), "sent": len(routed) / len(members)})
    by_group.sort(key=lambda g: g["sent"], reverse=True)

    def acc(subset, approach):
        return sum(preds[approach][r["ein"]] == r["group"] for r in subset) / len(subset)

    return {
        "sent_share": len(sent) / len(rows),
        "median_words_sent": statistics.median(words(r["mission"]) for r in sent),
        "median_words_kept": statistics.median(words(r["mission"]) for r in kept),
        "sonnet_on_sent": acc(sent, "claude_sonnet_guided"),
        "lora_on_sent": acc(sent, "lora_qwen2.5-0.5b"),
        "lora_on_kept": acc(kept, "lora_qwen2.5-0.5b"),
        "most_sent_groups": by_group[:6],
        "least_sent_groups": by_group[-4:],
    }


def hardest_examples(rows: list[dict], preds: dict, n: int = 10) -> list[dict]:
    """The shortest missions all five approaches got wrong, with what each said."""
    wrong = [r for r in rows if all(preds[a][r["ein"]] != r["group"] for a in APPROACHES)]
    wrong.sort(key=lambda r: (words(r["mission"]), r["ein"]))
    return [{"mission": r["mission"], "irs": r["group"], **{APPROACHES[a]: preds[a][r["ein"]] for a in APPROACHES}}
            for r in wrong[:n]]  # fmt: skip


# --- the blind grading sheet -------------------------------------------------------------------------------


def sheet_sample(rows: list[dict], preds: dict, seed: int = SEED) -> list[dict]:
    """HARD missions all five got wrong and CONTROL missions from the rest, shuffled together."""
    rng = random.Random(seed)
    all_wrong = sorted((r for r in rows if all(preds[a][r["ein"]] != r["group"] for a in APPROACHES)),
                       key=lambda r: r["ein"])  # fmt: skip
    others = sorted((r for r in rows if r not in all_wrong), key=lambda r: r["ein"])
    chosen = [r | {"kind": "hard"} for r in rng.sample(all_wrong, HARD)]
    chosen += [r | {"kind": "control"} for r in rng.sample(others, CONTROL)]
    rng.shuffle(chosen)
    return chosen


def write_sheet(sample: list[dict]) -> None:
    groups = "; ".join(f"**{k}** {v}" for k, v in ntee.GROUPS.items())
    parts = [
        "# Error check: do you agree with the IRS's NTEE group? (step 6)\n",
        "Same as the step 1 label check, on dev missions this time. Some missions here every approach got wrong, "
        "others not; which is which is hidden in `error_check_key.json` until you're done, so please don't open it. "
        "Write **yes**, **no** (and the group you'd pick) or **unsure** after `Your call:`. Judge only from the "
        "mission text.\n",
        f"Groups: {groups}\n",
    ]
    for i, r in enumerate(sample, 1):
        parts.append(
            f"---\n\n## {i}. EIN {r['ein']} ({r['form']})\n\n> {r['mission']}\n\n"
            f"IRS code **{r['ntee_code']}** → group **{r['group']}**: {ntee.GROUPS[r['group']]}\n\n**Your call:** \n"
        )
    SHEET_PATH.write_text("\n".join(parts), encoding="utf-8")
    KEY_PATH.write_text(json.dumps([{"ein": r["ein"], "kind": r["kind"]} for r in sample], indent=1))


def read_answers(text: str) -> dict[str, str]:
    """EIN -> yes / no / unsure from a graded sheet (the first word after 'Your call:', on that line or the next)."""
    answers = {}
    for block in text.split("\n## ")[1:]:
        ein = re.search(r"EIN (\d+)", block).group(1)
        after = block.split("**Your call:**", 1)[1].strip().lower()
        first = re.match(r"(yes|no|unsure)", after)
        if first:
            answers[ein] = first.group(1)
    return answers


def read_picks(text: str) -> dict[str, set[str]]:
    """EIN -> the group letters written after a no or unsure ("no, F or P" -> {F, P})."""
    picks = {}
    for block in text.split("\n## ")[1:]:
        ein = re.search(r"EIN (\d+)", block).group(1)
        call = block.split("**Your call:**", 1)[1].split("\n---")[0].strip()
        letters = set(re.findall(r"\b([A-Y])\b", call[len(call.split()[0]) :] if call else ""))
        if letters:
            picks[ein] = letters
    return picks


def picks_vs_models(key: list[dict], answers: dict[str, str], picks: dict[str, set[str]], preds: dict) -> dict:
    """On the hard missions where you disagreed with the IRS: how often each approach chose a group you picked."""
    disputed = [k["ein"] for k in key if k["kind"] == "hard" and answers.get(k["ein"]) == "no" and k["ein"] in picks]
    return {"n": len(disputed)} | {
        APPROACHES[a]: sum(preds[a][ein] in picks[ein] for ein in disputed) for a in APPROACHES
    }


def graded(key: list[dict], answers: dict[str, str]) -> dict:
    out = {}
    for kind in ("hard", "control"):
        calls = [answers[k["ein"]] for k in key if k["kind"] == kind and k["ein"] in answers]
        out[kind] = {"graded": len(calls), **{c: calls.count(c) for c in ("yes", "unsure", "no")}}
    return out


# --- report ------------------------------------------------------------------------------------------------


def markdown(result: dict) -> str:
    labels = list(APPROACHES.values())
    lines = ["## Agreement across the five approaches (dev)", "", "| | Share of missions |", "|---|---|"]
    lines += [f"| {k} | {v:.1%} |" for k, v in result["agreement"].items()]
    lines += ["", "## Group pairs every approach confuses (either direction)", "",
              "| Pair | Groups | " + " | ".join(labels) + " |", "|---|---|" + "---|" * len(labels)]  # fmt: skip
    lines += [f"| {t['pair']} | {t['names']} | " + " | ".join(str(t[x]) for x in labels) + " |"
              for t in result["confusions"]]  # fmt: skip
    lines += ["", "## Accuracy by mission length (words)", "", "| Words | Missions | " + " | ".join(labels) + " |",
              "|---|---|" + "---|" * len(labels)]  # fmt: skip
    lines += [f"| {b['words']} | {b['n']} | " + " | ".join(f"{b[x]:.0%}" for x in labels) + " |"
              for b in result["length"]]  # fmt: skip
    c = result["cascade"]
    lines += [
        "", f"## The cascade's routing (threshold {CASCADE_THRESHOLD})", "",
        f"- Sent to Sonnet: {c['sent_share']:.0%} of missions (median {c['median_words_sent']:.0f} words, vs "
        f"{c['median_words_kept']:.0f} for those kept).",
        f"- On the missions sent, Sonnet is right {c['sonnet_on_sent']:.0%} of the time, the small model "
        f"{c['lora_on_sent']:.0%}; on those kept, the small model is right {c['lora_on_kept']:.0%}.",
        "- Groups sent most: " + ", ".join(f"{g['group']} {g['sent']:.0%}" for g in c["most_sent_groups"]),
        "- Groups sent least: " + ", ".join(f"{g['group']} {g['sent']:.0%}" for g in c["least_sent_groups"]),
    ]  # fmt: skip
    lines += ["", "## The shortest missions all five got wrong", "",
              "| Mission | IRS | " + " | ".join(labels) + " |", "|---|---|" + "---|" * len(labels)]  # fmt: skip
    lines += [f"| {h['mission'][:90]} | {h['irs']} | " + " | ".join(h[x] for x in labels) + " |"
              for h in result["hardest"]]  # fmt: skip
    if "graded" in result:
        lines += ["", "## Label check of hard vs control missions (graded blind)", "",
                  "| Missions | Graded | Agree with IRS | Unsure | Disagree |", "|---|---|---|---|---|"]  # fmt: skip
        for kind, g in result["graded"].items():
            n = max(g["graded"], 1)
            lines.append(f"| {kind} | {g['graded']} | {g['yes'] / n:.0%} | {g['unsure'] / n:.0%} | {g['no'] / n:.0%} |")
        pv = result["picks_vs_models"]
        lines += ["", f"On the {pv['n']} hard missions where you disagreed with the IRS, each approach chose the group "
                  "you picked: " + ", ".join(f"{label} {pv[label]}" for label in APPROACHES.values())]  # fmt: skip
    return "\n".join(lines) + "\n"


def chart(result: dict, path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.6))
    shares = list(result["agreement"].values())
    left.bar(range(len(shares)), [s * 100 for s in shares], color="tab:purple")
    left.set_xticks(range(len(shares)), [k.split(" of")[0] for k in result["agreement"]])
    left.set_xlabel(f"How many of the {len(APPROACHES)} approaches are right")
    left.set_ylabel("Share of dev missions (%)")
    left.set_title("Agreement across approaches")
    buckets = result["length"]
    for label in APPROACHES.values():
        right.plot(range(len(buckets)), [b[label] * 100 for b in buckets], "o-", label=label)
    right.set_xticks(range(len(buckets)), [f"{b['words']}\n(n={b['n']})" for b in buckets])
    right.set_xlabel("Mission length (words)")
    right.set_ylabel("Accuracy (%)")
    right.set_ylim(0, 100)  # the full scale: length moves accuracy by only a few points
    right.set_title("Accuracy by mission length")
    right.legend(fontsize=8, loc="lower right")
    for ax in (left, right):
        ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet", action="store_true", help="write the blind grading sheet (once)")
    parser.add_argument("--graded", action="store_true", help="add your graded sheet to the report")
    args = parser.parse_args()

    rows, preds = data.load("dev"), load_predictions()
    result = {
        "agreement": agreement(rows, preds),
        "confusions": shared_confusions(rows, preds),
        "length": by_length(rows, preds),
        "cascade": cascade_routing(rows, preds),
        "hardest": hardest_examples(rows, preds),
    }
    if args.sheet:
        if SHEET_PATH.exists():
            raise SystemExit(f"{SHEET_PATH.name} exists; it may hold your answers, so it isn't overwritten")
        write_sheet(sheet_sample(rows, preds))
        print(f"wrote {SHEET_PATH.relative_to(ROOT)} ({HARD + CONTROL} missions) and its key")
    if args.graded:
        key = json.loads(KEY_PATH.read_text(encoding="utf-8"))
        sheet = SHEET_PATH.read_text(encoding="utf-8")
        answers = read_answers(sheet)
        result["graded"] = graded(key, answers)
        result["picks_vs_models"] = picks_vs_models(key, answers, read_picks(sheet), preds)
    text = markdown(result)
    print(text)
    (RESULTS_DIR / "errors_dev.md").write_text(text, encoding="utf-8")
    (RESULTS_DIR / "errors_dev.json").write_text(json.dumps(result, indent=1))
    chart(result, RESULTS_DIR / "errors_dev.png")
    print("wrote results/errors_dev.md, .json and .png")


if __name__ == "__main__":
    main()
