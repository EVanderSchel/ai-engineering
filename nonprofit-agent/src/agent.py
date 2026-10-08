"""The agent loop, built by hand on the Claude API: what every agent framework does under the hood.

1. Send the question, a system prompt, and the tool descriptions (TOOL_SPECS) to Claude.
2. If Claude answers, stop. If it asks for tools ("tool_use" blocks), run them (run_tool) and send the
   results back as "tool_result" blocks, then go to 2 with the whole conversation so far.
3. Stop early when a limit is hit: a number of model calls (steps), or a cost in dollars.

Claude never runs code. It only ever sees tool descriptions and tool results; our code decides what
actually happens, which is where limits, validation and (later) guardrails live.

Usage: python src/agent.py "How did the American Red Cross's revenue change from 2021 to 2023?"
"""

import argparse
import datetime
import json
from dataclasses import asdict, dataclass, field

import anthropic

import tools
from paths import DATA_DIR

MODEL = "claude-sonnet-5"
MAX_STEPS = 10  # model calls per question
MAX_COST = 0.25  # US dollars per question
MAX_TOKENS = 8000  # per model call, including thinking
RUNS_DIR = DATA_DIR / "runs"

# US dollars per million tokens: (input, output). Cache writes cost 1.25x input, cache reads 0.1x.
PRICES = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}

SYSTEM_PROMPT = """\
You research US tax-exempt organizations using public IRS data served by ProPublica's Nonprofit Explorer.

- Answer only from tool results, never from memory. If the tools can't answer something, say so and why.
- Find an organization's EIN with search_organizations before using the other tools, unless the user gave it.
  If several organizations could match, say which one you used (name, city, state, EIN).
- Use compute_ratios for ratios, percentages and changes between any two years instead of calculating them
  yourself.
- Search finds names containing your words, so it misses other spellings ("foodbank", "food reservoir",
  "food pantry"). When a question covers a group ("the largest ...", "all ... in Iowa"), search more than one
  way, and never claim a list is complete or a ranking is final beyond what you actually checked: say what
  you searched for.
- When a year has no data, say whether it was filed without figures yet or not filed at all, as the tool
  result states; don't call an earlier year "the most recent filing" when a later one was filed.
- Name the tax year and form for every figure you report. A tax year is the year the organization's fiscal
  year ended; say so when it doesn't match the calendar year.
- A null field means the form doesn't report it, not zero.
- Be concise: lead with the answer, then the figures that support it."""

# What Claude sees of each tool: a name, when and why to call it, and a JSON Schema for its input.
TOOL_SPECS = [
    {
        "name": "search_organizations",
        "description": (
            "Search US tax-exempt organizations by name or keyword. Call this first whenever the user names "
            "an organization without giving its EIN, or asks about a kind of organization (e.g. food banks). "
            "Returns up to 25 matches per page with EIN, name, city, state and NTEE category code, plus the "
            "total number of matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name or keywords, e.g. 'american red cross'."},
                "state": {"type": "string", "description": "Optional 2-letter US state code, e.g. 'IA'."},
                "page": {"type": "integer", "description": "Result page, starting at 0.", "default": 0},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_organization",
        "description": (
            "Profile of one organization by EIN: name, location, NTEE code, 501(c) subsection, tax-exempt "
            "since, and which tax years have financial data versus were filed only as PDFs (no figures). Call "
            "this to confirm you have the right organization or to see which years can be answered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ein": {"type": "string", "description": "EIN, e.g. '53-0196605'."}},
            "required": ["ein"],
        },
    },
    {
        "name": "get_financials",
        "description": (
            "Financial figures from an organization's IRS filings, in whole US dollars: total revenue, "
            "expenses, assets and liabilities on every form; on the full Form 990 also contributions and "
            "grants, program service revenue, officer compensation, other salaries and wages, and professional "
            "fundraising fees. Call this for any question about an organization's finances. Omit tax_year to "
            "get every year with data (newest first)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ein": {"type": "string", "description": "EIN, e.g. '53-0196605'."},
                "tax_year": {"type": "integer", "description": "Optional tax year, e.g. 2023."},
            },
            "required": ["ein"],
        },
    },
    {
        "name": "compute_ratios",
        "description": (
            "Ratios for one organization and tax year, computed exactly: surplus and surplus margin, "
            "liabilities to assets, contributions share of revenue, officer compensation share of expenses, "
            "and the change in revenue, expenses and assets from another year (amount and fraction, e.g. "
            "0.05 = 5%). Call this for any ratio, percentage or change between years instead of doing "
            "arithmetic yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ein": {"type": "string", "description": "EIN, e.g. '53-0196605'."},
                "tax_year": {"type": "integer", "description": "Tax year, e.g. 2023."},
                "compare_with_year": {
                    "type": "integer",
                    "description": (
                        "Optional earlier (or later) tax year to measure the changes from, e.g. 2021 for "
                        "'from 2021 to 2023'. Default: the previous year with data."
                    ),
                },
            },
            "required": ["ein", "tax_year"],
        },
    },
]

