"""The reviewer UI's API (step 8): one Lambda function behind an API Gateway HTTP API, which routes each request
here with its route key.

    GET /applications?status=waiting_for_review     the queue: status records, oldest first (applications.py)
    GET /applications/{application_id}              everything a reviewer needs to decide one application
    POST /applications/{application_id}/decision    the reviewer's decision (or corrections): resumes the workflow

The queue comes from the status records; one application's evidence from its checkpoints, read by a graph whose model
steps refuse to run (workflow.reader): reading never calls a model or moves an application on.

A decision names who made it from the sign-in (the claims API Gateway's JWT authorizer passes in, stage 4), never
from the request body, and when from this server's clock. It claims the application (applications.py), so only one
decision gets through, then runs the Resume function, which holds the model and checkpoint-writing permissions this
function doesn't have.

Most of an application's evidence comes from the applicant: the form, the narrative, and the model's reading of the
narrative. The API returns it as data in JSON; the UI must show it as text, never as markup or instructions. Logs get
the route, the status code and the application ID only (docs/data-handling.md).
"""

import datetime as dt
import functools
import json
import logging
import os
from typing import Literal, get_args

import boto3
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.json_schema import models_json_schema

import applications
import bedrock
import fund_rules
import workflow
from state import ApplicationForm, Extraction, Flag, ProposalReading, Research, ReviewerDecision, RuleId

log = logging.getLogger("grant_review")
log.setLevel(logging.INFO)
logging.getLogger("langgraph_checkpoint_aws").setLevel(logging.WARNING)


class RuleCheck(BaseModel):
    rule: RuleId
    description: str
    passed: bool


class Evidence(BaseModel):
    """One application, as the reviewer sees it."""

    record: applications.ApplicationRecord
    waiting_for_review: bool
    form: ApplicationForm  # untrusted: what the applicant typed
    rules: list[RuleCheck]  # every rule of the fund, in letter order, with whether it passed
    flags: list[Flag]
    extraction: Extraction | None  # the figures read from the return
    research: Research | None  # the public record
    proposal: ProposalReading | None  # the model's reading of the narrative: its quotes are the applicant's words
    narrative: str  # untrusted
    review: ReviewerDecision | None
    letter: str | None
    letter_problems: list[str]
    model_calls: int
    cost_usd: float


class Queue(BaseModel):
    applications: list[applications.ApplicationRecord]


class DecisionRequest(BaseModel):
    """What the reviewer sends. Corrections replace figures read from the return (ReturnFigures field -> value) and send
    the application back through the rules; with no decision, it pauses again afterwards."""

    model_config = ConfigDict(extra="forbid")  # a "reviewer" or "decided_at" in the body is refused, not ignored

    decision: Literal["approve", "decline"] | None
    corrections: dict[str, int] = {}
    reason: str = Field(min_length=1, max_length=2000)  # quoted word for word in the letter to the applicant


def contract() -> dict:
    """The API's responses as JSON Schema: the reviewer UI generates its TypeScript types from it (ui/README.md), and
    tests/test_ui_contract.py fails when this changes and the UI's copy doesn't."""
    _, schema = models_json_schema(
        [(Queue, "serialization"), (Evidence, "serialization"), (DecisionRequest, "validation")], title="ReviewerApi"
    )
    for model in schema["$defs"].values():
        # Every field is in every response (model_dump_json leaves none out), defaults included: required, so the UI
        # never handles a field that can't be missing. A field's title would become a type alias of its own.
        model["required"] = list(model["properties"])
        for field in model["properties"].values():
            field.pop("title", None)
    # A root that refers to both responses, so the type generator emits them (it emits only what's reachable).
    refs = {name: {"$ref": f"#/$defs/{name}"} for name in ("Queue", "Evidence", "DecisionRequest")}
    return {**schema, "type": "object", "properties": refs, "required": list(refs), "additionalProperties": False}


class ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@functools.cache
def _workflow():
    return workflow.reader(workflow.dynamodb_checkpointer())


@functools.cache
def _applications():
    return applications.DynamoDBStore()


@functools.cache
def _lambda():
    return boto3.Session(region_name=bedrock.REGION).client("lambda")


class ResumeFailed(Exception):
    pass


def _resume(payload: dict) -> None:
    """Run the Resume function and wait for it (stage 5 sets RESUME_FUNCTION). Locally, handlers.resume in-process."""
    response = _lambda().invoke(FunctionName=os.environ["RESUME_FUNCTION"], Payload=json.dumps(payload).encode())
    if "FunctionError" in response:
        error = json.loads(response["Payload"].read())
        raise ResumeFailed(error.get("errorType", "error"))  # the type only: its message may quote applicant text


