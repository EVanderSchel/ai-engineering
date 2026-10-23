"""The reviewer UI's API (step 8): one Lambda function behind an API Gateway HTTP API, which routes each request
here with its route key.

    GET /applications?status=waiting_for_review     the queue: status records, oldest first (applications.py)
    GET /applications/{application_id}              everything a reviewer needs to decide one application

The queue comes from the status records; one application's evidence from its checkpoints, read by a graph whose model
steps refuse to run (workflow.reader): reading never calls a model or moves an application on. Decisions arrive in
stage 3, sign-in in stage 4.

Most of an application's evidence comes from the applicant: the form, the narrative, and the model's reading of the
narrative. The API returns it as data in JSON; the UI must show it as text, never as markup or instructions. Logs get
the route, the status code and the application ID only (docs/data-handling.md).
"""

import functools
import json
import logging
from typing import get_args

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

import applications
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


def contract() -> dict:
    """The API's responses as JSON Schema: the reviewer UI generates its TypeScript types from it (ui/README.md), and
    tests/test_ui_contract.py fails when this changes and the UI's copy doesn't."""
    _, schema = models_json_schema([(Queue, "serialization"), (Evidence, "serialization")], title="ReviewerApi")
    for model in schema["$defs"].values():
        # Every field is in every response (model_dump_json leaves none out), defaults included: required, so the UI
        # never handles a field that can't be missing. A field's title would become a type alias of its own.
        model["required"] = list(model["properties"])
        for field in model["properties"].values():
            field.pop("title", None)
    # A root that refers to both responses, so the type generator emits them (it emits only what's reachable).
    refs = {"queue": {"$ref": "#/$defs/Queue"}, "evidence": {"$ref": "#/$defs/Evidence"}}
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
        else:
            raise ApiError(404, f"no route {route}")
        status_code = 200
    except ApiError as e:
        status_code, body = e.status_code, json.dumps({"error": str(e)})
    log.info(
        "api", extra={"event": "api", "route": route, "status_code": status_code, "application_id": application_id}
    )
    return _response(status_code, body)
