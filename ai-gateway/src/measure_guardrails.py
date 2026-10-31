"""Measure the guardrails on their labelled cases: what they catch, what they miss, what they wrongly flag, and what
they cost in time. Free: the model judge's answers come from src/judge_cases.py (paid), if it has been run.

    uv run python src/measure_guardrails.py        # writes results/guardrails.{json,md}

Personal data (src/pii.py), on data/guardrails/pii_cases.json: each case lists the kinds a careful human would mark,
including ones the detector misses by design, so recall isn't flattered. The cases were written alongside the
detector (there's no held-out set), so treat the numbers as a description of its behaviour, not a benchmark.
"""

import json
import statistics
import time
from decimal import Decimal

import injection
import paths
import pii
from paths import RESULTS_DIR

CASES_DIR = paths.ROOT / "data" / "guardrails"


def score_pii(cases: list[dict]) -> dict:
    """Per kind: how many labelled values were found (true positives), missed, and found where none was labelled."""
    per_kind = {kind: {"labelled": 0, "found": 0, "missed": 0, "false_alarms": 0} for kind in pii.KINDS}
    clean_flagged, misses, false_alarms = 0, [], []
    for case in cases:
        expected = list(case["expect"])
        got = [f.kind for f in pii.find(case["text"])]
        remaining = list(got)
        for kind in expected:
            per_kind[kind]["labelled"] += 1
            if kind in remaining:
                remaining.remove(kind)
                per_kind[kind]["found"] += 1
            else:
                per_kind[kind]["missed"] += 1
                misses.append({"kind": kind, "text": case["text"], "why": case.get("known_miss", "")})
        for kind in remaining:
            per_kind[kind]["false_alarms"] += 1
            false_alarms.append({"kind": kind, "text": case["text"]})
        if not expected and got:
            clean_flagged += 1
    labelled = sum(k["labelled"] for k in per_kind.values())
    found = sum(k["found"] for k in per_kind.values())
    flagged = found + len(false_alarms)
    return {
        "cases": len(cases),
        "clean_cases": sum(not c["expect"] for c in cases),
        "clean_cases_flagged": clean_flagged,
        "recall": round(found / labelled, 3),
        "precision": round(found / flagged, 3) if flagged else None,
        "per_kind": per_kind,
        "misses": misses,
        "false_alarms": false_alarms,
    }


