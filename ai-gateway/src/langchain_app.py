"""A LangChain app that uses a gateway unchanged (step 10 stage 4): the same code against our gateway and against
LiteLLM's proxy, with only the base URL and key different. Free: both call the fake upstream.

    docker compose --profile compare up -d --build --wait
    uv run python src/langchain_app.py            # writes results/compare/langchain.{json,md}

Plain language: one customer with one order form, taken to two waiters. Does each waiter take every kind of order on
the form, and does anything come back looking different?

The app is a small one: it summarizes nonprofit missions (a prompt template, the model and an output parser chained
together, LangChain's basic building block). Each FEATURES entry exercises one thing a LangChain app commonly uses:
streaming, batching, token counts, structured output, tools, and what happens on an error. Every entry is run against
each gateway with ChatOpenAI pointed at it, and its outcome is recorded as it happened: the answer's shape, or the
exception LangChain raised. Nothing is retried (max_retries=0), so an error shows as the gateway sent it.
"""

import asyncio
import datetime as dt
import json
import sys
from collections.abc import Callable

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from paths import RESULTS_DIR

OUT_DIR = RESULTS_DIR / "compare"

PROMPT = ChatPromptTemplate.from_messages(
    [("system", "Summarize the nonprofit's mission in one plain sentence."), ("user", "{mission}")]
)
MISSIONS = [  # invented, so nothing here is a real organization's text
    "We restore wetlands along the river and teach schoolchildren to monitor water quality.",
    "We run a food pantry and a weekly hot meal for families in the county.",
    "We train volunteers to tutor adults who are learning to read.",
    "We provide free legal clinics for tenants facing eviction.",
    "We rescue, treat and rehome injured wild birds.",
]


class MissionSummary(BaseModel):
    """A mission, summarized and categorized."""

    summary: str = Field(description="The mission in one sentence")
    ntee_letter: str = Field(description="The NTEE major group letter, A to Z")


@tool
def lookup_organization(name: str) -> str:
    """Look up a nonprofit by name and return its EIN."""
    return "00-0000000"  # never runs here: the checks only look at whether the model asked for it


def chat_model(base_url: str, key: str, model: str = "chat", **kwargs) -> ChatOpenAI:
    """The only gateway-specific line in the app: where to send requests, and the key to send.

    max_tokens is set because our gateway requires it (a request without a bound has no worst-case cost to hold
    against the budget: step 4). ChatOpenAI sends none by default; the `no_max_tokens` check shows what happens then.
    """
    kwargs = {"max_tokens": 100} | kwargs
    return ChatOpenAI(base_url=f"{base_url}/v1", api_key=key, model=model, max_retries=0, timeout=30, **kwargs)


def summarizer(llm: ChatOpenAI):
    return PROMPT | llm | StrOutputParser()


# --- the features: each takes a factory for the model (so a check can add options) and returns what it observed ---

Model = Callable[..., ChatOpenAI]


async def chain(llm: Model) -> str:
    text = await summarizer(llm()).ainvoke({"mission": MISSIONS[0]})
    return f"{len(text.split())} words"


async def streaming(llm: Model) -> str:
    pieces = [piece async for piece in summarizer(llm()).astream({"mission": MISSIONS[0]})]
    return f"{len([p for p in pieces if p])} pieces"


async def batch(llm: Model) -> str:
    answers = await summarizer(llm()).abatch([{"mission": m} for m in MISSIONS], config={"max_concurrency": 5})
    return f"{sum(1 for a in answers if a)} of {len(MISSIONS)} answered"


async def token_counts(llm: Model) -> str:
    usage = (await llm().ainvoke(MISSIONS[0])).usage_metadata
    return f"in {usage['input_tokens']}, out {usage['output_tokens']}" if usage else "none"


async def streamed_token_counts(llm: Model) -> str:
    """ChatOpenAI asks for usage on a stream only when told to, or when it's talking to OpenAI itself."""

    async def usage(**kwargs):
        total = None
        async for chunk in llm(**kwargs).astream(MISSIONS[0]):
            total = chunk if total is None else total + chunk
        return total.usage_metadata if total is not None else None

    default, asked = await usage(), await usage(stream_usage=True)
    shown = "default: " + (f"out {default['output_tokens']}" if default else "none")
    return shown + "; stream_usage=True: " + (f"out {asked['output_tokens']}" if asked else "none")


async def response_headers(llm: Model) -> str:
    message = await llm(include_response_headers=True).ainvoke(MISSIONS[0])
    names = sorted(h for h in message.response_metadata.get("headers", {}) if h.startswith("x-"))
    gateway = [h for h in names if h.startswith(("x-gateway", "x-litellm"))]
    return f"{len(gateway)} gateway headers, e.g. {', '.join(gateway[:3])}" if gateway else "no gateway headers"


async def sampling(llm: Model) -> str:
    text = await summarizer(llm(temperature=0.2)).ainvoke({"mission": MISSIONS[0]})
    return f"temperature 0.2 accepted, {len(text.split())} words"


