"""The checkpoint table script, without AWS: botocore's Stubber answers the DynamoDB control-plane calls and checks
each request against DynamoDB's API definition."""

import datetime as dt

import boto3
import pytest
from botocore.stub import Stubber

import checkpoint_table


@pytest.fixture
def stubbed():
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-2")
    client = session.client("dynamodb")
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def table_description(read_capacity=0):
    return {
        "Table": {
            "TableName": checkpoint_table.TABLE,
            "TableStatus": "ACTIVE",
            "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            "ProvisionedThroughput": {"ReadCapacityUnits": read_capacity, "WriteCapacityUnits": read_capacity},
            "ItemCount": 1092,
            "TableSizeBytes": 512_000,
            "CreationDateTime": dt.datetime(2026, 10, 20, tzinfo=dt.UTC),
        }
    }


TTL_ON = {"TimeToLiveDescription": {"TimeToLiveStatus": "ENABLED", "AttributeName": "ttl"}}


def test_report_describes_an_on_demand_table(stubbed, capsys):
    client, stubber = stubbed
    stubber.add_response("describe_table", table_description(), {"TableName": checkpoint_table.TABLE})
    stubber.add_response("describe_time_to_live", TTL_ON, {"TableName": checkpoint_table.TABLE})
    checkpoint_table.report(client)
    out = capsys.readouterr().out
    assert "ACTIVE" in out and "on-demand" in out and "TTL ENABLED" in out and "1092 items" in out


def test_report_says_when_the_table_is_missing(stubbed, capsys):
    client, stubber = stubbed
    stubber.add_client_error("describe_table", service_error_code="ResourceNotFoundException", http_status_code=400)
    checkpoint_table.report(client)
    assert "doesn't exist" in capsys.readouterr().out


def test_create_makes_the_layout_the_checkpointer_expects(stubbed):
    client, stubber = stubbed
    stubber.add_client_error("describe_table", service_error_code="ResourceNotFoundException", http_status_code=400)
    stubber.add_response(
        "create_table",
        {},
        {
            "TableName": checkpoint_table.TABLE,
            "AttributeDefinitions": [
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": checkpoint_table.TAGS,
        },
    )
    stubber.add_response("describe_table", table_description(), {"TableName": checkpoint_table.TABLE})  # the waiter
    stubber.add_response(
        "describe_time_to_live",
        {"TimeToLiveDescription": {"TimeToLiveStatus": "DISABLED"}},
        {"TableName": checkpoint_table.TABLE},
    )
    stubber.add_response(
        "update_time_to_live",
        {"TimeToLiveSpecification": {"Enabled": True, "AttributeName": "ttl"}},
        {"TableName": checkpoint_table.TABLE, "TimeToLiveSpecification": {"Enabled": True, "AttributeName": "ttl"}},
    )
    checkpoint_table.create(client)


def test_create_is_safe_to_rerun_on_an_existing_table(stubbed):
    client, stubber = stubbed
    stubber.add_response("describe_table", table_description(), {"TableName": checkpoint_table.TABLE})
    stubber.add_response("describe_time_to_live", TTL_ON, {"TableName": checkpoint_table.TABLE})
    checkpoint_table.create(client)  # no create_table, no update_time_to_live
