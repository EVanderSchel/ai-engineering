"""The runtime policy, without AWS: its shape, and the simulator and drift checks against Stubber."""

import boto3
import pytest
from botocore.stub import ANY, Stubber

import iam_policy

ACCOUNT = "111122223333"


def statements():
    return {s["Sid"]: s for s in iam_policy.policy(ACCOUNT)["Statement"]}


def test_every_statement_allows_and_names_its_resources():
    for s in statements().values():
        assert s["Effect"] == "Allow" and s["Resource"] != "*" and "*" not in str(s["Action"])
        assert all("*" not in arn for arn in ([s["Resource"]] if isinstance(s["Resource"], str) else s["Resource"]))


def test_models_are_reachable_only_through_the_workflows_profiles():
    found = statements()
    profiles = found["InvokeTheWorkflowsInferenceProfiles"]["Resource"]
    assert [p.split("/")[-1] for p in profiles] == [
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-4-6",
    ]
    via = found["InvokeTheirModelsOnlyThroughThoseProfiles"]
    assert via["Condition"] == {"StringEquals": {"bedrock:InferenceProfileArn": profiles}}
    assert len(via["Resource"]) == 2 * len(iam_policy.PROFILE_REGIONS)


def test_checkpoints_get_only_what_the_saver_calls():
    found = statements()["ReadAndWriteCheckpoints"]
    assert found["Resource"] == f"arn:aws:dynamodb:us-east-2:{ACCOUNT}:table/grant-review-checkpoints"
    assert not {"dynamodb:DeleteItem", "dynamodb:Scan", "dynamodb:DeleteTable"} & set(found["Action"])


def stubbed(service):
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-2")
    client = session.client(service)
    return client, Stubber(client)


def evaluations(actions, resources, decision):
    return {
        "EvaluationResults": [
            {"EvalActionName": a, "EvalResourceName": r, "EvalDecision": decision} for a in actions for r in resources
        ]
    }


@pytest.mark.parametrize("flip", [None, 0, -1])
def test_simulate_reports_every_case_that_comes_out_wrong(flip, capsys):
    client, stubber = stubbed("iam")
    cases = iam_policy.cases(ACCOUNT)
    for i, (_, actions, resources, context, expected) in enumerate(cases):
        wrong = {"allowed": "implicitDeny", "implicitDeny": "allowed"}[expected]
        decision = wrong if flip is not None and i == range(len(cases))[flip] else expected
        for resource in resources:
            params = {
                "PolicyInputList": [ANY],
                "ActionNames": actions,
                "ResourceArns": [resource],
                "ContextEntries": context,
            }
            stubber.add_response("simulate_custom_policy", evaluations(actions, [resource], decision), params)
    with stubber:
        problems = iam_policy.simulate(client, ACCOUNT)
        stubber.assert_no_pending_responses()
    assert len(problems) == (0 if flip is None else len(cases[flip][1]) * len(cases[flip][2]))
    assert ("FAIL" in capsys.readouterr().out) == (flip is not None)


def test_drift_check_finds_a_region_the_policy_doesnt_cover():
    client, stubber = stubbed("bedrock")
    extra = "arn:aws:bedrock:us-west-1::foundation-model/anthropic.claude-sonnet-4-6"
    for model_id in sorted(arn.split("/")[-1] for arn in iam_policy.profiles(ACCOUNT)):
        arns = [a for a in iam_policy.foundation_models() if model_id.removeprefix("us.") in a]
        if "sonnet" in model_id:
            arns.append(extra)
        response = {
            "inferenceProfileName": model_id,
            "inferenceProfileArn": f"arn:aws:bedrock:us-east-2:{ACCOUNT}:inference-profile/{model_id}",
            "inferenceProfileId": model_id,
            "status": "ACTIVE",
            "type": "SYSTEM_DEFINED",
            "models": [{"modelArn": a} for a in arns],
        }
        stubber.add_response("get_inference_profile", response, {"inferenceProfileIdentifier": model_id})
    with stubber:
        assert iam_policy.profile_regions_drifted(client) == [extra]
