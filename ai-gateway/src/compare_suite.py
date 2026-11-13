"""The parity suite (step 10 stage 2): the same black-box checks against the gateway and LiteLLM's proxy, both on
the shared fake upstream, recorded side by side. Free: nothing leaves this PC.

    uv run python src/compare_db.py                                   # once: both gateways' databases
    docker compose --profile compare up -d --build --wait
    uv run python src/compare_suite.py                                # writes results/compare/parity.{json,md}

Plain language: two waiters get the same list of awkward orders (a dish the kitchen can't make, a customer with no
table, a customer whose card is over its limit) and we write down what each one does. Where they differ, the table
says which behaviour this project's design asked for, and why.

What's checked:
- the request path: plain and streamed answers, what each says about who answered, fallback past an overloaded
  model (and how many upstream calls that took, from the fake upstream's /stats), a request the provider refuses,
  a provider that never answers, missing and wrong keys, an unknown model, a malformed body, an absurd max_tokens;
- governance: a revoked key (and how long until it stops working), a key limited to one model, a per-key rate limit,
  and a budget, one request at a time and under 30 at once.

Each gateway's keys are made fresh for every run: the gateway's in its own database (src/admin.py functions), LiteLLM's
through its admin API (/key/generate, /key/delete) with the master key from .env.
"""

import asyncio
import datetime as dt
import json
import pathlib
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx2
import psycopg

import admin
from compare_db import url_for
from paths import RESULTS_DIR, ROOT

GATEWAY_URL, LITELLM_URL, UPSTREAM_URL = "http://127.0.0.1:8101", "http://127.0.0.1:8102", "http://127.0.0.1:8103"
OUT_DIR = RESULTS_DIR / "compare"
MESSAGES = [{"role": "user", "content": "hello there"}]
BUDGET_USD = Decimal("0.02")  # about 9 requests at $100 per million tokens (2 in, 20 out: $0.0022 each). The
# gateway keeps budgets to the cent, so a smaller one rounds to $0.00 (a finding of its own: see the README).
SPEND_WRITE_WAIT = 20  # seconds: LiteLLM writes spend to its database in batches
CONCURRENT = 30
RPM = 5


def env_value(name: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} isn't in .env")


# --- issuing keys on each side ---


class GatewayAdmin:
    """Teams and keys in the comparison gateway's database. One team per key, so each case's budget is its own."""

    name = "gateway"

    def __init__(self):
        self.conn = psycopg.connect(url_for("compare_gateway"), autocommit=True)
        self.run = uuid.uuid4().hex[:6]
        self.teams: dict[str, str] = {}  # key -> team

    def issue(self, label: str, models: list[str] | None = None, budget: Decimal | None = None,
              rpm: int | None = None) -> str:  # fmt: skip
        team = f"parity-{self.run}-{label}"
        admin.add_team(self.conn, team, models)
        if budget is not None:
            admin.set_budget(self.conn, team, budget)
        key = admin.issue_key(self.conn, team)
        if rpm is not None:
            self.conn.execute("update api_keys set requests_per_minute = %s where prefix = %s", (rpm, key[:10]))
        self.teams[key] = team
        return key

    def revoke(self, key: str) -> None:
        admin.revoke_key(self.conn, key[:10])

    async def spent(self, key: str) -> float:
        _, spent, _ = admin.spending(self.conn, self.teams[key])
        return float(spent)


class LiteLLMAdmin:
    """Virtual keys through LiteLLM's admin API, with the master key."""

    name = "litellm"

    def __init__(self):
        self.client = httpx2.AsyncClient(
            base_url=LITELLM_URL, headers={"authorization": f"Bearer {env_value('LITELLM_MASTER_KEY')}"}, timeout=30
        )

    async def issue(self, label: str, models: list[str] | None = None, budget: Decimal | None = None,
                    rpm: int | None = None) -> str:  # fmt: skip
        body: dict = {"key_alias": f"parity-{uuid.uuid4().hex[:6]}-{label}"}
        if models:
            body["models"] = models
        if budget is not None:
            body["max_budget"] = float(budget)
        if rpm is not None:
            body["rpm_limit"] = rpm
        response = await self.client.post("/key/generate", json=body)
        response.raise_for_status()
        return response.json()["key"]

    async def revoke(self, key: str) -> None:
        (await self.client.post("/key/delete", json={"keys": [key]})).raise_for_status()

    async def spent(self, key: str) -> float:
        response = await self.client.get("/key/info", params={"key": key})
        return float(response.json()["info"].get("spend") or 0)


@dataclass
class Target:
    name: str
    url: str
    admin: object
    key: str = ""

    async def issue(self, label, **kwargs) -> str:
        result = self.admin.issue(label, **kwargs)
        return await result if asyncio.iscoroutine(result) else result

    async def revoke(self, key) -> None:
        result = self.admin.revoke(key)
        if asyncio.iscoroutine(result):
            await result


# --- asking ---


async def upstream_calls(client: httpx2.AsyncClient) -> dict:
    return (await client.get(f"{UPSTREAM_URL}/stats")).json()


