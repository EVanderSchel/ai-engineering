"""The evals (step 5): the whole workflow on a gold-set split, repeated, scored against the labels.

    $env:AWS_PROFILE = "agent-toolkit"
    uv run python src/evaluate.py --split dev --reps 3          # tune on this as often as needed
    uv run python src/evaluate.py --split test --reps 3 --final # once, at the end: the held-out result

Every application runs end to end through the real workflow (checkpoints in memory: durability is tested
separately, in step 4). Metrics, from "How it's measured" in docs/design.md:

- wrong automatic declines (must be 0), routing and failed-rules accuracy, reviewer workload
- flag recall and precision per kind, and false flags on the near misses (claims 10-20% off: not flagged)
- injection resistance: caught, false alarms, and whether an injected application's outcome is otherwise intact
- extraction field accuracy, focus-area accuracy, stated-figure accuracy
- rubric consistency across repeats (the rubric isn't labeled, so no accuracy is claimed)
- cost, latency, and boto3's own retries

Results go to results/eval-<split>.json and .md, stamped with a fingerprint of the prompts, models and fund rules
that produced them; a free CI test fails when the committed dev results no longer match the code.
"""

import argparse
import datetime as dt
import hashlib
import json
import statistics
import sys
import time
from collections import Counter, defaultdict

from botocore.exceptions import ClientError

import bedrock
import draft_letter
import extract
import fund_rules
import paths
import read_proposal
import research
import workflow
from intake import gold_packet, intake

RESULTS = paths.ROOT / "results"
PLANTED_KINDS = ["budget_mismatch", "staff_mismatch", "name_mismatch", "out_of_focus", "injection"]
# Flags the gold set never expects: any of these is a false positive by construction.
UNEXPECTED_KINDS = ["not_found", "figures_disagree", "arithmetic", "unverified_reading"]


def fingerprint() -> dict:
    """What produced a set of results: every prompt's text, every step's model, and the fund's rules. If any of
    these change, earlier results no longer describe the workflow."""
    prompts = {
        "extract": extract.SYSTEM,
        "research_judge": research.JUDGE_SYSTEM,
        "research_agent": research.AGENT_SYSTEM,
        "read_proposal": read_proposal.SYSTEM,
        "draft_letter": draft_letter.SYSTEM,
    }
    parts = {
        "prompts": {name: hashlib.sha256(text.encode()).hexdigest()[:16] for name, text in prompts.items()},
        "models": {step: bedrock.MODELS[model].model_id for step, model in workflow.STEP_MODELS.items()},
        "rules": {
            "rules": fund_rules.RULES,
            "revenue": [fund_rules.REVENUE_MIN, fund_rules.REVENUE_MAX],
            "request": [fund_rules.REQUEST_MIN, fund_rules.REQUEST_MAX, fund_rules.MAX_SHARE_OF_EXPENSES],
            "oldest_period_end": fund_rules.OLDEST_TAX_PERIOD_END.isoformat(),
            "claim_tolerance": fund_rules.CLAIM_TOLERANCE,
        },
    }
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]
    return {"digest": digest, **parts}


def packets(split: str) -> list[str]:
    return sorted(
        p.name
        for p in paths.GOLD_DIR.glob("gp-*")
        if json.loads((p / "label.json").read_text(encoding="utf-8"))["split"] == split
    )


def _gold(packet_id: str) -> tuple[dict, dict, dict]:
    folder = paths.GOLD_DIR / packet_id
    label = json.loads((folder / "label.json").read_text(encoding="utf-8"))
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    key = json.loads((paths.FILINGS_DIR / f"{spec['object_id']}.json").read_text(encoding="utf-8"))["fields"]
    return label, spec, key


# AWS errors that go away on their own: wait, then continue the run from its checkpoint. Anything else (access
# denied, a validation error) won't be fixed by waiting, so it stops the eval.
TRANSIENT = {
    "ServiceUnavailableException",
    "ThrottlingException",
    "ModelTimeoutException",
    "ModelNotReadyException",
    "InternalServerException",
}
WAITS_SECONDS = [30, 90]  # after the first failure, then the second; a third failure records an error row


class CaseFailed(RuntimeError):
    """An application failed on transient errors even after waiting and continuing."""


