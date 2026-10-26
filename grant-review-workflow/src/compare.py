"""LangGraph or Step Functions? (step 9): the dev packets through both deployed versions of the workflow, with the
same reviewer decisions, compared on correctness, latency per stage, cost and failure handling.

    $env:AWS_PROFILE = "agent-toolkit"
    uv run python src/compare.py            # about $1 in model calls: the 15 dev packets, each run by both versions

One upload runs both. Each packet goes to the intake bucket as <packet>-cmp<run>/..., form.json last: its upload
starts the LangGraph version (the Start function, step 7) and the script starts a Step Functions execution
(GrantReviewStepFunctions, step 9) on the same objects at the same moment, so both meet the same Bedrock conditions.
Packets go one at a time: the account's Lambda concurrency limit is 10, and one packet uses up to four (Start, and the
state machine's three parallel branches).

When a version waits for its reviewer, this script decides, the same way for both: approve, except gp-027, which
gets a round of corrections first (back through the rules), then a decline. The LangGraph version gets the decision
from the Resume function, as the reviewer UI's API sends it; the Step Functions version gets it by SendTaskSuccess
with the task token its execution left in the reviews table. The LangGraph runs leave decided records in the reviewer
UI's list (reviewer "compare-script"); they expire with the data after 30 days.

Measured, per version:
- correctness: each final state scored as the step 5 evals score one (evaluate.case_row, evaluate.score), and
  whether the two versions agree on each packet's route, failed rules, flags and decision
- latency: from the trigger (form.json uploaded; a decision sent) to the version waiting or finished, seen by
  polling every half second; and per stage, from each version's own records (LangGraph's checkpoints, the
  execution's history)
- cost: model calls (the same steps, so about the same), plus Step Functions' state transitions and the Lambda
  invocations each version makes
- failure handling: the retries each needed (Step Functions' Retry on a model step; boto3's own retries)

Results go to results/compare-sfn.json and .md. The account-specific names (bucket, function, state machine) are
looked up at run time, never written down.
"""

import argparse
import datetime as dt
import functools
import json
import statistics
import sys
import time
from collections import Counter

import boto3
from botocore.config import Config

import bedrock
import evaluate
import paths
import workflow
from intake import gold_packet
from state import ApplicationState, ReviewerDecision

RESULTS = paths.ROOT / "results"
REVIEWER = "compare-script"
POLL_SECONDS = 0.5
TIMEOUT_SECONDS = 600  # a run that hasn't waited or finished by then has failed
TRANSITION_USD = 0.000025  # Step Functions Standard, us-east-2: $0.025 per 1,000 state transitions

# The script's decisions: approve, except where a packet's list says otherwise (one decision per wait, in turn).
DECISIONS = {
    "gp-027": [
        {"decision": None, "corrections": {"employees": 12}, "reason": "The return's staff count was misread."},
        {"decision": "decline", "corrections": {"total_revenue_current_year": 99}, "reason": "Revenue is too low."},
    ],
}
APPROVE = {"decision": "approve", "corrections": {}, "reason": "The application meets the fund's priorities."}

# Step Functions states -> the graph's stage names (Join, Routed and Decided have no node in the graph).
SFN_STAGES = {
    "Intake": "intake",
    "Read": "extract+read_proposal+research",
    "Join": "join",
    "CrossCheck": "cross_check",
    "Eligibility": "eligibility",
    "Route": "route",
    "ApplyReview": "apply_review",
    "DraftLetter": "draft_letter",
}


def decisions_for(packet_id: str, now: dt.datetime) -> list[ReviewerDecision]:
    return [ReviewerDecision(**d, reviewer=REVIEWER, decided_at=now) for d in DECISIONS.get(packet_id, [APPROVE] * 3)]


# --- The two versions ------------------------------------------------------------------------------------------
# Each has begin (the trigger after the upload), status ("running", "waiting", "done" or "failed"), decide, and
# result (the final state and what that version records about the run).


