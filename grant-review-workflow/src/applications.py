"""Where each application stands, as one small record per application (step 8): what the reviewer UI's queue lists.

The checkpoints hold everything about a run, but only as LangGraph's saved state, one thread per application: there's
no way to ask them "which applications are waiting for a reviewer?" without reading every thread. So after each run
the Lambda handlers also write a record here, and the queue is one query on its status index.

    table grant-review-applications      partition key application_id
    index by-status                      partition key status, sort key received_at (oldest first)

A record is a summary: the form's name and amount, the route, the rules failed, the kinds of flag. Never the narrative
or the evidence: those stay in the checkpoints, read only when a reviewer opens the application. Records expire with
their checkpoints (30 days after the last run, design decision 8), so the queue never lists an application whose
evidence is gone.
"""

import datetime as dt
import re
from typing import Literal

import boto3
from boto3.dynamodb.conditions import Key
from pydantic import BaseModel

import bedrock
import workflow
from state import FlagKind, Route, RuleId

TABLE = "grant-review-applications"
STATUS_INDEX = "by-status"

Status = Literal["waiting_for_review", "decided", "declined_by_rules"]
APPLICATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # a folder name, an S3 prefix and a DynamoDB key


def check_id(value) -> str:
    """An application ID from outside (an S3 key, a URL path): refused unless it's safe to use as all three."""
    if not isinstance(value, str) or not APPLICATION_ID.fullmatch(value):
        raise ValueError(f"not a valid application ID: {value!r}")
    return value


class ApplicationRecord(BaseModel):
    """One row of the queue. organization_name is what the applicant typed: show it as text, never trust it."""

    application_id: str
    status: Status
    received_at: dt.datetime
    updated_at: dt.datetime
    organization_name: str
    amount_requested: int
    route: Route
    failed_rules: list[RuleId]
    flags: list[FlagKind]  # kinds only, sorted, repeats kept: the evidence is in the checkpoints
    decision: Literal["approve", "decline"] | None = None  # the reviewer's, once decided


def status(outcome: workflow.Outcome) -> Status:
    """Where a run left the application. A corrected figure can send it back through the rules to a decline."""
    state = outcome.state
    if outcome.waiting_for_review:
        return "waiting_for_review"
    if state.review is not None and state.review.decision is not None:
        return "decided"
    if state.route == "decline_ineligible" and state.letter is not None:
        return "declined_by_rules"
    raise ValueError(f"application {state.application_id} neither finished nor paused for a reviewer")


def record(outcome: workflow.Outcome, now: dt.datetime) -> ApplicationRecord:
    state = outcome.state
    return ApplicationRecord(
        application_id=state.application_id,
        status=status(outcome),
        received_at=state.received_at,
        updated_at=now,
        organization_name=state.form.organization_name,
        amount_requested=state.form.amount_requested,
        route=state.route,
        failed_rules=state.eligibility.failed_rules,
        flags=sorted(f.kind for f in state.flags),
        decision=state.review.decision if state.review else None,
    )


def _utc(moment: dt.datetime) -> str:
    """ISO 8601 in UTC: the index sorts received_at as text, which is time order only in one timezone and format."""
    return moment.astimezone(dt.UTC).isoformat(timespec="microseconds")


class MemoryStore:
    """Records in this process's memory: for tests and local runs, as workflow.memory_checkpointer is."""

    def __init__(self):
        self._records: dict[str, ApplicationRecord] = {}

    def put(self, rec: ApplicationRecord) -> None:
        self._records[rec.application_id] = rec

    def get(self, application_id: str) -> ApplicationRecord | None:
        return self._records.get(application_id)

    def list(self, status: Status) -> list[ApplicationRecord]:
        return sorted((r for r in self._records.values() if r.status == status), key=lambda r: _utc(r.received_at))


class DynamoDBStore:
    """Records in the grant-review-applications table (step 8's CDK creates it, with the index and TTL)."""

    def __init__(self, table=None):
        self.table = table or boto3.Session(region_name=bedrock.REGION).resource("dynamodb").Table(TABLE)

    def put(self, rec: ApplicationRecord) -> None:
        item = rec.model_dump(mode="json")
        item["received_at"], item["updated_at"] = _utc(rec.received_at), _utc(rec.updated_at)
        item["ttl"] = int(rec.updated_at.timestamp()) + workflow.CHECKPOINT_TTL_SECONDS
        self.table.put_item(Item=item)  # replaces the record: each run writes where the application now stands

    def get(self, application_id: str) -> ApplicationRecord | None:
        item = self.table.get_item(Key={"application_id": application_id}).get("Item")
        return ApplicationRecord.model_validate(item) if item else None

    def list(self, status: Status) -> list[ApplicationRecord]:
        pages = self.table.meta.client.get_paginator("query").paginate(
            TableName=self.table.name,
            IndexName=STATUS_INDEX,
            KeyConditionExpression=Key("status").eq(status),
        )
        return [ApplicationRecord.model_validate(item) for page in pages for item in page["Items"]]
