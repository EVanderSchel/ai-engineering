"""The status records, without AWS: the in-memory store for the logic, and the DynamoDB store against botocore's
Stubber, which checks each request against DynamoDB's API definition."""

import datetime as dt
from decimal import Decimal

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.stub import ANY, Stubber
from test_workflow import app, application, decision

import applications
import workflow

NOW = dt.datetime(2025, 7, 1, 12, tzinfo=dt.UTC)


def paused(tmp_path, packet="gp-027"):
    graph = app()
    return graph, workflow.start(graph, application(packet, tmp_path))


def test_a_paused_application_is_waiting_for_review(tmp_path):
    _, outcome = paused(tmp_path)
    rec = applications.record(outcome, NOW)
    assert rec.status == "waiting_for_review" and rec.route == "review" and rec.decision is None
    assert rec.application_id == "gp-027" and rec.updated_at == NOW
    assert rec.organization_name == outcome.state.form.organization_name
    assert rec.amount_requested == outcome.state.form.amount_requested


def test_a_decided_application_records_the_decision(tmp_path):
    graph, _ = paused(tmp_path)
    done = workflow.resume(graph, "gp-027", decision("approve"))
    rec = applications.record(done, NOW)
    assert rec.status == "decided" and rec.decision == "approve"


def test_a_decline_by_the_rules_is_recorded_as_such(tmp_path):
    outcome = workflow.start(app(), application("gp-018", tmp_path))  # fails revenue_range
    rec = applications.record(outcome, NOW)
    assert rec.status == "declined_by_rules" and rec.failed_rules and rec.decision is None


def test_corrections_that_fail_the_rules_end_as_declined_by_rules(tmp_path):
    graph, _ = paused(tmp_path)
    done = workflow.resume(graph, "gp-027", decision(corrections={"total_revenue_current_year": 10}))
    assert applications.record(done, NOW).status == "declined_by_rules"


def test_a_record_holds_no_applicant_narrative(tmp_path):
    _, outcome = paused(tmp_path)
    dumped = applications.record(outcome, NOW).model_dump_json()
    assert outcome.state.narrative[:40] not in dumped
    assert set(applications.ApplicationRecord.model_fields) == {
        "application_id",
        "status",
        "received_at",
        "updated_at",
        "organization_name",
        "amount_requested",
        "route",
        "failed_rules",
        "flags",
        "decision",
    }


def rec(application_id, status="waiting_for_review", received_hour=9):
    return applications.ApplicationRecord(
        application_id=application_id,
        status=status,
        received_at=dt.datetime(2025, 6, 1, received_hour, tzinfo=dt.UTC),
        updated_at=NOW,
        organization_name="Example Food Bank",
        amount_requested=20_000,
        route="review",
        failed_rules=[],
        flags=["budget_mismatch"],
    )


def test_the_memory_store_lists_one_status_oldest_first_and_replaces_on_put():
    store = applications.MemoryStore()
    store.put(rec("b", received_hour=11))
    store.put(rec("a", received_hour=10))
    store.put(rec("c", status="decided"))
    assert [r.application_id for r in store.list("waiting_for_review")] == ["a", "b"]
    store.put(rec("a", status="decided"))
    assert [r.application_id for r in store.list("waiting_for_review")] == ["b"]
    assert store.get("a").status == "decided" and store.get("missing") is None


def test_received_at_sorts_as_text_only_in_utc():
    # 10:00 in Chicago (15:00 UTC) arrived after 14:00 UTC, though "10" sorts before "14" as text.
    chicago = dt.datetime(2025, 6, 1, 10, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert applications._utc(chicago) > applications._utc(dt.datetime(2025, 6, 1, 14, tzinfo=dt.UTC))


@pytest.fixture
def stubbed():
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-2")
    table = session.resource("dynamodb").Table(applications.TABLE)
    with Stubber(table.meta.client) as stubber:
        yield applications.DynamoDBStore(table), stubber
        stubber.assert_no_pending_responses()


def test_put_writes_utc_times_and_a_ttl_matching_the_checkpoints(stubbed):
    store, stubber = stubbed
    # The Stubber sees the item as the resource layer receives it (plain Python), before it becomes AttributeValues.
    item = {
        **rec("gp-027").model_dump(mode="json"),
        "received_at": "2025-06-01T09:00:00.000000+00:00",
        "updated_at": "2025-07-01T12:00:00.000000+00:00",
        "ttl": int(NOW.timestamp()) + workflow.CHECKPOINT_TTL_SECONDS,
    }
    stubber.add_response("put_item", {}, {"TableName": applications.TABLE, "Item": item})
    store.put(rec("gp-027"))


def as_item(r):
    """A record as DynamoDB returns it: numbers as Decimal, with the ttl attribute."""
    item = r.model_dump(mode="json")
    return {**item, "amount_requested": Decimal(item["amount_requested"]), "ttl": Decimal(1)}


def test_get_reads_a_record_back_and_none_when_missing(stubbed):
    store, stubber = stubbed
    serialize = boto3.dynamodb.types.TypeSerializer().serialize
    item = {k: serialize(v) for k, v in as_item(rec("gp-027")).items()}
    stubber.add_response(
        "get_item", {"Item": item}, {"TableName": applications.TABLE, "Key": {"application_id": "gp-027"}}
    )
    stubber.add_response("get_item", {}, {"TableName": applications.TABLE, "Key": {"application_id": "nope"}})
    assert store.get("gp-027") == rec("gp-027")
    assert store.get("nope") is None


def test_list_queries_the_status_index_across_pages(stubbed):
    store, stubber = stubbed
    serialize = boto3.dynamodb.types.TypeSerializer().serialize

    def page(*records, last=None):
        response = {"Items": [{k: serialize(v) for k, v in as_item(r).items()} for r in records]}
        return {**response, "LastEvaluatedKey": {"application_id": {"S": last}}} if last else response

    expected = {
        "TableName": applications.TABLE,
        "IndexName": applications.STATUS_INDEX,
        "KeyConditionExpression": Key("status").eq("waiting_for_review"),
    }
    stubber.add_response("query", page(rec("a"), last="a"), expected)
    stubber.add_response("query", page(rec("b")), {**expected, "ExclusiveStartKey": ANY})
    assert [r.application_id for r in store.list("waiting_for_review")] == ["a", "b"]