class LangGraphOnLambda:
    name = "langgraph"

    def __init__(self, session, resume_function: str):
        self.app = workflow.reader(workflow.dynamodb_checkpointer(session))  # reads the checkpoints, runs nothing
        self.lam = session.client("lambda", config=Config(read_timeout=120, retries={"mode": "standard"}))
        self.resume_function = resume_function
        self.resumes = Counter()

    def begin(self, application_id: str) -> None:
        pass  # the upload's event starts it

    def status(self, application_id: str) -> str:
        snapshot = self.app.get_state(workflow.thread(application_id))
        if not snapshot.values:
            return "running"  # not started yet
        if any(task.interrupts for task in snapshot.tasks):
            return "waiting"
        # Not `next`: it lists only the tasks without saved results, so it's empty for a moment after the parallel
        # steps have all saved theirs and before the next checkpoint (the first comparison run stopped on that).
        return "running" if snapshot.tasks else "done"

    def decide(self, application_id: str, decision: ReviewerDecision) -> None:
        response = self.lam.invoke(
            FunctionName=self.resume_function,
            Payload=json.dumps({"application_id": application_id, "decision": decision.model_dump(mode="json")}),
        )
        if response.get("FunctionError"):
            raise RuntimeError(f"{application_id}: Resume failed ({response['FunctionError']})")
        self.resumes[application_id] += 1

    def result(self, application_id: str) -> tuple[ApplicationState, dict]:
        state = workflow.outcome(self.app, application_id).state
        stages = [  # the parallel stage named as SFN_STAGES names it; the reviewer's wait left out
            {**s, "stage": "+".join(sorted(s["stage"].split("+")))}
            for s in workflow.stage_timings(self.app, application_id)
            if s["stage"] != "human_review"
        ]
        return state, {"stages": stages, "lambda_invocations": 1 + self.resumes[application_id]}  # Start, Resumes


class StepFunctions:
    name = "step_functions"

    def __init__(self, session, state_machine: str, reviews_table: str, bucket: str):
        self.sfn = session.client("stepfunctions")
        self.reviews = session.resource("dynamodb").Table(reviews_table)
        self.state_machine, self.bucket = state_machine, bucket
        self.used_tokens: set[str] = set()

    def _execution(self, application_id: str) -> str:
        return self.state_machine.replace(":stateMachine:", ":execution:") + f":{application_id}"

    def begin(self, application_id: str) -> None:
        self.sfn.start_execution(
            stateMachineArn=self.state_machine,
            name=application_id,  # one execution per application: starting it twice is refused
            input=json.dumps({"application_id": application_id, "bucket": self.bucket}),
        )

    def _token(self, application_id: str) -> str | None:
        item = self.reviews.get_item(Key={"application_id": application_id}, ConsistentRead=True).get("Item")
        return item["task_token"] if item and item["task_token"] not in self.used_tokens else None

    def status(self, application_id: str) -> str:
        execution = self.sfn.describe_execution(executionArn=self._execution(application_id))
        if execution["status"] == "SUCCEEDED":
            return "done"
        if execution["status"] != "RUNNING":
            return "failed"
        return "waiting" if self._token(application_id) else "running"

    def decide(self, application_id: str, decision: ReviewerDecision) -> None:
        token = self._token(application_id)
        self.used_tokens.add(token)
        self.sfn.send_task_success(taskToken=token, output=decision.model_dump_json())

    def result(self, application_id: str) -> tuple[ApplicationState, dict]:
        execution = self.sfn.describe_execution(executionArn=self._execution(application_id))
        pages = self.sfn.get_paginator("get_execution_history").paginate(executionArn=self._execution(application_id))
        events = [e for page in pages for e in page["events"]]
        return ApplicationState.model_validate_json(execution["output"]), history_measures(events)


