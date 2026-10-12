"""Step 4: the same agent on the SDK's Tool Runner (client.beta.messages.tool_runner) instead of the
hand-built loop in src/agent.py, so the two can be compared on the eval.

Everything that defines the agent is shared, so a difference in results comes from the loop alone: the
model, system prompt, tool names, descriptions and schemas, limits, tool execution (agent.run_tool) and the
grounding guardrail. What the runner takes over: calling the API, finding tool_use blocks, running the tools,
matching results to requests by id, appending messages, and stopping on end_turn or max_iterations.

What it doesn't do, and this file adds (found by reading the SDK source, anthropic 1.12.1):
- A cost limit. The runner counts iterations, not dollars, so the loop body adds up usage and stops.
- Our tool errors. Only the SDK's own ToolError becomes a clean is_error result; any other exception is sent
  to the model as repr(exc) ("ToolError('No financial data...')") and logged with a stack trace. The
  adapter below raises the SDK's ToolError with the same message the hand-built loop sends.
- A check of the final answer. end_turn always ends a runner, whatever is appended to it, so the grounding
  guardrail keeps its own copy of the conversation and starts a new runner with the feedback added (the same
  pattern the SDK documents for resuming pause_turn).
- A record of what happened. The runner keeps its message history private, so the trajectory is rebuilt from
  each yielded message plus runner.generate_tool_call_response(), which runs that turn's tools and caches the
  result so the runner doesn't run them a second time.

Usage: python src/runner_agent.py "How did Iowa Public Radio's revenue change from 2020 to 2023?"
"""

import argparse

import anthropic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError

import agent
import grounding


def _runner_tool(spec: dict):
    """One tool for the runner, with exactly the hand-built loop's description, schema and behavior."""

    def call(**tool_input) -> str:
        output, is_error = agent.run_tool(spec["name"], tool_input)
        if is_error:
            raise ToolError(output)  # the runner sends output as an is_error tool_result
        return output

    return beta_tool(call, name=spec["name"], description=spec["description"], input_schema=spec["input_schema"])


RUNNER_TOOLS = [_runner_tool(spec) for spec in agent.TOOL_SPECS]


def _text(content) -> str:
    """A tool_result's content as text (the runner may send a string or a list of text blocks)."""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content)


def run(
    client: anthropic.Anthropic,
    question: str,
    *,
    model: str = agent.MODEL,
    max_steps: int = agent.MAX_STEPS,
    max_cost: float = agent.MAX_COST,
    on_event=None,
) -> agent.Result:
    """Answer one question with tools, like agent.run, using the Tool Runner for the loop."""
    result = agent.Result(question=question, model=model)
    messages: list = [{"role": "user", "content": question}]  # our copy; the runner's is private

    def record(event: dict) -> None:
        result.trajectory.append(event)
        if on_event:
            on_event(event)

    while True:
        if result.steps >= max_steps:
            result.stop = "max_steps"
            return result
        runner = client.beta.messages.tool_runner(
            model=model,
            max_tokens=agent.MAX_TOKENS,
            system=[{"type": "text", "text": agent.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=RUNNER_TOOLS,
            messages=list(messages),
            cache_control={"type": "ephemeral"},
            max_iterations=max_steps - result.steps,  # model calls left in the step budget
        )
        last = None
        for message in runner:
            last = message
            result.steps += 1
            result.usage.add(message.usage)
            result.served_models.append(message.model)
            for block in message.content:
                if block.type == "text" and block.text.strip():
                    record({"type": "text", "text": block.text})
            messages.append({"role": "assistant", "content": message.content})

            # Runs this turn's tools now (the runner reuses the cached results instead of running them again).
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                requests = {b.id: b for b in message.content if b.type == "tool_use"}
                for tool_result in tool_response["content"]:
                    block = requests[tool_result["tool_use_id"]]
                    record(
                        {
                            "type": "tool",
                            "name": block.name,
                            "input": block.input,
                            "output": _text(tool_result["content"]),
                            "is_error": bool(tool_result.get("is_error")),
                        }
                    )
                messages.append(tool_response)

            if result.cost >= max_cost:  # the runner has no cost limit: stop iterating it
                result.stop = "max_cost"
                return result

        if last is None or last.stop_reason == "tool_use":  # max_iterations reached with tools still pending
            result.stop = "max_steps"
            return result
        if last.stop_reason != "end_turn":  # max_tokens, refusal, ...
            result.stop = last.stop_reason
            return result

        result.answer = "\n".join(b.text for b in last.content if b.type == "text").strip()
        result.stop = "answered"
        # The grounding guardrail, as in agent.run: send an ungrounded draft back once, with a new runner.
        stray = [grounding.describe(f) for f in grounding.ungrounded(result.answer, result.trajectory)]
        result.ungrounded = stray
        out_of_budget = result.steps >= max_steps or result.cost >= max_cost
        if not stray or result.grounding_retries >= agent.MAX_GROUNDING_RETRIES or out_of_budget:
            return result
        result.grounding_retries += 1
        feedback = agent.GROUNDING_FEEDBACK.format(figures=", ".join(stray))
        record({"type": "guardrail", "figures": stray, "text": feedback})
        messages.append({"role": "user", "content": feedback})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("--model", default=agent.MODEL, choices=sorted(agent.PRICES))
    args = parser.parse_args()
    result = run(anthropic.Anthropic(), args.question, model=args.model, on_event=agent.show)
    tools_used = sum(e["type"] == "tool" for e in result.trajectory)
    print(f"\n--- stop: {result.stop} | {result.steps} model calls, {tools_used} tool calls | ${result.cost:.4f}")
    if result.ungrounded:
        print(f"WARNING: figures not in any tool result: {', '.join(result.ungrounded)}")


if __name__ == "__main__":
    main()
