"""The workflow's Lambda entry points (step 7): start an application when it's uploaded, resume it with the
reviewer's decision.

An application arrives in the intake bucket as three objects under its ID, form.json uploaded last:

    <application_id>/narrative.md
    <application_id>/return.pdf
    <application_id>/form.json      <- its upload event starts the run, so the packet is complete by then

`start` receives that S3 "Object Created" event from EventBridge, downloads the packet to /tmp, runs intake and
the workflow until it finishes or pauses for its reviewer, with checkpoints in DynamoDB. Lambda retries a failed
asynchronous invocation; a retry finds the thread already saved and continues it from its last checkpoint
(workflow.carry_on), so steps that had finished aren't paid for twice.

`resume` is invoked by the reviewer UI's API (src/api.py) with the application ID and a ReviewerDecision, validated
before the graph sees it; or with "finish": true, to complete a run that took a decision but stopped before its
letter. Both return a summary, never the narrative: the logs stay free of applicant
text (docs/data-handling.md). After each run both write the application's status record (applications.py), which
the reviewer UI's queue lists (step 8).

Each summary is logged through `logging` with its fields as extras: Lambda's JSON log format makes every line a
JSON object with the request ID, and the summary's trace_id links it to the run's trace in X-Ray (stage 5).

The Bedrock client, S3 client and compiled graph are created once per Lambda container and reused while it's warm.
"""

import datetime as dt
import functools
import json
import logging
import os
import pathlib

import boto3

import applications
import bedrock
import tracing
import workflow
from intake import IntakeError, intake
from state import ReviewerDecision

PACKET = ("narrative.md", "return.pdf", "form.json")
WORK_DIR = pathlib.Path(os.environ.get("GRANT_REVIEW_WORK_DIR", "/tmp/applications"))  # Lambda: only /tmp is writable
log = logging.getLogger("grant_review")
log.setLevel(logging.INFO)
# The checkpoint library logs every DynamoDB write at INFO (about 30 lines a run): keep its warnings only.
logging.getLogger("langgraph_checkpoint_aws").setLevel(logging.WARNING)


def _log(event: str, result: dict) -> None:
    log.info(event, extra={"event": event, **result, "trace_id": tracing.current_trace_id()})


@functools.cache
def _workflow():
    return workflow.build(workflow.bedrock_steps(bedrock.make_client()), checkpointer=workflow.dynamodb_checkpointer())


@functools.cache
def _applications():
    return applications.DynamoDBStore()


def _record(outcome: workflow.Outcome) -> None:
    _applications().put(applications.record(outcome, dt.datetime.now(dt.UTC)))


@functools.cache
def _s3():
    return boto3.Session(region_name=bedrock.REGION).client("s3")


def _download(bucket: str, application_id: str) -> tuple[dict, str, pathlib.Path]:
    folder = WORK_DIR / application_id
    folder.mkdir(parents=True, exist_ok=True)
    for name in PACKET:
        _s3().download_file(bucket, f"{application_id}/{name}", str(folder / name))
    form = json.loads((folder / "form.json").read_text(encoding="utf-8"))
    return form, (folder / "narrative.md").read_text(encoding="utf-8"), folder / "return.pdf"


def summary(outcome: workflow.Outcome) -> dict:
    """What a run did, without the applicant's text: safe to log and to return."""
    state = outcome.state
    return {
        "application_id": state.application_id,
        "route": state.route,
        "failed_rules": state.eligibility.failed_rules if state.eligibility else None,
        "flags": sorted(f.kind for f in state.flags),
        "waiting_for_review": outcome.waiting_for_review,
        "letter_ready": state.letter is not None,
        "letter_problems": state.letter_problems,
        "model_calls": len(state.calls),
        "cost_usd": round(state.cost_usd, 5),
    }


def start(event: dict, context=None) -> dict:
    """EventBridge 'Object Created' for <application_id>/form.json in the intake bucket."""
    detail = event["detail"]
    bucket, key = detail["bucket"]["name"], detail["object"]["key"]
    application_id, _, name = key.rpartition("/")
    if name != "form.json":
        raise ValueError(f"start runs on <application_id>/form.json, not {key!r}")
    application_id = applications.check_id(application_id)
    form, narrative, pdf = _download(bucket, application_id)

    app = _workflow()
    if app.get_state(workflow.thread(application_id)).values:
        outcome = workflow.carry_on(app, application_id)  # a retry: continue from the last checkpoint
    else:
        try:
            application = intake(form, narrative, pdf, application_id=application_id)
        except IntakeError as e:
            # A malformed packet is an answer, not an outage: retrying won't fix it, so report and stop.
            result = {"application_id": application_id, "rejected": e.problems}
            _log("rejected", result)
            return result
        outcome = workflow.start(app, application)
    _record(outcome)
    result = summary(outcome)
    _log("started", result)
    return result


def resume(event: dict, context=None) -> dict:
    """{"application_id": ..., "decision": {ReviewerDecision fields}} from the reviewer UI's API, or
    {"application_id": ..., "finish": true} to complete a run whose decision went in but whose letter wasn't drafted."""
    application_id = applications.check_id(event.get("application_id"))
    finish = event.get("finish") is True
    # Invalid input never reaches the graph.
    decision = None if finish else ReviewerDecision.model_validate(event.get("decision"))
    app = _workflow()
    current = workflow.outcome(app, application_id) if app.get_state(workflow.thread(application_id)).values else None
    if finish:
        if current is None or not workflow.unfinished_decision(current):
            raise ValueError(f"application {application_id} has no decision waiting to be finished")
        outcome = workflow.carry_on(app, application_id)  # from the last checkpoint: the decision is kept
    elif current is None or not current.waiting_for_review:
        raise ValueError(f"application {application_id} isn't waiting for a reviewer")
    else:
        outcome = workflow.resume(app, application_id, decision)
    _record(outcome)
    result = summary(outcome)
    _log("resumed", result)
    return {**result, "letter": outcome.state.letter}  # the reviewer sees the letter; the log doesn't