def _reviewer(event: dict) -> str:
    """The signed-in reviewer, from the claims API Gateway's JWT authorizer verified (stage 4)."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or {}
    if not claims.get("email"):
        raise ApiError(401, "sign in to decide")
    return claims["email"]


def _application_id(value) -> str:
    try:
        return applications.check_id(value)  # it comes from the browser's URL, and becomes a DynamoDB key
    except ValueError as e:
        raise ApiError(400, str(e)) from None


def queue(status: str) -> Queue:
    if status not in get_args(applications.Status):
        raise ApiError(400, f"status must be one of {list(get_args(applications.Status))}")
    return Queue(applications=_applications().list(status))


def evidence(application_id: str) -> Evidence:
    rec = _applications().get(application_id)
    app = _workflow()
    if rec is None or not app.get_state(workflow.thread(application_id)).values:
        raise ApiError(404, f"no application {application_id}")  # unknown, or its checkpoints have expired
    outcome = workflow.outcome(app, application_id)
    state = outcome.state
    failed = state.eligibility.failed_rules if state.eligibility else None
    rules = (
        [RuleCheck(rule=r, description=d, passed=r not in failed) for r, d in fund_rules.RULES.items()]
        if failed is not None
        else []
    )
    return Evidence(
        record=rec,
        waiting_for_review=outcome.waiting_for_review,
        form=state.form,
        rules=rules,
        flags=state.flags,
        extraction=state.extraction,
        research=state.research,
        proposal=state.proposal,
        narrative=state.narrative,
        review=state.review,
        letter=state.letter,
        letter_problems=state.letter_problems,
        model_calls=len(state.calls),
        cost_usd=round(state.cost_usd, 5),
    )


def decide(application_id: str, request: DecisionRequest, reviewer: str, now: dt.datetime) -> Evidence:
    try:
        decision = ReviewerDecision(**request.model_dump(), reviewer=reviewer, decided_at=now)
    except ValidationError as e:
        raise ApiError(400, "; ".join(err["msg"] for err in e.errors())) from None  # e.g. not a figure on the return
    store = _applications()
    if store.get(application_id) is None:
        raise ApiError(404, f"no application {application_id}")
    if not store.claim(application_id, now):
        raise ApiError(409, f"application {application_id} isn't waiting for a reviewer (decided, or being decided)")
    try:
        _resume({"application_id": application_id, "decision": decision.model_dump(mode="json")})
    except Exception as e:
        if workflow.outcome(_workflow(), application_id).waiting_for_review:
            store.release(application_id)  # the thread never took the decision: put it back in the queue
            raise ApiError(502, "the decision wasn't applied; nothing changed, try again") from None
        # The thread took the decision but stopped before the letter: it stays claimed for a person to finish.
        log.error("decided, not finished", extra={"event": "resume_failed", "application_id": application_id})
        raise ApiError(502, "the decision was saved but the letter wasn't drafted; it needs finishing") from e
    return evidence(application_id)


def _decision_request(event: dict) -> DecisionRequest:
    try:
        return DecisionRequest.model_validate_json(event.get("body") or "")
    except ValidationError as e:
        problems = [f"{'.'.join(map(str, err['loc'])) or 'body'}: {err['msg']}" for err in e.errors()]
        raise ApiError(400, "; ".join(problems)) from None  # field names and rules only, never the values sent


def _response(status_code: int, body: str) -> dict:
    return {"statusCode": status_code, "headers": {"content-type": "application/json"}, "body": body}


def handler(event: dict, context=None) -> dict:
    """An API Gateway HTTP API request (payload format 2.0)."""
    route = event.get("routeKey")
    application_id = (event.get("pathParameters") or {}).get("application_id")
    try:
        if route == "GET /applications":
            params = event.get("queryStringParameters") or {}
            body = queue(params.get("status", "waiting_for_review")).model_dump_json()
        elif route == "GET /applications/{application_id}":
            body = evidence(_application_id(application_id)).model_dump_json()
        elif route == "POST /applications/{application_id}/decision":
            checked_id, reviewer, request = _application_id(application_id), _reviewer(event), _decision_request(event)
            body = decide(checked_id, request, reviewer, dt.datetime.now(dt.UTC)).model_dump_json()
        else:
            raise ApiError(404, f"no route {route}")
        status_code = 200
    except ApiError as e:
        status_code, body = e.status_code, json.dumps({"error": str(e)})
    log.info(
        "api", extra={"event": "api", "route": route, "status_code": status_code, "application_id": application_id}
    )
    return _response(status_code, body)
