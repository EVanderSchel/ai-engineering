"""The Bedrock client, tested without AWS. botocore's Stubber replaces the network layer of a real boto3
client and checks every request and canned response against Bedrock's own API definition, so a misspelled
parameter or a malformed tool config fails here the way the real service would reject it."""

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from pydantic import BaseModel

import bedrock


class Verdict(BaseModel):
    """Whether the claim holds."""

    holds: bool
    reason: str


@pytest.fixture
def stubbed():
    """A real bedrock-runtime client with fake credentials, its calls answered by a Stubber."""
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=bedrock.REGION)
    client = session.client("bedrock-runtime")
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def reply(content, stop_reason="end_turn", input_tokens=1000, output_tokens=200):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        },
        "metrics": {"latencyMs": 850},
    }


def test_converse_sends_the_model_system_prompt_and_max_tokens(stubbed):
    client, stubber = stubbed
    stubber.add_response(
        "converse",
        reply([{"text": "A summary."}]),
        expected_params={
            "modelId": "us.anthropic.claude-sonnet-4-6",
            "system": [{"text": "Be brief."}],
            "messages": [{"role": "user", "content": [{"text": "Summarize."}]}],
            "inferenceConfig": {"maxTokens": 300},
        },
    )
    text, record = bedrock.converse(
        client, step="test", model="sonnet", system="Be brief.", content=[{"text": "Summarize."}], max_tokens=300
    )
    assert text == "A summary."
    assert (record.step, record.model, record.input_tokens, record.output_tokens) == ("test", "sonnet", 1000, 200)
    assert record.latency_ms == 850 and record.stop_reason == "end_turn"


def test_cost_uses_the_models_us_profile_prices():
    sonnet, haiku = bedrock.MODELS["sonnet"], bedrock.MODELS["haiku"]
    usage = {"inputTokens": 1_000_000, "outputTokens": 1_000_000}
    assert bedrock.cost_usd(sonnet, usage) == pytest.approx(3.30 + 16.50)
    assert bedrock.cost_usd(haiku, usage) == pytest.approx(1.10 + 5.50)
    cached = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 1_000_000,
        "cacheWriteInputTokens": 1_000_000,
    }
    assert bedrock.cost_usd(sonnet, cached) == pytest.approx(0.33 + 4.125)


def test_the_record_carries_the_cost(stubbed):
    client, stubber = stubbed
    stubber.add_response("converse", reply([{"text": "ok"}], input_tokens=2000, output_tokens=100))
    _, record = bedrock.converse(client, step="t", model="haiku", system="s", content=[{"text": "q"}], max_tokens=50)
    assert record.cost_usd == pytest.approx((2000 * 1.10 + 100 * 5.50) / 1_000_000)


def test_structured_forces_a_single_tool_built_from_the_schema(stubbed):
    client, stubber = stubbed
    tool_use = {"toolUse": {"toolUseId": "t1", "name": "Verdict", "input": {"holds": True, "reason": "It matches."}}}
    stubber.add_response(
        "converse",
        reply([tool_use], stop_reason="tool_use"),
        expected_params={
            "modelId": ANY,
            "system": ANY,
            "messages": ANY,
            "inferenceConfig": {"maxTokens": 400},
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": "Verdict",
                            "description": "Whether the claim holds.",
                            "inputSchema": {"json": Verdict.model_json_schema()},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": "Verdict"}},
            },
        },
    )
    verdict, record = bedrock.structured(
        client, step="t", model="sonnet", system="s", content=[{"text": "q"}], schema=Verdict, max_tokens=400
    )
    assert verdict == Verdict(holds=True, reason="It matches.")
    assert record.stop_reason == "tool_use"