def delta(before: dict, after: dict) -> dict:
    return {m: after[m] - before.get(m, 0) for m in after if after[m] != before.get(m, 0)}


async def chat(client: httpx2.AsyncClient, target: Target, model: str = "chat", key: str | None = None,
               stream: bool = False, body: dict | None = None, timeout: float = 60) -> dict:  # fmt: skip
    """One request; what came back, in the same shape for either gateway."""
    headers = {} if key == "" else {"authorization": f"Bearer {key or target.key}"}
    payload = body if body is not None else {"model": model, "messages": MESSAGES, "max_tokens": 50, "stream": stream}
    if stream:
        payload["stream_options"] = {"include_usage": True}
    started = time.perf_counter()
    response = await client.post(f"{target.url}/v1/chat/completions", json=payload, headers=headers, timeout=timeout)
    out = {"status": response.status_code, "ms": round((time.perf_counter() - started) * 1000)}
    if response.status_code != 200:
        try:
            error = response.json().get("error") or {}
            out["error"] = str(error.get("code") or error.get("type") or "")[:40]
        except ValueError:
            out["error"] = response.text[:40]
        return out
    out["headers"] = sorted(h for h in response.headers if h.startswith(("x-gateway-", "x-litellm-")))
    if stream:
        text, usage, model_seen = "", None, None
        for line in response.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[6:])
                model_seen = chunk.get("model") or model_seen
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    text += (choice.get("delta") or {}).get("content") or ""
        out |= {"text": text, "model": model_seen, "usage": bool(usage)}
    else:
        data = response.json()
        out |= {"text": data["choices"][0]["message"]["content"], "model": data.get("model"),
                "usage": bool(data.get("usage"))}  # fmt: skip
    return out


def answered_in_full(result: dict) -> bool:
    return result.get("status") == 200 and result.get("text", "").split() == [f"word{i}" for i in range(1, 21)]


# --- the cases: each returns what it saw on one gateway ---


async def plain(c, t):
    r = await chat(c, t)
    return {"status": r["status"], "complete": answered_in_full(r), "model": r.get("model"), "usage": r.get("usage")}


async def streamed(c, t):
    r = await chat(c, t, stream=True)
    return {"status": r["status"], "complete": answered_in_full(r), "usage": r.get("usage")}


async def says_who_answered(c, t):
    r = await chat(c, t, model="fallback")
    return {"model": r.get("model"), "headers": r.get("headers")}


async def fallback(c, t, stream=False):
    before = await upstream_calls(c)
    r = await chat(c, t, model="fallback", stream=stream)
    calls = delta(before, await upstream_calls(c))
    return {"status": r["status"], "complete": answered_in_full(r), "upstream_calls": calls, "ms": r["ms"]}


async def fallback_streamed(c, t):
    return await fallback(c, t, stream=True)


async def refused(c, t):
    before = await upstream_calls(c)
    r = await chat(c, t, model="refused")
    return {
        "status": r["status"],
        "answered": answered_in_full(r),
        "upstream_calls": delta(before, await upstream_calls(c)),
    }


async def hung_provider(c, t):
    before = await upstream_calls(c)
    r = await chat(c, t, model="slow", timeout=120)
    return {
        "status": r["status"],
        "seconds": round(r["ms"] / 1000, 1),
        "upstream_calls": delta(before, await upstream_calls(c)),
    }


async def no_key(c, t):
    return {"status": (await chat(c, t, key=""))["status"]}


async def wrong_key(c, t):
    plain_wrong = await chat(c, t, key="wrong-key")
    shaped = await chat(c, t, key="sk-" + uuid.uuid4().hex if t.name == "litellm" else "gw_" + uuid.uuid4().hex)
    return {"status": plain_wrong["status"], "status_well_formed_key": shaped["status"]}


async def unknown_model(c, t):
    r = await chat(c, t, model="no-such-model")
    return {"status": r["status"], "error": r.get("error")}


async def malformed_body(c, t):
    r = await chat(c, t, body={"model": "chat"})  # no messages
    return {"status": r["status"]}


async def absurd_max_tokens(c, t):
    r = await chat(c, t, body={"model": "chat", "messages": MESSAGES, "max_tokens": 10_000_000})
    return {"status": r["status"], "error": r.get("error")}


async def revoked_key(c, t):
    key = await t.issue("revoke")
    before = (await chat(c, t, key=key))["status"]
    await t.revoke(key)
    started, statuses = time.perf_counter(), []
    while time.perf_counter() - started < 90:  # how long a revoked key keeps working
        status = (await chat(c, t, key=key))["status"]
        statuses.append(status)
        if status != 200:
            break
        await asyncio.sleep(1)
    return {"before": before, "after": statuses[-1], "worked_for_s": round(time.perf_counter() - started, 1),
            "requests_after_revoking": len(statuses) - 1}  # fmt: skip


async def model_scope(c, t):
    key = await t.issue("scope", models=["chat"])
    allowed = (await chat(c, t, key=key))["status"]
    other = await chat(c, t, key=key, model="fallback")
    return {"allowed_model": allowed, "other_model": other["status"], "error": other.get("error")}


