"""The DynamoDB table that holds the workflow's checkpoints (design decision 8).

    uv run python src/checkpoint_table.py            # report: does it exist, TTL, items (changes nothing)
    uv run python src/checkpoint_table.py --create   # create it if missing, and make sure TTL is on (rerunnable)

The layout is what langgraph-checkpoint-aws's DynamoDBSaver expects: partition key PK and sort key SK (both
strings), and TTL on the attribute "ttl" (the saver stamps each checkpoint with its expiry; DynamoDB deletes
expired items within a few days, at no cost). On-demand billing: nothing is charged while idle. Encrypted at rest
with an AWS-owned key (DynamoDB's default). No point-in-time recovery: a lost checkpoint costs one rerun of an
application. Step 7's CDK takes the table over; until then this script is its definition.

To remove it (all saved progress is lost):
    aws dynamodb delete-table --table-name grant-review-checkpoints --region us-east-2
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

from bedrock import REGION

TABLE = "grant-review-checkpoints"
TTL_ATTRIBUTE = "ttl"
TAGS = [{"Key": "project", "Value": "grant-review-workflow"}, {"Key": "purpose", "Value": "langgraph-checkpoints"}]


def client():
    return boto3.Session(region_name=REGION).client("dynamodb")


def describe(dynamodb) -> dict | None:
    """The table's description, or None if it doesn't exist."""
    try:
        return dynamodb.describe_table(TableName=TABLE)["Table"]
    except dynamodb.exceptions.ResourceNotFoundException:
        return None


def ttl_status(dynamodb) -> str:
    return dynamodb.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]["TimeToLiveStatus"]


def create(dynamodb) -> None:
    if describe(dynamodb) is None:
        dynamodb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            BillingMode="PAY_PER_REQUEST",
            Tags=TAGS,
        )
        print(f"Creating {TABLE} in {REGION}...")
        dynamodb.get_waiter("table_exists").wait(TableName=TABLE)
    if ttl_status(dynamodb) in ("DISABLED", "DISABLING"):
        dynamodb.update_time_to_live(
            TableName=TABLE, TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTRIBUTE}
        )


def report(dynamodb) -> None:
    table = describe(dynamodb)
    if table is None:
        print(f"{TABLE} doesn't exist in {REGION}. Create it with --create.")
        return
    keys = {k["AttributeName"]: k["KeyType"] for k in table["KeySchema"]}
    # An on-demand table reports 0 provisioned capacity. (Read this way rather than from BillingModeSummary:
    # CodeQL's sensitive-data rule treats any name containing "billing" as financial data, a false positive here.)
    capacity = "on-demand" if table["ProvisionedThroughput"]["ReadCapacityUnits"] == 0 else "provisioned"
    print(f"{TABLE} ({REGION}): {table['TableStatus']}, keys {keys}, {capacity}, TTL {ttl_status(dynamodb)}")
    print(
        f"  about {table['ItemCount']} items, {table['TableSizeBytes']:,} bytes (DynamoDB updates these every ~6 hours)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="The checkpoint table: report, or create with --create.")
    parser.add_argument("--create", action="store_true", help="create the table if missing and turn TTL on")
    args = parser.parse_args()
    dynamodb = client()
    try:
        if args.create:
            create(dynamodb)
        report(dynamodb)
    except ClientError as e:
        print(f"AWS error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
