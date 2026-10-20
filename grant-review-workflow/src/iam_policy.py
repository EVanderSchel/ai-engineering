"""The least-privilege policy for the workflow's runtime role (step 6d), and its check with IAM's policy simulator.

    uv run python src/iam_policy.py               # print the policy for this project's account
    uv run python src/iam_policy.py --simulate    # check it with iam simulate-custom-policy (free, changes nothing)

The baseline came from IAM Policy Autopilot (awslabs, static analysis of the boto3 calls in src/bedrock.py,
src/workflow.py and langgraph-checkpoint-aws's DynamoDB saver). Autopilot's draft was broader than the workflow
needs; this narrows it:

- bedrock:InvokeModel (what Converse needs): only the two inference profiles the steps use, and the foundation
  models behind them only when called through those profiles (bedrock:InferenceProfileArn). Autopilot had
  Resource "*". Dropped: CallWithBearerToken (Bedrock API keys; the role signs requests), InvokeTool (Bedrock's
  server-side tools), ApplyGuardrail (the workflow never calls the guardrail; only guardrail.py --compare does).
- DynamoDB: the five calls the saver makes, on the checkpoint table only (Autopilot had table/*). Dropped:
  Read/WriteDataForReplication (global tables) and kms:Decrypt (the table uses DynamoDB's AWS-owned key).

Not here: creating tables or guardrails (admin scripts, run with the developer's own sign-in), the ProPublica API
(no AWS), and the S3 client the saver creates only for its optional large-checkpoint offload (not configured).
The role itself is created in step 7 (CDK); the account ID is passed in, never committed.
"""

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

import bedrock
import checkpoint_table
import workflow

# Where the us. profiles send requests (get-inference-profile, checked 2026-10-20). --simulate rechecks it live.
PROFILE_REGIONS = ["us-east-1", "us-east-2", "us-west-2"]
SAVER_ACTIONS = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
]


def profiles(account: str) -> list[str]:
    """The inference profiles the workflow's steps use, as ARNs."""
    ids = sorted({bedrock.MODELS[model].model_id for model in workflow.STEP_MODELS.values()})
    return [f"arn:aws:bedrock:{bedrock.REGION}:{account}:inference-profile/{i}" for i in ids]


def foundation_models() -> list[str]:
    ids = sorted({bedrock.MODELS[model].model_id.removeprefix("us.") for model in workflow.STEP_MODELS.values()})
    return [f"arn:aws:bedrock:{region}::foundation-model/{i}" for i in ids for region in PROFILE_REGIONS]


def table_arn(account: str) -> str:
    return f"arn:aws:dynamodb:{bedrock.REGION}:{account}:table/{checkpoint_table.TABLE}"


def policy(account: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeTheWorkflowsInferenceProfiles",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": profiles(account),
            },
            {
                "Sid": "InvokeTheirModelsOnlyThroughThoseProfiles",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": foundation_models(),
                "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": profiles(account)}},
            },
            {
                "Sid": "ReadAndWriteCheckpoints",
                "Effect": "Allow",
                "Action": SAVER_ACTIONS,
                "Resource": table_arn(account),
            },
        ],
    }


def _through(profile_arn: str) -> list[dict]:
    """The simulator's stand-in for a request arriving through an inference profile."""
    return [
        {"ContextKeyName": "bedrock:InferenceProfileArn", "ContextKeyType": "string", "ContextKeyValues": [profile_arn]}
    ]


