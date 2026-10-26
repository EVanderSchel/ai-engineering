"""The workflow's steps for AWS Step Functions (step 9): the same workflow, orchestrated by a state machine instead of
LangGraph, to compare the two on evidence.

The state machine (infra/lib/step-functions-stack.ts) passes the application's state between its states as JSON and
calls this one Lambda function for every step:

    {"step": "<name>", "state": <ApplicationState as JSON>, ...}  ->  <ApplicationState as JSON>

Each step runs the code the LangGraph graph runs (workflow.bedrock_steps, checks.py, intake). What LangGraph did for us,
the state machine and this module do explicitly:

- Parallel steps: a Parallel state runs extract, research and read_proposal on the same state and hands back three
  copies of it. "join" takes each branch's own field and appends the branches' new flags and model-call records, as
  the graph's reducers (operator.add on `flags` and `calls`) do.
- Saving progress: Step Functions keeps each execution's state; there are no checkpoints of our own.
- The reviewer: "await_review" leaves the execution's task token in the reviews table and the state machine waits
  until someone sends the decision with it (SendTaskSuccess); "apply_review" then validates the decision and keeps
  earlier corrections (workflow.with_earlier_corrections, shared with the graph's human_review). A Lambda task does
  the waiting because Step Functions' DynamoDB integration can't wait for a task token.

Logs carry the step, the application ID and the number of model calls, never applicant text (docs/data-handling.md).
"""

import functools
import logging
import os
import pathlib
import time

import boto3

import applications
import bedrock
import checks
import handlers
import workflow
from intake import intake
from state import ApplicationState, ReviewerDecision

log = logging.getLogger("grant_review")
log.setLevel(logging.INFO)

BRANCHES = {"extract": "extraction", "research": "research", "read_proposal": "proposal"}  # step -> the field it owns
APPENDED = ("flags", "calls")  # the fields more than one step adds to: appended, never replaced


def _apply(state: ApplicationState, update: dict) -> ApplicationState:
    """A step's returned fields merged into the state the way the graph merges them."""
    update = dict(update)
    for key in APPENDED:
        if key in update:
            update[key] = [*getattr(state, key), *update[key]]
    return state.model_copy(update=update)


def join(state: ApplicationState, branches: dict[str, ApplicationState]) -> ApplicationState:
    """The Parallel state's three results, merged: each branch's own field, plus every branch's additions to the
    appended lists (a branch's copy starts with `state`'s lists, so its additions are what follows them)."""
    update: dict = {field: getattr(branches[step], field) for step, field in BRANCHES.items()}
    for key in APPENDED:
        before = len(getattr(state, key))
        update[key] = [item for step in BRANCHES for item in getattr(branches[step], key)[before:]]
    return _apply(state, update)


def run(steps: workflow.Steps, step: str, state: ApplicationState, **extra) -> ApplicationState:
    """One step of the state machine, on the given state; `steps` are the model steps (Bedrock, or stand-ins)."""
    if step in BRANCHES:
        return _apply(state, getattr(steps, step)(state))
    if step == "join":
        return join(state, extra["branches"])
    if step == "cross_check":
        return _apply(state, {"flags": checks.cross_check(state)})
    if step == "eligibility":
        return _apply(state, {"eligibility": checks.eligibility(state)})
    if step == "route":
        return _apply(state, {"route": checks.route(state)})
    if step == "apply_review":
        decision = ReviewerDecision.model_validate(extra["decision"])  # invalid input never reaches the state
        return _apply(state, {"review": workflow.with_earlier_corrections(state, decision)})
    if step == "draft_letter":
        return _apply(state, steps.draft_letter(state))
    raise ValueError(f"no step {step!r}")


REVIEW_WAIT_SECONDS = 30 * 24 * 3600  # the state machine's own timeout for the wait: the data's 30 days


@functools.cache
def _reviews():
    """The table where a waiting execution leaves its task token (infra/lib/step-functions-stack.ts)."""
    return boto3.Session(region_name=bedrock.REGION).resource("dynamodb").Table(os.environ["REVIEWS_TABLE"])


def await_review(application_id: str, task_token: str, now: float | None = None) -> None:
    """Leave the execution's task token for whoever decides; the item expires with the wait."""
    now = time.time() if now is None else now
    _reviews().put_item(
        Item={"application_id": application_id, "task_token": task_token, "ttl": int(now) + REVIEW_WAIT_SECONDS}
    )


@functools.cache
def _steps() -> workflow.Steps:
    return workflow.bedrock_steps(bedrock.make_client())


def _with_local_return(state: ApplicationState, bucket: str) -> ApplicationState:
    """The return PDF where extract reads it: each step may run in a fresh Lambda container, so download it again
    when this one doesn't have it (as handlers.start does when Lambda retries a run)."""
    if not pathlib.Path(state.return_pdf).exists():
        handlers._download(bucket, state.application_id)
    return state


def handler(event: dict, context=None) -> dict:
    """{"step": ..., "state": {...}} -> the new state; "intake" takes {"application_id", "bucket"} instead."""
    step = event["step"]
    if step == "await_review":  # the execution waits for SendTaskSuccess; this task's own result is ignored
        await_review(applications.check_id(event["application_id"]), event["task_token"])
        log.info("step", extra={"event": "step", "step": step, "application_id": event["application_id"]})
        return {}
    if step == "intake":
        application_id = applications.check_id(event["application_id"])
        form, narrative, pdf = handlers._download(event["bucket"], application_id)
        new = intake(form, narrative, pdf, application_id=application_id)
    else:
        state = ApplicationState.model_validate(event["state"])
        if step == "extract":
            state = _with_local_return(state, event["bucket"])
        branches = {s: ApplicationState.model_validate(b) for s, b in (event.get("branches") or {}).items()}
        calls_before = len(state.calls)
        new = run(_steps(), step, state, branches=branches, decision=event.get("decision"))
        log.info(
            "step",
            extra={
                "event": "step",
                "step": step,
                "application_id": new.application_id,
                "model_calls": len(new.calls) - calls_before,
            },
        )
    return new.model_dump(mode="json")