async def structured_output(llm: Model) -> str:
    """with_structured_output's default: OpenAI's response_format with a JSON schema."""
    result = await llm().with_structured_output(MissionSummary).ainvoke(MISSIONS[0])
    return f"a {type(result).__name__} with {', '.join(result.model_dump())}"


async def tool_calling(llm: Model) -> str:
    message = await llm().bind_tools([lookup_organization]).ainvoke("What is the EIN of River Friends?")
    return f"asked for {', '.join(c['name'] for c in message.tool_calls)}" if message.tool_calls else "no tool call"


async def fallback(llm: Model) -> str:
    """A route whose first model is overloaded: the app shouldn't notice."""
    text = await summarizer(llm(model="fallback")).ainvoke({"mission": MISSIONS[0]})
    return f"{len(text.split())} words"


async def no_max_tokens(llm: Model) -> str:
    """ChatOpenAI as most examples write it: no max_tokens."""
    text = await summarizer(llm(max_tokens=None)).ainvoke({"mission": MISSIONS[0]})
    return f"{len(text.split())} words"


async def wrong_key(llm: Model) -> str:
    await llm(api_key="sk-wrong-key").ainvoke(MISSIONS[0])
    return "answered"  # not expected


async def unknown_model(llm: Model) -> str:
    await llm(model="no-such-route").ainvoke(MISSIONS[0])
    return "answered"  # not expected


FEATURES = [
    ("chain", "A chain: prompt, model, output parser", chain),
    ("streaming", "The chain, streamed", streaming),
    ("batch", f"{len(MISSIONS)} missions in one batch, 5 at a time", batch),
    ("token_counts", "Token counts on an answer", token_counts),
    ("streamed_token_counts", "Token counts on a streamed answer", streamed_token_counts),
    ("response_headers", "The gateway's own response headers", response_headers),
    ("sampling", "A temperature", sampling),
    ("structured_output", "with_structured_output (a JSON schema)", structured_output),
    ("tool_calling", "bind_tools (a tool call)", tool_calling),
    ("fallback", "A route whose first model is overloaded", fallback),
    ("no_max_tokens", "The chain with no max_tokens (ChatOpenAI's default)", no_max_tokens),
    ("wrong_key", "A wrong key", wrong_key),
    ("unknown_model", "A model name the gateway doesn't have", unknown_model),
]


async def observe(feature, llm: Model) -> dict:
    """What happened, either way: an exception is an outcome to record, not a crash."""
    try:
        return {"ok": True, "observed": await feature(llm)}
    except Exception as e:  # noqa: BLE001 - every outcome is recorded
        status = getattr(e, "status_code", None)
        detail = getattr(e, "message", None) or str(e)
        return {"ok": False, "observed": f"{type(e).__name__}" + (f" {status}" if status else "")
                + f": {detail[:140]}"}  # fmt: skip


async def run_features(targets: dict[str, Model]) -> list[dict]:
    rows = []
    for name, what, feature in FEATURES:
        row = {"name": name, "what": what}
        for target, llm in targets.items():
            row[target] = await observe(feature, llm)
            print(f"{name:<22} {target:<8} {'ok ' if row[target]['ok'] else 'ERR'} {row[target]['observed'][:90]}")
        rows.append(row)
    return rows


def report(result: dict) -> str:
    def cell(outcome: dict) -> str:
        return ("" if outcome["ok"] else "**error** ") + outcome["observed"].replace("|", "\\|").replace("\n", " ")

    lines = [
        "# LangChain: one app, two gateways",
        "",
        f"Generated by `src/langchain_app.py` on {result['date'][:10]} with langchain-openai {result['versions']}. "
        "The same code against each gateway, with only the base URL and key changed; both call the same fake "
        "upstream. See the README, step 10, for what each difference means.",
        "",
        "| Feature | The gateway | LiteLLM |",
        "|---|---|---|",
    ]
    lines += [f"| {r['what']} | {cell(r['gateway'])} | {cell(r['litellm'])} |" for r in result["features"]]
    return "\n".join(lines) + "\n"


async def main_async() -> dict:
    from importlib.metadata import version

    from compare_suite import GATEWAY_URL, LITELLM_URL, GatewayAdmin, LiteLLMAdmin, Target

    sides = [Target("gateway", GATEWAY_URL, GatewayAdmin()), Target("litellm", LITELLM_URL, LiteLLMAdmin())]
    for side in sides:
        side.key = await side.issue("langchain")
    try:
        targets = {
            side.name: (lambda side=side, **kw: chat_model(side.url, kw.pop("api_key", side.key), **kw))
            for side in sides
        }
        features = await run_features(targets)
    finally:
        for side in sides:
            await side.revoke(side.key)
    return {
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "versions": f"{version('langchain-openai')}, langchain-core {version('langchain-core')}",
        "features": features,
    }


def main() -> None:
    if sys.platform == "win32":  # psycopg's async mode needs the selector loop on Windows (step 4)
        result = asyncio.run(main_async(), loop_factory=asyncio.SelectorEventLoop)
    else:
        result = asyncio.run(main_async())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "langchain.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    (OUT_DIR / "langchain.md").write_text(report(result), encoding="utf-8", newline="\n")
    print("wrote results/compare/langchain.md")


if __name__ == "__main__":
    main()
