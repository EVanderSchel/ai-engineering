"""The drift monitor (step 9 stage 4): free checks on every live answer, and alert rules whose expected levels come
from the eval canary's baselines.

    uv run python src/drift.py      # regenerates observability/drift.yml from results/canary/baselines/

Plain language: the canary (src/canary.py) only checks changes we make. Drift is change we don't make: a provider
updates a model behind the same name, a fallback starts answering more of the traffic, apps start sending different
questions. So the gateway checks every answer as it goes out, for free:
- **format**: does it match the registered prompt's `answer_format` (ntee-classify@v1: "B, O, N")? An answer that
  explains itself first, or rambles, is "bad" even if a person would find the letters in it;
- **finish**: did it end normally, hit max_tokens ("length"), or get withheld ("content_filter")?

They're counted in the metric gateway_answers_total{route, prompt, finish, format} and logged on the request's line
(never the answer itself). The canary measured what's normal for each (route, prompt) it checks, so this script turns
its baselines into Prometheus recording rules (the expected rates) and alert rules that fire when a live rate is
well above its expected one for long enough. Those rules are generated, not written by hand, so they can't drift from
the baselines: tests/test_drift.py fails if the file isn't current.
"""

import json
import pathlib

import prompts
from paths import RESULTS_DIR, ROOT

RULES_FILE = ROOT / "observability" / "drift.yml"
TOLERANCE = 0.10  # how far a live rate may rise above the canary's before the alert fires
MIN_ANSWERS = 20  # in the hour: below this a few odd answers are noise, not drift
WINDOW, FOR = "1h", "15m"
# The judge's alert (stage 4b): it sees a sample, so its window is longer and it needs fewer answers.
SCORE_DROP = 0.5  # how far the live mean grade (1-5) may fall below the canary's before the alert fires
MIN_GRADES = 20  # graded answers in the window
JUDGE_WINDOW, JUDGE_FOR = "6h", "30m"


def answer_signals(text: str, finish: str, prompt: prompts.Prompt | None) -> dict:
    """The log line's drift fields for one finished answer. A withheld answer isn't format-checked: what was sent
    is the guardrails' notice, not the model's answer."""
    well_formed = None if prompt is None or finish == "content_filter" else prompt.well_formed(text)
    return {"finish": finish, "answer_format": {True: "ok", False: "bad", None: None}[well_formed]}


def references(
    baselines_dir: pathlib.Path = RESULTS_DIR / "canary" / "baselines",
    judges_dir: pathlib.Path = RESULTS_DIR / "canary" / "judge",
) -> list[dict]:
    """For each canary check with a prompt: the share of its baseline answers that were badly formed, and the share
    cut off by max_tokens. The prompt version is the baseline's, which is the one the route serves while the
    baseline is current (tests/test_canary.py checks that)."""
    import config  # here, not at the top: the gateway imports this module, and the canary imports the gateway
    from canary import load_checks, subject

    routes, registry = config.load_routes(), prompts.Registry()
    out = []
    for name, check in load_checks().checks.items():
        if not check.prompt:
            continue  # nothing to check the format against
        baseline = json.loads((baselines_dir / f"{name}.json").read_text(encoding="utf-8"))
        prompt = registry.resolve(subject(check, routes, registry)["prompt"]["ref"])
        answered = [i for i in baseline["items"] if "error" not in i]
        signals = [answer_signals(i["answer"], i.get("finish", "stop"), prompt) for i in answered]
        checked = [s for s in signals if s["answer_format"] is not None]
        ref = {
            "check": name, "route": check.route, "prompt": prompt.ref, "answers": len(answered),
            "bad_format": round(sum(s["answer_format"] == "bad" for s in checked) / len(checked), 4),
            "cut_off": round(sum(s["finish"] == "length" for s in signals) / len(signals), 4),
        }  # fmt: skip
        # The judge's mean grade of these same answers (drift_judge.py, paid, made once), if it has been made and
        # grades the version the baseline used.
        judged = judges_dir / f"{name}.json"
        if judged.exists():
            record = json.loads(judged.read_text(encoding="utf-8"))
            if record["prompt"] == prompt.ref and record["mean"] is not None:
                ref["judge_mean"] = record["mean"]
        out.append(ref)
    return out


def ratio(matcher: str) -> str:
    return (
        f'sum by (route, prompt) (rate(gateway_answers_total{{{matcher}}}[{WINDOW}]))\n'
        f'  / sum by (route, prompt) (rate(gateway_answers_total{{format!=""}}[{WINDOW}]))'
    )  # fmt: skip


def enough() -> str:
    return f'sum by (route, prompt) (increase(gateway_answers_total{{format!=""}}[{WINDOW}])) >= {MIN_ANSWERS}'


