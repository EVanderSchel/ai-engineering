"""Calling Claude through Amazon Bedrock's Converse API, with a cost and latency record for every call.

Every model step in the workflow goes through `converse` (free text) or `structured` (a Pydantic object), so
model choice, token limits, retries and cost accounting live in one place.

    $env:AWS_PROFILE = "agent-toolkit"; uv run python src/bedrock.py    # one live call per model

Region and credentials: the client always uses REGION (the project's AWS Region; nothing is created or
called elsewhere). Credentials come from boto3's usual chain: AWS_PROFILE locally (after `aws login`), an IAM
role when deployed.
"""

import dataclasses
import os
import sys
import time
from collections.abc import Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, ValidationError

REGION = "us-east-2"


@dataclasses.dataclass(frozen=True)
class Model:
    """A Claude model on Bedrock, with its on-demand prices in USD per million tokens."""

    model_id: str
    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute cache


# Called through `us.` cross-Region inference profiles: every Claude model on this account requires an
# inference profile, and `us.` keeps requests in US Regions. Prices are the us-east-2 on-demand rates for
# those profiles from AWS's public price list (AmazonBedrockFoundationModels, read 2026-10-20). They are 10%
# above the `global.` profiles' rates: the price of keeping data in the US.
MODELS = {
    "sonnet": Model("us.anthropic.claude-sonnet-4-6", input=3.30, output=16.50, cache_read=0.33, cache_write=4.125),
    "haiku": Model(
        "us.anthropic.claude-haiku-4-5-20251001-v1:0", input=1.10, output=5.50, cache_read=0.11, cache_write=1.375
    ),
}


class CallRecord(BaseModel, frozen=True):
    """What one model call cost and how long it took; the workflow keeps one per call in its state (a
    Pydantic model so it's saved with the state's checkpoints)."""

    step: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    latency_ms: int  # measured by Bedrock: time spent in the model
    wall_ms: int  # measured here: includes network and any retries
    stop_reason: str
    attempts: int = 1  # more than 1: the first answer didn't fit the schema; tokens and cost cover every attempt
    sdk_retries: int = (
        0  # retries boto3 made on its own (throttling, busy service): the gap between wall_ms and latency_ms
    )

    def plus(self, other: CallRecord) -> CallRecord:
        """This call and a retry of it, as one record."""
        return self.model_copy(
            update={
                "input_tokens": self.input_tokens + other.input_tokens,
                "output_tokens": self.output_tokens + other.output_tokens,
                "cache_read_tokens": self.cache_read_tokens + other.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens + other.cache_write_tokens,
                "cost_usd": self.cost_usd + other.cost_usd,
                "latency_ms": self.latency_ms + other.latency_ms,
                "wall_ms": self.wall_ms + other.wall_ms,
                "stop_reason": other.stop_reason,
                "attempts": self.attempts + other.attempts,
                "sdk_retries": self.sdk_retries + other.sdk_retries,
            }
        )


class TruncatedOutput(RuntimeError):
    """The model hit max_tokens before finishing. Raised rather than returning a cut-off answer."""


class InvalidStructuredOutput(RuntimeError):
    """The model's structured output didn't match the schema."""


def make_client(profile: str | None = None):
    """A bedrock-runtime client (the endpoint that runs models; `bedrock` is the management one).

    Created once and shared: boto3 clients are thread-safe. Adaptive retries back off and retry throttling
    and transient server errors (up to 5 attempts in all); validation and access errors are raised at once.
    """
    session = boto3.Session(profile_name=profile or os.environ.get("AWS_PROFILE"), region_name=REGION)
    config = Config(retries={"total_max_attempts": 5, "mode": "adaptive"}, connect_timeout=10, read_timeout=120)
    return session.client("bedrock-runtime", config=config)


def cost_usd(model: Model, usage: dict) -> float:
    """The price of one call, from the token counts Bedrock reports."""
    per_token = {
        "inputTokens": model.input,
        "outputTokens": model.output,
        "cacheReadInputTokens": model.cache_read,
        "cacheWriteInputTokens": model.cache_write,
    }
    return sum(usage.get(key, 0) * price for key, price in per_token.items()) / 1_000_000


def _call(client, *, step: str, model: str, system: str, messages: list[dict], max_tokens: int, **extra):
    spec = MODELS[model]
    started = time.perf_counter()
    response = client.converse(
        modelId=spec.model_id,
        system=[{"text": system}],
        messages=messages,
        # Always set: Bedrock reserves quota against maxTokens, and the default is the model's maximum.
        inferenceConfig={"maxTokens": max_tokens},
        **extra,
    )
    usage = response["usage"]
    record = CallRecord(
        step=step,
        model=model,
        input_tokens=usage["inputTokens"],
        output_tokens=usage["outputTokens"],
        cache_read_tokens=usage.get("cacheReadInputTokens", 0),
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0),
        cost_usd=cost_usd(spec, usage),
        latency_ms=response["metrics"]["latencyMs"],
        wall_ms=round((time.perf_counter() - started) * 1000),
        stop_reason=response["stopReason"],
        sdk_retries=response.get("ResponseMetadata", {}).get("RetryAttempts", 0),
    )
    if response["stopReason"] == "max_tokens":
        raise TruncatedOutput(f"{step}: output reached max_tokens={max_tokens}")
    return response["output"]["message"], record


def converse(client, *, step: str, model: str, system: str, content: list[dict], max_tokens: int):
    """One user turn, free-text answer. `content` is Converse content blocks, e.g. [{"text": ...}].
    Returns (text, CallRecord)."""
    message, record = _call(
        client,
        step=step,
        model=model,
        system=system,
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
    )
    text = "".join(block["text"] for block in message["content"] if "text" in block)
    return text, record