async def rate_limit(c, t):
    key = await t.issue("rpm", rpm=RPM)
    statuses = [(await chat(c, t, key=key))["status"] for _ in range(RPM * 2)]
    return {"limit_rpm": RPM, "sent": len(statuses), "served": statuses.count(200), "refused": statuses.count(429),
            "other": len([s for s in statuses if s not in (200, 429)])}  # fmt: skip


async def budget_one_at_a_time(c, t):
    key = await t.issue("budget-seq", budget=BUDGET_USD)
    served = refused_at = 0
    refusal = None
    for i in range(40):
        r = await chat(c, t, key=key)
        if r["status"] != 200:
            refused_at, refusal = i + 1, f"{r['status']} {r.get('error')}"
            break
        served += 1
        await asyncio.sleep(0.2)
    await asyncio.sleep(SPEND_WRITE_WAIT)
    return {"budget_usd": float(BUDGET_USD), "served": served, "refused_on_request": refused_at, "refusal": refusal,
            "spent_usd": round(await t.admin.spent(key), 6)}  # fmt: skip


async def budget_all_at_once(c, t):
    key = await t.issue("budget-burst", budget=BUDGET_USD)
    results = await asyncio.gather(*(chat(c, t, key=key) for _ in range(CONCURRENT)))
    statuses = [r["status"] for r in results]
    await asyncio.sleep(SPEND_WRITE_WAIT)
    spent = await t.admin.spent(key)
    refusals = sorted({f"{r['status']} {r.get('error')}" for r in results if r["status"] != 200})
    return {"budget_usd": float(BUDGET_USD), "sent_at_once": CONCURRENT, "served": statuses.count(200),
            "refused": len(statuses) - statuses.count(200), "refusals": refusals, "spent_usd": round(spent, 6),
            "over_budget_usd": round(max(0.0, spent - float(BUDGET_USD)), 6)}  # fmt: skip


CASES = [
    ("plain", "A plain answer", plain),
    ("streamed", "A streamed answer, with usage", streamed),
    ("says_who_answered", "What the response says about who answered (after a fallback)", says_who_answered),
    ("fallback", "Fallback past an overloaded model (503)", fallback),
    ("fallback_streamed", "The same, streamed", fallback_streamed),
    ("refused", "The provider refuses the request (400)", refused),
    ("hung_provider", "The provider never answers", hung_provider),
    ("no_key", "No key", no_key),
    ("wrong_key", "A wrong key", wrong_key),
    ("unknown_model", "An unknown model name", unknown_model),
    ("malformed_body", "A request with no messages", malformed_body),
    ("absurd_max_tokens", "max_tokens of 10 million", absurd_max_tokens),
    ("revoked_key", "A key revoked while in use", revoked_key),
    ("model_scope", "A key allowed only `chat`, asking for another model", model_scope),
    ("rate_limit", f"A key limited to {RPM} requests a minute, sent {RPM * 2}", rate_limit),
    ("budget_one_at_a_time", f"A ${BUDGET_USD} budget, requests one at a time", budget_one_at_a_time),
    ("budget_all_at_once", f"A ${BUDGET_USD} budget, {CONCURRENT} requests at once", budget_all_at_once),
]


async def run() -> dict:
    targets = [Target("gateway", GATEWAY_URL, GatewayAdmin()), Target("litellm", LITELLM_URL, LiteLLMAdmin())]
    results: dict = {}
    async with httpx2.AsyncClient(timeout=120) as client:
        for t in targets:
            t.key = await t.issue("main")
        for name, _, case in CASES:
            results[name] = {}
            for t in targets:
                results[name][t.name] = await case(client, t)
                print(f"{name:<22} {t.name:<8} {json.dumps(results[name][t.name])[:110]}")
    return {
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "litellm_image": "ghcr.io/berriai/litellm-database:v1.106.4",
        "cases": [{"name": n, "what": w, **results[n]} for n, w, _ in CASES],
    }


def report(result: dict) -> str:
    def show(observed: dict) -> str:
        return "; ".join(f"{k}: {v}" for k, v in observed.items())

    lines = [
        "# Parity: the gateway and LiteLLM's proxy, same checks",
        "",
        f"Generated by `src/compare_suite.py` on {result['date'][:10]} against {result['litellm_image']}, both "
        "calling the same fake upstream (src/fake_upstream.py). See the README, step 10, for what each difference "
        "means.",
        "",
        "| Check | The gateway | LiteLLM |",
        "|---|---|---|",
    ]
    lines += [f"| {c['what']} | {show(c['gateway'])} | {show(c['litellm'])} |" for c in result["cases"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    if sys.platform == "win32":
        result = asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)
    else:
        result = asyncio.run(run())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "parity.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    (OUT_DIR / "parity.md").write_text(report(result), encoding="utf-8", newline="\n")
    print(f"wrote {pathlib.Path('results/compare/parity.md')}")


if __name__ == "__main__":
    main()