def rules(refs: list[dict]) -> dict:
    recorded = []
    for ref in refs:
        labels = {"route": ref["route"], "prompt": ref["prompt"]}
        for record, key in (("answer_bad_format_ratio", "bad_format"), ("answer_cut_off_ratio", "cut_off"),
                            ("judge_score_mean", "judge_mean")):  # fmt: skip
            if key in ref:
                recorded.append({"record": f"gateway:{record}:canary", "expr": f"vector({ref[key]})", "labels": labels})
    judge_mean = (
        f"sum by (route, prompt) (rate(gateway_judge_scores_sum[{JUDGE_WINDOW}]))\n"
        f"  / sum by (route, prompt) (rate(gateway_judge_scores_count[{JUDGE_WINDOW}]))"
    )
    graded_enough = f"sum by (route, prompt) (increase(gateway_judge_scores_count[{JUDGE_WINDOW}])) >= {MIN_GRADES}"
    alerts = [
        {
            "alert": "GatewayAnswerFormatDrift",
            "expr": f"(\n{ratio('format="bad"')}\n)\n> on (route, prompt) (gateway:answer_bad_format_ratio:canary + "
            f"{TOLERANCE})\nand on (route, prompt) {enough()}\n",
            "for": FOR,
            "labels": {"severity": "ticket"},
            "annotations": {
                "summary": "{{ $value | humanizePercentage }} of answers on route {{ $labels.route }} with prompt "
                "{{ $labels.prompt }} don't match its answer format",
                "look_at": "gateway_answers_total by finish, gateway_fallbacks_total (a fallback model answering?), "
                "and recent changes to the provider's model; then run the canary (src/canary.py)",
            },
        },
        {
            "alert": "GatewayAnswersCutOff",
            "expr": f"(\n{ratio('format!="",finish="length"')}\n)\n> on (route, prompt) "
            f"(gateway:answer_cut_off_ratio:canary + {TOLERANCE})\nand on (route, prompt) {enough()}\n",
            "for": FOR,
            "labels": {"severity": "ticket"},
            "annotations": {
                "summary": "{{ $value | humanizePercentage }} of answers on route {{ $labels.route }} with prompt "
                "{{ $labels.prompt }} were cut off by max_tokens",
                "look_at": "whether the model now thinks or explains before answering (output tokens per request), "
                "or callers lowered max_tokens",
            },
        },
    ]
    if any("judge_mean" in ref for ref in refs):
        alerts.append({
            "alert": "GatewayJudgeScoreDrop",
            "expr": f"(\n{judge_mean}\n)\n< on (route, prompt) (gateway:judge_score_mean:canary - {SCORE_DROP})\n"
            f"and on (route, prompt) {graded_enough}\n",
            "for": JUDGE_FOR,
            "labels": {"severity": "ticket"},
            "annotations": {
                "summary": "the drift judge's mean grade on route {{ $labels.route }} with prompt {{ $labels.prompt }} "
                "is {{ $value | printf \"%.2f\" }} of 5, well below the canary's",
                "look_at": "which model answered (gateway_fallbacks_total, the response model), recent provider "
                "changes, and the canary (src/canary.py); the judge sees a sample, so check gateway_judge_runs_total",
            },
        })  # fmt: skip
    return {"groups": [{"name": "gateway-drift", "rules": recorded + alerts}]}


HEADER = f"""\
# GENERATED by src/drift.py from the eval canary's baselines (results/canary/baselines/): don't edit, regenerate.
#
# The drift monitor's rules (step 9 stage 4). Recording rules hold each (route, prompt)'s expected rates, as the
# canary measured them; the alerts fire when a live rate over the last {WINDOW} is more than {TOLERANCE:.0%} above
# its expected one, with at least {MIN_ANSWERS} answers in that hour, for {FOR}. The judge's alert (once
# src/drift_judge.py has graded the canary's answers) fires when the mean grade over {JUDGE_WINDOW} is more than
# {SCORE_DROP} below the canary's, with at least {MIN_GRADES} grades, for {JUDGE_FOR}. Unit-tested with promtool:
# observability/drift_test.yml.
"""


def render(refs: list[dict]) -> str:
    """The rules as YAML, with multi-line expressions as block strings, so they read like alerts.yml."""
    import yaml

    class Block(yaml.SafeDumper):
        def ignore_aliases(self, data):  # write each rule's labels out, not as &id001 / *id001
            return True

    Block.add_representer(
        str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)
    )
    return HEADER + yaml.dump(rules(refs), Dumper=Block, sort_keys=False, width=120)


def main() -> None:
    refs = references()
    RULES_FILE.write_text(render(refs), encoding="utf-8", newline="\n")
    for ref in refs:
        rates = f"badly formed {ref['bad_format']:.1%}, cut off {ref['cut_off']:.1%}"
        print(f"{ref['route']:<6} {ref['prompt']:<17} {rates} (canary check {ref['check']}, {ref['answers']} answers)")
    print(f"wrote {RULES_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