def _run_with_patience(app, application, sleep=time.sleep):
    """Start the application; on a transient AWS error, wait and continue its thread from the last checkpoint, so
    steps that already succeeded aren't paid for again. Returns (final state, errors survived)."""
    errors = []
    try:
        return workflow.start(app, application).state, errors
    except ClientError as e:
        if e.response["Error"]["Code"] not in TRANSIENT:
            raise
        errors.append(e.response["Error"]["Code"])
    for wait in WAITS_SECONDS:
        sleep(wait)
        try:
            return workflow.carry_on(app, application.application_id).state, errors
        except ClientError as e:
            if e.response["Error"]["Code"] not in TRANSIENT:
                raise
            errors.append(e.response["Error"]["Code"])
    raise CaseFailed(f"{application.application_id}: {', '.join(errors)}")


def run_case(app, packet_id: str, rep: int, run_id: str, sleep=time.sleep) -> dict:
    """One application end to end, with everything the metrics need."""
    form, narrative, pdf = gold_packet(packet_id)
    started = time.perf_counter()
    application_id = f"{packet_id}-{run_id}-r{rep}"
    state, survived = _run_with_patience(app, intake(form, narrative, pdf, application_id=application_id), sleep)
    seconds = time.perf_counter() - started
    return {
        **case_row(packet_id, rep, state),
        "seconds": round(seconds, 2),
        "stages": workflow.stage_timings(app, application_id),
        "transient_errors_survived": survived,
    }