def history_measures(events: list[dict]) -> dict:
    """From an execution's history: how long each state took (the reviewer's wait left out), the state transitions
    Step Functions bills, the Lambda invocations, and each state's failed tasks (in an execution that succeeded,
    every one of them was retried)."""
    by_id = {e["id"]: e for e in events}

    def state_of(event: dict) -> str | None:
        """The state an event belongs to: walk back to the StateEntered event that led to it."""
        while event:
            if event["type"].endswith("StateEntered"):
                return event["stateEnteredEventDetails"]["name"]
            event = by_id.get(event.get("previousEventId"))
        return None

    entered: dict[str, dt.datetime] = {}
    stages, branches, retries = [], [], Counter()
    for e in events:
        if e["type"].endswith("StateEntered"):
            entered[e["stateEnteredEventDetails"]["name"]] = e["timestamp"]
        elif e["type"].endswith("StateExited"):
            name = e["stateExitedEventDetails"]["name"]
            seconds = round((e["timestamp"] - entered.pop(name)).total_seconds(), 3)
            if name in SFN_STAGES:
                stages.append({"stage": SFN_STAGES[name], "seconds": seconds})
            elif name in ("Extract", "Research", "ReadProposal"):
                branches.append({"stage": name, "seconds": seconds})
        elif e["type"] in ("TaskFailed", "TaskTimedOut", "LambdaFunctionFailed"):
            retries[state_of(e)] += 1
    return {
        "stages": stages,
        "branches": branches,
        "state_transitions": sum(e["type"].endswith("StateEntered") for e in events),
        "lambda_invocations": sum(e["type"] == "TaskScheduled" for e in events),
        "task_failures": dict(retries),
    }


# --- One packet through both -----------------------------------------------------------------------------------


def upload(s3, bucket: str, packet_id: str, application_id: str) -> None:
    """The packet under its application ID, form.json last: its upload event starts the LangGraph version."""
    form, narrative, pdf = gold_packet(packet_id)
    s3.put_object(Bucket=bucket, Key=f"{application_id}/narrative.md", Body=narrative.encode("utf-8"))
    s3.upload_file(str(pdf), bucket, f"{application_id}/return.pdf")
    s3.put_object(Bucket=bucket, Key=f"{application_id}/form.json", Body=json.dumps(form).encode("utf-8"))