TOOL_FUNCTIONS = {
    "search_organizations": tools.search_organizations,
    "get_organization": tools.get_organization,
    "get_financials": tools.get_financials,
    "compute_ratios": tools.compute_ratios,
}


# --- Running tools -----------------------------------------------------------------------------------


def run_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Run one tool call from Claude. Returns (result as JSON text, is_error). Never raises: every failure
    goes back to Claude as an error result it can react to (try another EIN, another year, ...)."""
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return f"Unknown tool {name!r}. Available tools: {sorted(TOOL_FUNCTIONS)}.", True
    try:
        return json.dumps(function(**tool_input)), False
    except tools.ToolError as e:
        return str(e), True
    except TypeError as e:  # wrong or missing arguments
        return f"Invalid arguments for {name}: {e}", True
    except Exception as e:  # network failure, API down, ...: the agent should report it, not crash
        return f"{name} failed: {type(e).__name__}: {e}. The data source may be unavailable; try again later.", True


# --- The loop ----------------------------------------------------------------------------------------


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, usage) -> None:
        for name in asdict(self):
            setattr(self, name, getattr(self, name) + (getattr(usage, name, 0) or 0))

    def cost(self, model: str) -> float:
        input_price, output_price = PRICES[model]
        return (
            self.input_tokens * input_price
            + self.cache_creation_input_tokens * input_price * 1.25
            + self.cache_read_input_tokens * input_price * 0.10
            + self.output_tokens * output_price
        ) / 1_000_000


@dataclass
class Result:
    question: str
    model: str
    answer: str | None = None
    # Why the loop ended: "answered", or a limit/problem: "max_steps", "max_cost", "max_tokens", "refusal", ...
    stop: str = ""
    steps: int = 0  # model calls
    # Everything the agent did, in order: {"type": "text", ...} and {"type": "tool", "name", "input", "output",
    # "is_error"}. This is the trajectory that step 3's evals score.
    trajectory: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    @property
    def cost(self) -> float:
        return self.usage.cost(self.model)


def run(
    client: anthropic.Anthropic,
    question: str,
    *,
    model: str = MODEL,
    max_steps: int = MAX_STEPS,
    max_cost: float = MAX_COST,
    on_event=None,
) -> Result:
    """Answer one question with tools. on_event(dict), if given, is called with each trajectory entry as
    it happens (the CLI prints them)."""
    result = Result(question=question, model=model)
    messages = [{"role": "user", "content": question}]

    def record(event: dict) -> None:
        result.trajectory.append(event)
        if on_event:
            on_event(event)

    while True:
        if result.steps >= max_steps:
            result.stop = "max_steps"
            return result
        if result.cost >= max_cost:
            result.stop = "max_cost"
            return result

        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            # The system prompt and tools are identical on every call: mark them for caching. The top-level
            # cache_control also caches the growing conversation, so each call re-reads the previous one's
            # history at a tenth of the price instead of paying for it again.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=TOOL_SPECS,
            messages=messages,
            cache_control={"type": "ephemeral"},
        )
        result.steps += 1
        result.usage.add(response.usage)

        for block in response.content:
            if block.type == "text" and block.text.strip():
                record({"type": "text", "text": block.text})

        if response.stop_reason == "end_turn":
            result.answer = "\n".join(b.text for b in response.content if b.type == "text").strip()
            result.stop = "answered"
            return result
        if response.stop_reason != "tool_use":
            # "max_tokens" (ran out of room mid-answer), "refusal" (declined for safety), ...: stop and say why.
            result.stop = response.stop_reason
            return result

        # Claude asked for one or more tools. Keep its whole message (including thinking and tool_use blocks:
        # the API needs them back to continue), run every tool, and send all results in one user message,
        # each matched to its request by tool_use_id.
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            output, is_error = run_tool(block.name, block.input)
            record({"type": "tool", "name": block.name, "input": block.input, "output": output, "is_error": is_error})
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
            )
        messages.append({"role": "user", "content": tool_results})


# --- Command line ------------------------------------------------------------------------------------


def show(event: dict) -> None:
    if event["type"] == "text":
        print(f"\n[claude] {event['text']}")
    else:
        flag = " ERROR" if event["is_error"] else ""
        output = event["output"] if len(event["output"]) <= 300 else event["output"][:300] + " ..."
        print(f"\n[tool{flag}] {event['name']}({json.dumps(event['input'])})\n  -> {output}")


def save(result: Result) -> str:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
    record = asdict(result) | {"cost": round(result.cost, 6)}
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--max-cost", type=float, default=MAX_COST)
    args = parser.parse_args()

    result = run(
        anthropic.Anthropic(),
        args.question,
        model=args.model,
        max_steps=args.max_steps,
        max_cost=args.max_cost,
        on_event=show,
    )
    tools_used = sum(e["type"] == "tool" for e in result.trajectory)
    print(f"\n--- stop: {result.stop} | {result.steps} model calls, {tools_used} tool calls | ${result.cost:.4f}")
    print(f"saved {save(result)}")


if __name__ == "__main__":
    main()