def case_row(packet_id: str, rep: int, state) -> dict:
    """What score() needs from a finished application's state, however it was run (step 9 scores the Step Functions
    version with it too)."""
    _, _, key = _gold(packet_id)
    proposal = state.proposal
    return {
        "packet_id": packet_id,
        "rep": rep,
        "route": state.route,
        "failed_rules": state.eligibility.failed_rules,
        "flags": sorted(f.kind for f in state.flags),
        "wrong_fields": extract.compare(state.extraction, key),
        "return_name_read": extract.name_read_right(state.extraction, key),
        # What the name check had before step 6.5: ProPublica's name alone (the old rule flagged when this is False).
        "public_record_name_matches": state.research.name_matches_form if state.research else None,
        "focus_area": proposal.focus_area,
        "claims": proposal.claims.model_dump(),
        "rubric": {s.criterion: s.score for s in proposal.rubric},
        "letter_problems": state.letter_problems,
        "cost_usd": round(state.cost_usd, 5),
        "calls": [
            {
                "step": c.step,
                "model": c.model,
                "latency_ms": c.latency_ms,
                "wall_ms": c.wall_ms,
                "sdk_retries": c.sdk_retries,
                "attempts": c.attempts,
            }
            for c in state.calls
        ],
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def score(rows: list[dict]) -> dict:
    """Metrics over a set of results (one repetition, or all of them pooled)."""
    labels = {p: _gold(p) for p in {r["packet_id"] for r in rows}}
    n = len(rows)
    out: dict = {"applications": n}

    def label(r):
        return labels[r["packet_id"]][0]

    def spec(r):
        return labels[r["packet_id"]][1]

    out["wrong_automatic_declines"] = sum(r["route"] == "decline_ineligible" != label(r)["route"] for r in rows)
    out["declines_sent_to_review"] = sum(r["route"] == "review" != label(r)["route"] for r in rows)
    out["route_accuracy"] = _ratio(sum(r["route"] == label(r)["route"] for r in rows), n)
    out["failed_rules_accuracy"] = _ratio(sum(r["failed_rules"] == label(r)["failed_rules"] for r in rows), n)
    out["flags_exact"] = _ratio(sum(r["flags"] == label(r)["flags"] for r in rows), n)
    out["reviewer_workload"] = _ratio(sum(r["route"] == "review" for r in rows), n)

    per_kind = {}
    for kind in PLANTED_KINDS + UNEXPECTED_KINDS:
        tp = sum(kind in r["flags"] and kind in label(r)["flags"] for r in rows)
        fp = sum(kind in r["flags"] and kind not in label(r)["flags"] for r in rows)
        fn = sum(kind not in r["flags"] and kind in label(r)["flags"] for r in rows)
        if tp or fp or fn:
            per_kind[kind] = {
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "recall": _ratio(tp, tp + fn),
                "precision": _ratio(tp, tp + fp),
            }
    out["flags"] = per_kind

    near = [r for r in rows if label(r)["near_miss"]]
    out["near_misses_flagged"] = sum(any(k in r["flags"] for k in ("budget_mismatch", "staff_mismatch")) for r in near)
    out["near_misses"] = len(near)

    injected = [r for r in rows if label(r)["injection"]]
    clean = [r for r in rows if not label(r)["injection"]]
    without = lambda flags: [f for f in flags if f != "injection"]  # noqa: E731
    out["injections"] = len(injected)
    out["injections_caught"] = sum("injection" in r["flags"] for r in injected)
    out["injection_false_alarms"] = sum("injection" in r["flags"] for r in clean)
    out["injected_outcome_intact"] = sum(
        r["route"] == label(r)["route"]
        and r["failed_rules"] == label(r)["failed_rules"]
        and without(r["flags"]) == without(label(r)["flags"])
        for r in injected
    )

    fields = len(extract.COMPARED)
    out["extraction_field_accuracy"] = _ratio(n * fields - sum(len(r["wrong_fields"]) for r in rows), n * fields)
    named = [r for r in rows if "return_name_read" in r]  # scored from step 6.5 on
    out["return_name_accuracy"] = _ratio(sum(r["return_name_read"] for r in named), len(named))
    out["focus_accuracy"] = _ratio(sum(r["focus_area"] == label(r)["focus_area"] for r in rows), n)
    out["claims_accuracy"] = _ratio(sum(r["claims"] == spec(r)["claims"] for r in rows), n)
    out["letters_with_problems"] = sum(bool(r["letter_problems"]) for r in rows)

    costs = sorted(r["cost_usd"] for r in rows)
    times = sorted(r["seconds"] for r in rows)
    out["cost_per_application"] = {"mean": round(statistics.mean(costs), 4), "max": costs[-1]}
    out["seconds_per_application"] = {"median": times[n // 2], "p90": times[min(n - 1, int(n * 0.9))]}
    calls = [c for r in rows for c in r["calls"]]
    out["model_calls"] = len(calls)
    out["calls_with_sdk_retries"] = sum(c["sdk_retries"] > 0 for c in calls)
    out["calls_with_schema_retries"] = sum(c["attempts"] > 1 for c in calls)
    return out


def rubric_consistency(rows: list[dict]) -> dict:
    """How much each packet's rubric scores move between repeats, and how they spread overall."""
    by_packet = defaultdict(lambda: defaultdict(list))
    for r in rows:
        for criterion, value in r["rubric"].items():
            by_packet[r["packet_id"]][criterion].append(value)
    out = {}
    for criterion in ("clear_outcomes", "budget_matches_request", "feasible_for_size"):
        ranges = [max(v[criterion]) - min(v[criterion]) for v in by_packet.values() if v[criterion]]
        scores = [s for v in by_packet.values() for s in v[criterion]]
        out[criterion] = {
            "same_score_every_repeat": _ratio(sum(x == 0 for x in ranges), len(ranges)),
            "max_change_between_repeats": max(ranges) if ranges else None,
            "distribution": dict(sorted(Counter(scores).items())),
        }
    return out


def evaluate(app, split: str, reps: int) -> dict:
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    rows, errors = [], []
    for rep in range(1, reps + 1):
        for packet_id in packets(split):
            try:
                row = run_case(app, packet_id, rep, run_id)
            except CaseFailed as e:
                errors.append({"packet_id": packet_id, "rep": rep, "error": str(e)})
                print(f"rep {rep} {packet_id}: FAILED after waiting ({e})", flush=True)
                continue
            rows.append(row)
            label = _gold(packet_id)[0]
            ok = row["route"] == label["route"] and row["flags"] == label["flags"] and not row["wrong_fields"]
            note = (
                "ok"
                if ok
                else f"route {row['route']}, flags {row['flags']} vs {label['flags']}, misread {row['wrong_fields']}"
            )
            print(f"rep {rep} {packet_id}: {note} (${row['cost_usd']:.4f}, {row['seconds']} s)", flush=True)
    return {
        "split": split,
        "reps": reps,
        "run_id": run_id,
        "date": dt.date.today().isoformat(),
        "fingerprint": fingerprint(),
        "pooled": score(rows),
        "per_rep": [score([r for r in rows if r["rep"] == rep]) for rep in range(1, reps + 1)],
        "rubric_consistency": rubric_consistency(rows),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "failed_cases": errors,
        "transient_errors_survived": sum(len(r["transient_errors_survived"]) for r in rows),
        "rows": rows,
    }


def report(result: dict) -> str:
    """A short Markdown summary of a result file."""
    p, reps = result["pooled"], result["per_rep"]

    def spread(metric):
        values = [r[metric] for r in reps]
        return f"{min(values)}-{max(values)}" if min(values) != max(values) else f"{values[0]}"

    lines = [
        f"# Eval: {result['split']} split, {result['reps']} repetitions ({result['date']})",
        "",
        f"Fingerprint `{result['fingerprint']['digest']}`; {p['applications']} application runs; "
        f"${result['total_cost_usd']:.2f} in model calls. Transient AWS errors survived by waiting and continuing: "
        f"{result['transient_errors_survived']}; cases that still failed: {len(result['failed_cases'])}.",
        "",
        "| Metric | Pooled | Per repetition |",
        "|---|---|---|",
        f"| Wrong automatic declines | **{p['wrong_automatic_declines']}** | {spread('wrong_automatic_declines')} |",
        f"| Declines sent to review instead | {p['declines_sent_to_review']} | {spread('declines_sent_to_review')} |",
        f"| Route accuracy | {p['route_accuracy']} | {spread('route_accuracy')} |",
        f"| Failed-rules accuracy | {p['failed_rules_accuracy']} | {spread('failed_rules_accuracy')} |",
        f"| Flags exactly as labeled | {p['flags_exact']} | {spread('flags_exact')} |",
        f"| Extraction field accuracy | {p['extraction_field_accuracy']} | {spread('extraction_field_accuracy')} |",
        f"| Return name read (box C) | {p['return_name_accuracy']} | {spread('return_name_accuracy')} |",
        f"| Focus-area accuracy | {p['focus_accuracy']} | {spread('focus_accuracy')} |",
        f"| Stated-figure accuracy | {p['claims_accuracy']} | {spread('claims_accuracy')} |",
        f"| Injections caught | {p['injections_caught']}/{p['injections']} | {spread('injections_caught')} |",
        f"| Injected outcome intact | {p['injected_outcome_intact']}/{p['injections']} "
        f"| {spread('injected_outcome_intact')} |",
        f"| Injection false alarms | {p['injection_false_alarms']} | {spread('injection_false_alarms')} |",
        f"| Near misses flagged (should be 0) | {p['near_misses_flagged']}/{p['near_misses']} "
        f"| {spread('near_misses_flagged')} |",
        f"| Letters with problems | {p['letters_with_problems']} | {spread('letters_with_problems')} |",
        f"| Reviewer workload | {p['reviewer_workload']} | {spread('reviewer_workload')} |",
        f"| Cost per application (mean) | ${p['cost_per_application']['mean']} | |",
        f"| Seconds per application (median, p90) | {p['seconds_per_application']['median']}, "
        f"{p['seconds_per_application']['p90']} | |",
        f"| Model calls with boto3 retries | {p['calls_with_sdk_retries']}/{p['model_calls']} | |",
        "",
        "**Flags by kind** (pooled):",
        "",
        "| Kind | Recall | Precision | Missed | False |",
        "|---|---|---|---|---|",
        *[f"| {kind} | {v['recall']} | {v['precision']} | {v['fn']} | {v['fp']} |" for kind, v in p["flags"].items()],
        "",
        "**Rubric consistency** (not labeled, so no accuracy):",
        "",
        "| Criterion | Same score every repeat | Largest change | Scores given |",
        "|---|---|---|---|",
        *[
            f"| {c} | {v['same_score_every_repeat']} | {v['max_change_between_repeats']} | {v['distribution']} |"
            for c, v in result["rubric_consistency"].items()
        ],
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the workflow on a gold-set split and score it.")
    parser.add_argument("--split", choices=["dev", "test", "holdout"], required=True)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--final", action="store_true", help="required for the test and holdout splits (run once)")
    args = parser.parse_args()
    out = RESULTS / f"eval-{args.split}.json"
    if args.split in ("test", "holdout"):
        if not args.final:
            parser.error(f"the {args.split} split is a held-out result: run it once, at the end, with --final")
        if out.exists():
            parser.error(
                f"{out.name} exists: the {args.split} split has been scored, and rerunning it would make it a dev set"
            )
    app = workflow.build(workflow.bedrock_steps(bedrock.make_client()), checkpointer=workflow.memory_checkpointer())
    try:
        result = evaluate(app, args.split, args.reps)
    except ClientError as e:
        print(f"AWS error: {e}", file=sys.stderr)
        return 1
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(report(result), encoding="utf-8")
    print()
    print(report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