def cases(account: str) -> list[tuple]:
    """(what, actions, resources, context, expected): what the workflow does is allowed; everything else isn't."""
    sonnet, haiku = sorted(profiles(account), key=lambda arn: "haiku" in arn)
    model = foundation_models()[0]
    other = f"arn:aws:bedrock:{bedrock.REGION}:{account}:inference-profile/us.anthropic.claude-opus-4-6-v1"
    table = table_arn(account)
    guardrail = f"arn:aws:bedrock:{bedrock.REGION}:{account}:guardrail/*"
    return [
        ("call the workflow's profiles", ["bedrock:InvokeModel"], [sonnet, haiku], [], "allowed"),
        *[
            (
                f"reach {arn.split('/')[-1]} in {arn.split(':')[3]} through its profile",
                ["bedrock:InvokeModel"],
                [arn],
                _through(sonnet if "sonnet" in arn else haiku),
                "allowed",
            )
            for arn in foundation_models()
        ],
        ("checkpoint reads and writes", SAVER_ACTIONS, [table], [], "allowed"),
        ("call a foundation model directly", ["bedrock:InvokeModel"], [model], [], "implicitDeny"),
        ("call another model's profile", ["bedrock:InvokeModel"], [other], [], "implicitDeny"),
        (
            "stream, or use Bedrock API keys",
            ["bedrock:InvokeModelWithResponseStream", "bedrock:CallWithBearerToken"],
            [sonnet],
            [],
            "implicitDeny",
        ),
        (
            "use or change the guardrail",
            ["bedrock:ApplyGuardrail", "bedrock:UpdateGuardrail"],
            [guardrail],
            [],
            "implicitDeny",
        ),
        (
            "delete, scan or change the table",
            ["dynamodb:DeleteItem", "dynamodb:Scan", "dynamodb:DeleteTable", "dynamodb:UpdateTimeToLive"],
            [table],
            [],
            "implicitDeny",
        ),
        (
            "touch another table",
            ["dynamodb:GetItem", "dynamodb:PutItem"],
            [table.replace(checkpoint_table.TABLE, "another-table")],
            [],
            "implicitDeny",
        ),
        ("pass roles or change IAM", ["iam:PassRole", "iam:PutRolePolicy"], ["*"], [], "implicitDeny"),
    ]


def simulate(iam, account: str) -> list[str]:
    """Problems found (empty when every case comes out as expected)."""
    document = json.dumps(policy(account))
    problems = []
    for what, actions, resources, context, expected in cases(account):
        decisions = {}
        for resource in resources:  # one call each: with several resources the simulator reports one combined result
            results = iam.simulate_custom_policy(
                PolicyInputList=[document], ActionNames=actions, ResourceArns=[resource], ContextEntries=context
            )["EvaluationResults"]
            decisions |= {(r["EvalActionName"], resource): r["EvalDecision"] for r in results}
        wrong = {k: d for k, d in decisions.items() if d != expected}
        print(f"{'ok  ' if not wrong else 'FAIL'} {what}: {expected} ({len(decisions)} checks)")
        problems += [f"{what}: {action} on {resource} was {d}" for (action, resource), d in wrong.items()]
    return problems


def profile_regions_drifted(control) -> list[str]:
    """Foundation-model ARNs the live inference profiles route to that the policy doesn't cover."""
    covered = set(foundation_models())
    live = set()
    for model in sorted({bedrock.MODELS[m].model_id for m in workflow.STEP_MODELS.values()}):
        found = control.get_inference_profile(inferenceProfileIdentifier=model)
        live |= {m["modelArn"] for m in found["models"]}
    return sorted(live - covered)


def main() -> int:
    parser = argparse.ArgumentParser(description="The workflow's runtime policy: print it, or --simulate.")
    parser.add_argument("--simulate", action="store_true", help="check it with iam simulate-custom-policy")
    args = parser.parse_args()
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=bedrock.REGION)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
        if not args.simulate:
            print(json.dumps(policy(account), indent=2))
            return 0
        missing = profile_regions_drifted(session.client("bedrock"))
        problems = simulate(session.client("iam"), account)
        problems += [f"the profiles now also route to {arn}: add its Region to PROFILE_REGIONS" for arn in missing]
    except ClientError as e:
        print(f"AWS error: {e}", file=sys.stderr)
        return 1
    print("\n".join(["", *problems]) if problems else "\nEvery case came out as expected.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