def time_pii(kilobytes: int = 100, repeats: int = 20) -> dict:
    """Microseconds to scan (and to redact) a kilobyte of ordinary text with a few values in it."""
    filler = "The committee met on 2026-10-28 to review 14 applications totalling $1,250,000. "
    text = (filler * (kilobytes * 1024 // len(filler))) + " jo@example.com 515-555-0134 123-45-6789"
    scans, redactions = [], []
    for _ in range(repeats):
        started = time.perf_counter()
        pii.find(text)
        scans.append(time.perf_counter() - started)
        started = time.perf_counter()
        pii.redact(text)
        redactions.append(time.perf_counter() - started)
    return {
        "kilobytes": kilobytes,
        "scan_us_per_kb": round(statistics.median(scans) / kilobytes * 1e6, 1),
        "redact_us_per_kb": round(statistics.median(redactions) / kilobytes * 1e6, 1),
    }


def injection_cases() -> list[dict]:
    return json.loads((CASES_DIR / "injection_cases.json").read_text(encoding="utf-8"))["cases"]


def judge_answers(split: str) -> dict | None:
    path = RESULTS_DIR / f"injection-judge-{split}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def confusion(cases: list[dict], flagged: dict[str, bool | None]) -> dict:
    """Caught (of injections), false alarms (of clean cases), and undecided (the judge declined)."""
    injections = [c for c in cases if c["label"] == "injection"]
    clean = [c for c in cases if c["label"] == "clean"]
    return {
        "caught": sum(flagged[c["id"]] is True for c in injections),
        "injections": len(injections),
        "false_alarms": sum(flagged[c["id"]] is True for c in clean),
        "clean": len(clean),
        "undecided": sum(flagged[c["id"]] is None for c in cases),
        "missed": [c["id"] for c in injections if flagged[c["id"]] is not True],
        "false_alarm_ids": [c["id"] for c in clean if flagged[c["id"]] is True],
    }


def score_injection(cases: list[dict]) -> dict:
    """Per split: the pattern screen, and (where its answers exist) the judge and the two combined."""
    out = {}
    for split in ("dev", "held_out"):
        part = [c for c in cases if c["split"] == split]
        patterns = {c["id"]: bool(injection.screen_patterns(c["text"])) for c in part}
        result = {"patterns": confusion(part, patterns)}
        answers = judge_answers(split)
        if answers:
            judged = {a["id"]: a["injection"] for a in answers["answers"]}
            either = {i: patterns[i] or judged[i] is True for i in patterns}
            result |= {
                "judge": confusion(part, judged),
                "either": confusion(part, either),
                "judge_meta": {k: answers[k] for k in ("model", "effort", "prompt_version", "total_cost_usd")},
                "judge_seconds_median": round(statistics.median(a["seconds"] for a in answers["answers"]), 2),
                "judge_cost_per_case_usd": f"{Decimal(answers['total_cost_usd']) / len(answers['answers']):.6f}",
            }
        by_shape: dict[str, dict] = {}
        for c in part:
            if c["label"] == "injection":
                row = by_shape.setdefault(c["shape"], {"cases": 0, "patterns": 0, "judge": 0})
                row["cases"] += 1
                row["patterns"] += patterns[c["id"]]
                if answers:
                    row["judge"] += judged[c["id"]] is True
        result["by_shape"] = by_shape
        out[split] = result
    return out


def time_patterns(cases: list[dict], repeats: int = 50) -> float:
    """Median microseconds to run every pattern rule over one case."""
    times = []
    for _ in range(repeats):
        for c in cases:
            started = time.perf_counter()
            injection.screen_patterns(c["text"])
            times.append(time.perf_counter() - started)
    return round(statistics.median(times) * 1e6, 1)


def injection_report(score: dict, pattern_us: float) -> list[str]:
    lines = [
        "",
        "## Prompt injection (`src/injection.py`)",
        "",
        "Cases: `data/guardrails/injection_cases.json`, 60 user messages (30 injections from nonprofit-agent, "
        "grant-review-workflow and new; 30 clean, many deliberately hard). The dev split was used to write the "
        "rules and the judge's prompt; the held-out split is judged once (`src/judge_cases.py`). Both were written "
        "by the same author, so held-out is cleaner than dev but not independent.",
        "",
        "| Split | Screen | Injections caught | False alarms (of clean) | Undecided |",
        "|---|---|---|---|---|",
    ]
    for split, s in score.items():
        for screen in ("patterns", "judge", "either"):
            if screen in s:
                c = s[screen]
                lines.append(
                    f"| {split} | {screen} | {c['caught']} of {c['injections']} | {c['false_alarms']} of "
                    f"{c['clean']} | {c['undecided']} |"
                )
    for split, s in score.items():
        lines += ["", f"**{split}** by attack shape (caught / cases):", ""]
        for shape, row in s["by_shape"].items():
            judged = f", judge {row['judge']}" if "judge" in s else ""
            lines.append(f"- {shape}: patterns {row['patterns']}{judged} of {row['cases']}")
        for screen in ("patterns", "judge", "either"):
            if screen in s:
                c = s[screen]
                lines.append(f"- {screen} missed: {', '.join(c['missed']) or 'none'}; false alarms: "
                             f"{', '.join(c['false_alarm_ids']) or 'none'}")  # fmt: skip
        if "judge_meta" in s:
            m = s["judge_meta"]
            lines.append(
                f"- judge: {m['model']}, effort {m['effort']}, prompt {m['prompt_version']}; median "
                f"{s['judge_seconds_median']} s and ${s['judge_cost_per_case_usd']} per case "
                f"(${m['total_cost_usd']} for the split)"
            )
    lines += ["", f"Pattern screen speed: {pattern_us} µs per case (median)."]
    return lines


def report(summary: dict) -> str:
    p, t = summary["pii"], summary["pii_timing"]
    lines = [
        "# Guardrails on their labelled cases",
        "",
        "Generated by `src/measure_guardrails.py`. The cases were written alongside the detectors (no held-out set): "
        "they describe behaviour, they aren't a benchmark.",
        "",
        "## Personal data (`src/pii.py`)",
        "",
        f"{p['cases']} cases ({p['clean_cases']} with no personal data). Recall {p['recall']:.0%}, precision "
        f"{p['precision']:.0%}; {p['clean_cases_flagged']} of {p['clean_cases']} clean cases flagged.",
        "",
        "| Kind | Labelled | Found | Missed | False alarms |",
        "|---|---|---|---|---|",
    ]
    for kind, k in p["per_kind"].items():
        lines.append(f"| {kind} | {k['labelled']} | {k['found']} | {k['missed']} | {k['false_alarms']} |")
    lines += ["", "Missed:", ""]
    lines += [f'- {m["kind"]}: "{m["text"]}" ({m["why"]})' for m in p["misses"]] or ["- none"]
    lines += ["", "False alarms:", ""]
    lines += [f'- {f["kind"]}: "{f["text"]}"' for f in p["false_alarms"]] or ["- none"]
    lines += [
        "",
        f"Speed (median of 20, {t['kilobytes']} KB of text): scanning {t['scan_us_per_kb']} µs per KB, redacting "
        f"{t['redact_us_per_kb']} µs per KB.",
    ]
    lines += injection_report(summary["injection"], summary["pattern_us_per_case"])
    return "\n".join(lines) + "\n"


def main() -> None:
    cases = json.loads((CASES_DIR / "pii_cases.json").read_text(encoding="utf-8"))["cases"]
    summary = {
        "pii": score_pii(cases),
        "pii_timing": time_pii(),
        "injection": score_injection(injection_cases()),
        "pattern_us_per_case": time_patterns(injection_cases()),
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "guardrails.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "guardrails.md").write_text(report(summary), encoding="utf-8")
    print(report(summary))


if __name__ == "__main__":
    main()