def structured[T: BaseModel](
    client, *, step: str, model: str, system: str, content: list[dict], schema: type[T], max_tokens: int
) -> tuple[T, CallRecord]:
    """One user turn whose answer must fit `schema`.

    Converse has no JSON mode for Claude, so the schema becomes the input schema of a single tool, and
    toolChoice forces the model to call it: the tool call's input is the answer. Pydantic then validates it,
    because a schema given to a model is a request, not a guarantee (Haiku once returned a list as a JSON
    string). An answer that doesn't fit is asked for once more; the returned record covers both attempts.
    """
    tool_name = schema.__name__
    tool_config = {"tools": [_schema_tool(schema)], "toolChoice": {"tool": {"name": tool_name}}}
    record = None
    for attempt in (1, 2):
        message, this_call = _call(
            client,
            step=step,
            model=model,
            system=system,
            messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens,
            toolConfig=tool_config,
        )
        record = this_call if record is None else record.plus(this_call)
        calls = [block["toolUse"] for block in message["content"] if "toolUse" in block]
        try:
            if len(calls) != 1 or calls[0]["name"] != tool_name:
                raise InvalidStructuredOutput(f"{step}: expected one {tool_name} call, got {len(calls)}")
            try:
                return schema.model_validate(calls[0]["input"]), record
            except ValidationError as e:
                raise InvalidStructuredOutput(f"{step}: {e}") from e
        except InvalidStructuredOutput:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _schema_tool(schema: type[BaseModel]) -> dict:
    """A Pydantic model as a Converse tool: calling the tool means answering in that shape."""
    name = schema.__name__
    description = (schema.__doc__ or f"Record the {name}.").strip()
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema.model_json_schema()}}}


def tool_loop[T: BaseModel](
    client,
    *,
    step: str,
    model: str,
    system: str,
    content: list[dict],
    tools: dict[str, Callable[..., dict]],
    tool_specs: dict[str, dict],
    finish: type[T],
    max_tokens: int,
    max_turns: int = 6,
    max_cost_usd: float = 0.10,
    tool_errors: tuple[type[Exception], ...] = (),
) -> tuple[T, list[CallRecord], list[dict]]:
    """A tool-using agent: the model calls `tools` until it calls the `finish` tool, whose input is the answer.

    Every turn the model must call some tool (toolChoice "any"). Each tool call is run and its result sent back
    as a toolResult matched by toolUseId; a tool that fails (one of `tool_errors`, an unknown tool, or wrong
    arguments) returns status "error" with its message, so the model can correct itself. On the last allowed
    turn, or once the calls have cost max_cost_usd, the model is forced to call `finish` with what it has.

    Returns (answer, one CallRecord per model call, trajectory: every tool call with its input and outcome).
    """
    specs = [{"toolSpec": {"name": name, **spec}} for name, spec in tool_specs.items()] + [_schema_tool(finish)]
    messages = [{"role": "user", "content": content}]
    calls: list[CallRecord] = []
    trajectory: list[dict] = []
    for turn in range(max_turns):
        must_finish = turn == max_turns - 1 or sum(c.cost_usd for c in calls) >= max_cost_usd
        choice = {"tool": {"name": finish.__name__}} if must_finish else {"any": {}}
        message, record = _call(
            client,
            step=step,
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            toolConfig={"tools": specs, "toolChoice": choice},
        )
        calls.append(record)
        messages.append(message)
        uses = [block["toolUse"] for block in message["content"] if "toolUse" in block]
        done = next((use for use in uses if use["name"] == finish.__name__), None)
        if done is not None:
            trajectory.append({"tool": finish.__name__, "forced": must_finish})
            try:
                return finish.model_validate(done["input"]), calls, trajectory
            except ValidationError as e:
                raise InvalidStructuredOutput(f"{step}: {e}") from e
        results = []
        for use in uses:
            try:
                output = tools[use["name"]](**use["input"])
                result = {"toolUseId": use["toolUseId"], "content": [{"json": output}], "status": "success"}
            except (*tool_errors, KeyError, TypeError) as e:
                message_text = f"Unknown tool {use['name']}" if isinstance(e, KeyError) else str(e)
                result = {"toolUseId": use["toolUseId"], "content": [{"text": message_text}], "status": "error"}
            trajectory.append({"tool": use["name"], "input": use["input"], "status": result["status"]})
            results.append({"toolResult": result})
        messages.append({"role": "user", "content": results})
    raise InvalidStructuredOutput(f"{step}: no {finish.__name__} call within {max_turns} turns")


class _Check(BaseModel):
    """A one-sentence answer and how confident the model is in it."""

    answer: str
    confident: bool


def main() -> int:
    """One small structured call per model: confirms access, the tool-based structured output, and costs."""
    client = make_client()
    content = [{"text": "In one sentence: what does an IRS Form 990 report?"}]
    try:
        for model in MODELS:
            result, record = structured(
                client,
                step="smoke",
                model=model,
                system="Answer briefly.",
                content=content,
                schema=_Check,
                max_tokens=200,
            )
            print(f"{model} ({MODELS[model].model_id}): {result.answer}")
            print(
                f"  {record.input_tokens} in / {record.output_tokens} out tokens, ${record.cost_usd:.6f}, "
                f"model {record.latency_ms} ms, wall {record.wall_ms} ms, stop: {record.stop_reason}"
            )
    except ClientError as e:
        print(f"Bedrock error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