def wait_for(versions, application_id: str, sent: float, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Poll every version until it waits for its reviewer, finishes or fails; returns each one's status and how long
    it took from `sent` (the trigger)."""
    pending, seen = {v.name: v for v in versions}, {}
    while pending:
        for name, version in list(pending.items()):
            status = version.status(application_id)
            if status != "running":
                seen[name] = {"status": status, "seconds": round(clock() - sent, 2)}
                del pending[name]
        if pending and clock() - sent > TIMEOUT_SECONDS:
            seen.update({name: {"status": "failed", "seconds": None, "timed_out": True} for name in pending})
            break
        if pending:
            sleep(POLL_SECONDS)
    return seen


def run_packet(versions, packet_id: str, application_id: str, start_upload, clock=time.monotonic, sleep=time.sleep):
    """One packet through every version at once, then the same decisions for each, round by round."""
    start_upload()
    sent = clock()
    for v in versions:
        v.begin(application_id)
    rounds = [wait_for(versions, application_id, sent, clock, sleep)]
    for decision in decisions_for(packet_id, dt.datetime.now(dt.UTC)):
        waiting = [v for v in versions if rounds[-1].get(v.name, {}).get("status") == "waiting"]
        if not waiting:
            break
        sent = clock()
        for v in waiting:
            v.decide(application_id, decision)
        rounds.append(wait_for(waiting, application_id, sent, clock, sleep))
    out = {"packet_id": packet_id, "application_id": application_id}
    for v in versions:
        mine = [r[v.name] for r in rounds if v.name in r]
        entry = {"status": mine[-1]["status"], "rounds": mine}
        if entry["status"] == "done":
            state, measures = v.result(application_id)
            entry.update(
                row=evaluate.case_row(packet_id, 1, state),
                decision=state.review.decision if state.review else None,
                letter=state.letter is not None,
                system_seconds=round(sum(r["seconds"] for r in mine), 2),  # the reviewer's time isn't in it
                **measures,
            )
        out[v.name] = entry
    return out


# --- Comparing --------------------------------------------------------------------------------------------------


def agreement(packets: list[dict], a: str = "langgraph", b: str = "step_functions") -> dict:
    """Packets where both versions finished and reached the same outcome; the differences, by packet."""
    both = [p for p in packets if p[a]["status"] == p[b]["status"] == "done"]

    def outcome(entry):
        r = entry["row"]
        return {
            "route": r["route"],
            "failed_rules": r["failed_rules"],
            "flags": r["flags"],
            "decision": entry["decision"],
        }

    differences = {
        p["packet_id"]: {
            k: [outcome(p[a])[k], outcome(p[b])[k]] for k in outcome(p[a]) if outcome(p[a])[k] != outcome(p[b])[k]
        }
        for p in both
    }
    return {
        "both_finished": len(both),
        "same_outcome": sum(not d for d in differences.values()),
        "differences": {k: v for k, v in differences.items() if v},
    }


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def summarize(packets: list[dict], name: str) -> dict:
    done = [p[name] for p in packets if p[name]["status"] == "done"]
    stage_seconds: dict[str, list[float]] = {}
    for entry in done:
        for s in entry["stages"]:
            stage_seconds.setdefault(s["stage"], []).append(s["seconds"])
    out = {
        "finished": len(done),
        "failed": [p["packet_id"] for p in packets if p[name]["status"] != "done"],
        "scores": evaluate.score([e["row"] | {"seconds": e["system_seconds"]} for e in done]) if done else None,
        "to_first_stop_seconds": {"median": _median([e["rounds"][0]["seconds"] for e in done])},
        "after_decision_seconds": {"median": _median([r["seconds"] for e in done for r in e["rounds"][1:]])},
        "system_seconds": {"median": _median([e["system_seconds"] for e in done])},
        "stage_seconds_median": {stage: _median(v) for stage, v in stage_seconds.items()},
        "model_cost_usd": round(sum(e["row"]["cost_usd"] for e in done), 4),
        "lambda_invocations": sum(e["lambda_invocations"] for e in done),
        "calls_with_sdk_retries": sum(c["sdk_retries"] > 0 for e in done for c in e["row"]["calls"]),
    }
    if name == "step_functions":
        transitions = sum(e["state_transitions"] for e in done)
        out["state_transitions"] = transitions
        out["state_transitions_usd"] = round(transitions * TRANSITION_USD, 4)
        out["task_failures"] = dict(sum((Counter(e["task_failures"]) for e in done), Counter()))
    return out


def report(result: dict) -> str:
    lg, sf = result["langgraph"], result["step_functions"]
    agree = result["agreement"]

    def score(version, metric):
        return version["scores"][metric] if version["scores"] else None

    rows = [
        ("Finished", lg["finished"], sf["finished"]),
        ("Wrong automatic declines", score(lg, "wrong_automatic_declines"), score(sf, "wrong_automatic_declines")),
        ("Route accuracy", score(lg, "route_accuracy"), score(sf, "route_accuracy")),
        ("Flags exactly as labeled", score(lg, "flags_exact"), score(sf, "flags_exact")),
        ("Extraction field accuracy", score(lg, "extraction_field_accuracy"), score(sf, "extraction_field_accuracy")),
        ("Letters with problems", score(lg, "letters_with_problems"), score(sf, "letters_with_problems")),
        (
            "Upload to first stop, median s",
            lg["to_first_stop_seconds"]["median"],
            sf["to_first_stop_seconds"]["median"],
        ),
        (
            "Decision to finish, median s",
            lg["after_decision_seconds"]["median"],
            sf["after_decision_seconds"]["median"],
        ),
        ("System time per application, median s", lg["system_seconds"]["median"], sf["system_seconds"]["median"]),
        ("Model calls, $", lg["model_cost_usd"], sf["model_cost_usd"]),
        ("Lambda invocations", lg["lambda_invocations"], sf["lambda_invocations"]),
        ("Model calls with boto3 retries", lg["calls_with_sdk_retries"], sf["calls_with_sdk_retries"]),
    ]
    lines = [
        f"# LangGraph vs Step Functions: dev packets ({result['date']})",
        "",
        f"Run `{result['run_id']}`, {len(result['packets'])} packets, the same decisions for both. "
        f"Both versions reached the same outcome on {agree['same_outcome']} of the {agree['both_finished']} "
        "packets both finished.",
        "",
        "| | LangGraph on Lambda | Step Functions |",
        "|---|---|---|",
        *(f"| {label} | {a} | {b} |" for label, a, b in rows),
        "",
        f"Step Functions also made {sf.get('state_transitions')} state transitions "
        f"(${sf.get('state_transitions_usd')}); failed tasks, all retried: {sf.get('task_failures') or 'none'}.",
        "",
        "## Median seconds per stage",
        "",
        "| Stage | LangGraph | Step Functions |",
        "|---|---|---|",
    ]
    stages = list(dict.fromkeys([*lg["stage_seconds_median"], *sf["stage_seconds_median"]]))
    lines += [
        f"| {s} | {lg['stage_seconds_median'].get(s, '')} | {sf['stage_seconds_median'].get(s, '')} |" for s in stages
    ]
    if agree["differences"]:
        lines += ["", "## Where they differed", ""]
        lines += [f"- {packet}: {diff}" for packet, diff in agree["differences"].items()]
    return "\n".join(lines) + "\n"


# --- Running it -------------------------------------------------------------------------------------------------


def _outputs(cfn, stack: str) -> dict:
    return {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--packets", nargs="*", help="dev packet IDs (default: all 15)")
    args = parser.parse_args()
    packets = args.packets or evaluate.packets("dev")
    if not set(packets) <= set(evaluate.packets("dev")):
        parser.error("only dev packets: the test and holdout splits are held out")

    session = boto3.Session(region_name=bedrock.REGION)
    cfn = session.client("cloudformation")
    bucket = f"grant-review-intake-{session.client('sts').get_caller_identity()['Account']}"
    sfn_out = _outputs(cfn, "GrantReviewStepFunctions")
    versions = [
        LangGraphOnLambda(session, _outputs(cfn, "GrantReviewWorkflow")["ResumeFunctionName"]),
        StepFunctions(session, sfn_out["StateMachineArn"], sfn_out["ReviewsTable"], bucket),
    ]
    s3 = session.client("s3")
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    results = []
    RESULTS.mkdir(exist_ok=True)
    partial = RESULTS / "compare-sfn.partial.json"  # after every packet, so a crash keeps what was measured
    for packet_id in packets:
        application_id = f"{packet_id}-cmp{run_id}"
        result = run_packet(
            versions, packet_id, application_id, functools.partial(upload, s3, bucket, packet_id, application_id)
        )
        results.append(result)
        notes = ", ".join(
            f"{v.name} {result[v.name]['status']} {result[v.name].get('system_seconds', '')} s" for v in versions
        )
        print(f"{packet_id}: {notes}", flush=True)
        partial.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")

    out = {
        "run_id": run_id,
        "date": dt.date.today().isoformat(),
        "fingerprint": evaluate.fingerprint(),
        "agreement": agreement(results),
        "langgraph": summarize(results, "langgraph"),
        "step_functions": summarize(results, "step_functions"),
        "packets": results,
    }
    (RESULTS / "compare-sfn.json").write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
    (RESULTS / "compare-sfn.md").write_text(report(out), encoding="utf-8")
    partial.unlink()
    print(report(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