def test_structured_output_that_breaks_the_schema_is_rejected(stubbed):
    client, stubber = stubbed
    bad = {"toolUse": {"toolUseId": "t1", "name": "Verdict", "input": {"holds": "maybe"}}}
    stubber.add_response("converse", reply([bad], stop_reason="tool_use"))
    stubber.add_response("converse", reply([bad], stop_reason="tool_use"))  # the retry fails too
    with pytest.raises(bedrock.InvalidStructuredOutput):
        bedrock.structured(
            client, step="t", model="sonnet", system="s", content=[{"text": "q"}], schema=Verdict, max_tokens=400
        )


def test_structured_output_without_the_tool_call_is_rejected(stubbed):
    client, stubber = stubbed
    stubber.add_response("converse", reply([{"text": "I'd rather just say it holds."}]))
    stubber.add_response("converse", reply([{"text": "Still just text."}]))
    with pytest.raises(bedrock.InvalidStructuredOutput):
        bedrock.structured(
            client, step="t", model="sonnet", system="s", content=[{"text": "q"}], schema=Verdict, max_tokens=400
        )


def test_output_cut_off_at_max_tokens_raises_instead_of_returning_half_an_answer(stubbed):
    client, stubber = stubbed
    stubber.add_response("converse", reply([{"text": "The organization rep"}], stop_reason="max_tokens"))
    with pytest.raises(bedrock.TruncatedOutput):
        bedrock.converse(client, step="t", model="haiku", system="s", content=[{"text": "q"}], max_tokens=5)


def test_access_errors_propagate_unchanged(stubbed):
    """Not swallowed or retried: an access problem should stop the run and be visible."""
    client, stubber = stubbed
    stubber.add_client_error("converse", service_error_code="AccessDeniedException", http_status_code=403)
    with pytest.raises(ClientError) as raised:
        bedrock.converse(client, step="t", model="sonnet", system="s", content=[{"text": "q"}], max_tokens=10)
    assert raised.value.response["Error"]["Code"] == "AccessDeniedException"


def test_client_is_the_runtime_endpoint_in_the_project_region_with_adaptive_retries(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    client = bedrock.make_client()
    assert client.meta.service_model.service_name == "bedrock-runtime"
    assert client.meta.region_name == "us-east-2"
    assert client.meta.config.retries["mode"] == "adaptive"


def test_every_model_uses_a_us_inference_profile():
    assert all(m.model_id.startswith("us.anthropic.") for m in bedrock.MODELS.values())


def test_an_answer_that_breaks_the_schema_is_asked_for_once_more(stubbed):
    """The returned record covers both attempts, so a retry shows up in the cost instead of hiding."""
    client, stubber = stubbed
    bad = {"toolUse": {"toolUseId": "t1", "name": "Verdict", "input": {"holds": True, "reason": ["not", "text"]}}}
    good = {"toolUse": {"toolUseId": "t2", "name": "Verdict", "input": {"holds": True, "reason": "It matches."}}}
    stubber.add_response("converse", reply([bad], stop_reason="tool_use", input_tokens=1000, output_tokens=100))
    stubber.add_response("converse", reply([good], stop_reason="tool_use", input_tokens=1000, output_tokens=100))
    verdict, record = bedrock.structured(
        client, step="t", model="haiku", system="s", content=[{"text": "q"}], schema=Verdict, max_tokens=400
    )
    assert verdict.reason == "It matches." and record.attempts == 2
    assert (record.input_tokens, record.output_tokens) == (2000, 200)
    assert record.cost_usd == pytest.approx(2 * (1000 * 1.10 + 100 * 5.50) / 1_000_000)


def test_retries_boto3_makes_on_its_own_are_recorded(stubbed):
    client, stubber = stubbed
    response = reply([{"text": "ok"}])
    response["ResponseMetadata"] = {"RetryAttempts": 2, "HTTPStatusCode": 200}
    stubber.add_response("converse", response)
    _, record = bedrock.converse(client, step="t", model="haiku", system="s", content=[{"text": "q"}], max_tokens=10)
    assert record.sdk_retries == 2
