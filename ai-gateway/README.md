# ai-gateway

Project 6 of the AI Engineer roadmap: an **AI gateway with self-hosted, optimized inference, run on Kubernetes**. Every app sends its model requests to one service. The gateway decides which model answers, enforces each team's keys, rate limits and budget, screens the input and output, records what every call cost, and keeps answering when a provider fails. Behind it sit two hosted providers (Claude through the Anthropic API and through Amazon Bedrock) and two small models served by the project itself, tuned and measured for speed and cost.

Plain language: the gateway is a building's main electrical panel. Every outlet (app) draws power through it; it meters each circuit (team), and a breaker trips before one circuit overloads the building. If the utility (a provider) goes down, it switches to the backup generator (another provider) on its own.

## Why this project

The first five projects built AI applications. Job postings for AI engineers also ask for the platform underneath them, and the portfolio has nothing there yet. In 2026 analyses of AI engineer postings, **Kubernetes** appears at 60% of 198 hiring companies and **inference optimization** at 57%; observability (78%), distributed-systems patterns (62%) and security (62%) are also common. Saved postings ask for the same things in plain words: "AI Gateway services, MCP integrations, self-hosted inference", and debugging "provider outages, prompt regressions, quality drift and cost spikes".

## Plan

Each step is merged on its own; numbers are measured, not assumed.

| Step | What | Concept |
|---|---|---|
| 0 | Scaffolding: uv project, ruff, pytest, CI with change detection, dependency audit; local tools (kind, Helm, k6) *(done)* | |
| 1 | Design doc ([docs/design.md](docs/design.md)): API shape, routing and failure rules, data model, what gets measured, the memory budget for this PC *(done)* | What a gateway is responsible for, and what it isn't |
| 2 | A minimal gateway: one endpoint, one provider (Anthropic API), streaming, API keys, a stub provider for tests *(done)* | The request path, streaming through a proxy |
| 3 | Providers and resilience: Bedrock and self-hosted models behind one interface; routing rules; timeouts, retries with backoff, a circuit breaker, fallback; fault-injection tests *(done)* | Failure handling as code, measured under injected outages |
| 4 | Governance in PostgreSQL: teams, hashed keys, rate limits (token bucket), monthly budgets, a usage ledger with the cost of every call, an audit log; migrations *(done)* | Multi-tenant limits that hold under concurrency |
| 5 | Guardrails: PII redaction, prompt-injection screening, output validation, per-route policy (reusing what Projects 3 and 5 measured) | Reusable safety checks at the platform layer |
| 6 | Self-hosted inference and optimization: a small instruct model on llama.cpp (quantization levels compared) and the NTEE classifier from Project 4 on ONNX Runtime (int8 vs fp32); accuracy, latency and throughput under concurrency; vLLM on a Colab GPU for comparison | Inference optimization: quantization, batching, CPU vs GPU |
| 7 | Observability: OpenTelemetry traces, Prometheus metrics, a Grafana dashboard, alerts on error rate, latency and cost spikes | Seeing a platform's health and cost |
| 8 | Kubernetes: a local cluster (kind), a Helm chart, resource requests and limits, health probes, secrets, autoscaling (HPA) shown with a k6 load test, a rolling update with no dropped requests | Running services on Kubernetes |
| 9 | Quality: a versioned prompt registry, an eval canary that gates prompt and model changes on the earlier projects' gold sets, a drift monitor that scores sampled live traffic | Catching prompt regressions and drift |
| 10 | Build vs. adopt: the same tests and load against LiteLLM's proxy; a LangChain app that uses the gateway unchanged | When to build a platform piece and when to adopt one |
| 11 | Optional: the chart on Amazon EKS for a short, measured window (infrastructure as code), then torn down | Managed Kubernetes and its cost |
| Final | Write-up, cost report, release `ai-gateway-v1.0.0` | |

## Step 1: design

[docs/design.md](docs/design.md) sets out what the gateway is responsible for and the 15 decisions the later steps build on. The ones that shape everything else:

- **`model` names a route** (`smart`, `fast`, `local`, `classify`), not a provider model. Each route lists candidates tried in order, so a client's code doesn't change when the model behind a route does, and the response always says which model actually answered (on this AWS account, `smart` falls back from Sonnet 5.5 on the Anthropic API to Sonnet 4.6 on Bedrock: a different model, so it's never hidden).
- **No fallback after the first streamed byte.** Before it, a slow or failing provider is skipped; after it, switching would splice two answers together, so the stream ends with an error instead.
- **Budgets are reserved before a call and settled after**, like a hotel holding an amount on a card at check-in, so fifty requests arriving at once can't overspend a team's budget together.
- **Shared state lives in PostgreSQL, rate limits included**, so gateway replicas can scale without multiplying anyone's limit.
- **It has to fit this PC**: about 3.1 GB estimated for the whole cluster (kind node, two gateway replicas, PostgreSQL, a 0.5B model on llama-server, the NTEE classifier on ONNX Runtime, monitoring) against Docker's 3.8 GB, so only the smallest model variants run in the cluster.

## Step 2: a minimal gateway

One endpoint, `POST /v1/chat/completions`, in the format OpenAI clients speak, with Claude behind it. Plain language: a receptionist who takes requests in one standard form, checks the caller's badge, passes the request to the right specialist in that specialist's own language, and hands the answer back in the standard form, word by word if the caller wants it streamed.

The request path:

```
request -> request id -> API key (hash lookup) -> validate (unknown fields refused by name) -> route
        -> max_tokens required and under the route's ceiling -> temperature/top_p allowed on this route?
        -> translate to the provider's request -> provider (Claude API, or the stub) -> OpenAI-shaped answer
```

- **Routes** ([config/routes.yaml](config/routes.yaml)): `smart` is Claude Sonnet 5.5, `fast` is Claude Haiku 4.5, and `stub` echoes the message back offline for free. `smart` turns on the Claude API's server-side refusal fallback: if Sonnet 5.5 declines, the API re-runs the request on another model in the same call, and the response's `model` names whichever answered.
- **Streaming** waits for the provider's first event before answering. An error before any output is still a proper HTTP status (503, 504, ...); after the first byte the status is fixed at 200, so a later failure ends the stream with an error event instead of `[DONE]` (decision 10). Step 3 adds the fallback that this waiting makes possible.
- **API keys** are random `gw_...` strings; only their SHA-256 hash is configured (`GATEWAY_KEYS="team:hash,..."`), so a leaked config holds no working key. `uv run python src/keys.py new <team>` makes one.
- **The API moved under the project.** The Anthropic SDK 2.x sends API version 2026-10-21, which rejects `temperature` and `top_p` on every Claude model. Routes with a Claude candidate now refuse those fields with a 400 that says why, rather than dropping them silently (decision 16, added to the design doc).
- **No retries yet**: the SDK's own retries are off, because step 3 puts retries and fallback in the gateway, and two layers of retries multiply waits and costs.
- **Tested with the official OpenAI Python client** ([tests/test_openai_client.py](tests/test_openai_client.py)): completions, streams with usage, `/v1/models`, the gateway's own options via `extra_body`, and gateway errors arriving as the client's own exception classes, all with only the base URL changed. The client's HTTP goes straight into the app in memory, so tests never touch a network.

New files:

| File | What it does |
|---|---|
| `src/app.py` | The web service: `/v1/chat/completions` (streamed or not), `/v1/models`, `/healthz`, `/readyz`, request ids, OpenAI-style errors |
| `src/schemas.py` | The request format it accepts (strictly checked) and the response, chunk and error shapes it sends |
| `src/providers.py` | The shapes every provider adapter uses (request, answer, stream events, error kinds), so the rest is written once |
| `src/translate.py` | OpenAI messages to that common request (system prompt pulled out, same-role messages merged); Claude's stop reasons to OpenAI's finish reasons |
| `src/anthropic_provider.py` | The Claude API adapter: calls the official SDK, turns on refusal fallback per route, maps SDK errors to the gateway's error kinds |
| `src/stub_provider.py` | A free, offline provider that echoes the message; can be told to fail, for tests and load tests |
| `src/config.py`, `config/routes.yaml` | The routes and their limits, validated at start |
| `src/keys.py` | Makes API keys and checks them by hash |
| `tests/` | 71 tests: the API, translation, the Claude adapter against a fake SDK client, config and keys, and the OpenAI client |

**Checked live** (2026-10-27): four short calls through a local gateway with the official OpenAI client, `smart` and `fast`, plain and streamed, all `finish_reason: stop`, about $0.002 in total. First streamed text arrived after 0.6 s (Sonnet 5.5) and 0.4 s (Haiku 4.5); whole answers took 0.7-1.6 s. One finding for step 4: the API reports Haiku as `claude-haiku-4-5-20251001`, the dated snapshot, not the `claude-haiku-4-5` alias the route names, so prices must be looked up by the model that answered, aliases included.

Run it locally (the stub route needs no Anthropic key; `smart` and `fast` use `ANTHROPIC_API_KEY` from the environment):

```powershell
uv run python src/keys.py new local          # prints a key once, and the GATEWAY_KEYS line
$env:GATEWAY_KEYS = "local:<hash from above>"
uv run uvicorn app:create_app --factory --app-dir src --port 8000
```

## Step 3: providers and resilience

Built in stages: (1) retries, fallback and circuit breakers in the gateway, staged with a fault-injecting stub; (2) measuring them under injected outages; (3) Amazon Bedrock as a second real provider; (4) an adapter for OpenAI-compatible servers such as llama-server.

### Stage 1: retries, fallback, circuit breakers

Plain language: each route has a list of models in order of preference. If the first is having a moment (rate limited, overloaded), the gateway waits a fraction of a second and asks once more; if it's still failing, slow, or misconfigured, it moves down the list. A model that keeps failing gets its "breaker" tripped, like a household circuit breaker: for 30 seconds it's skipped outright instead of making every request wait on it, then one trial request decides whether it's back.

| Provider says | Gateway does | Why |
|---|---|---|
| rate limited, overloaded, 5xx, connection failed | retry once after a short random wait (honoring `Retry-After`, capped at 2 s), then next candidate | Often gone a moment later |
| too slow (`first_token` or `attempt` timeout) | next candidate, no retry | A slow provider is rarely fast a second later |
| misconfigured (bad key, unknown model) | next candidate, no retry | Only that candidate is broken |
| the request is invalid or too large | stop, return the provider's reason | It would fail on every candidate, and each try costs money |

- **Streams switch only before their first byte** (decision 10): the gateway waits for the first event from a candidate, within `first_token`, before answering; after that, a failure ends the stream with an error event.
- **The answer is never anonymous**: the body's `model` names the model that answered, and headers add `x-gateway-provider`, `x-gateway-attempts` and `x-gateway-fallback`.
- **When nothing answers**, the status says why: 400/413 for a refused request (with the provider's reason), 504 if every try timed out or the deadline passed, 503 `all_candidates_failed` listing what each candidate did, or 503 `all_candidates_paused` (with `Retry-After: 30`) when every breaker is open, without calling anyone.
- **Breakers are per provider model and per gateway replica**: 5 failures within 30 s open one; a bad request never counts against a provider.

New files:

| File | What it does |
|---|---|
| `src/router.py` | Tries a route's candidates in order: retry policy, per-candidate timeouts, circuit breakers, and the attempt record |
| `tests/test_router.py` | 24 tests: each failure kind, deadlines, backoff, stream fallback before the first byte only, breaker states, and the same through the HTTP API |
| `src/measure_resilience.py` | Stage 2's measurement: scenarios of injected outages, three ways each, and a recovery timeline; writes `results/resilience.{json,md}` |
| `tests/test_measure_resilience.py` | Its summary, percentile, timeline and report logic |
| `results/resilience.{json,md}` | The measurement's output |

### Stage 2: measured under injected outages

[`src/measure_resilience.py`](src/measure_resilience.py) runs the real gateway in memory against two fault-injecting stubs, 200 requests per scenario, 10 at a time, each scenario three ways: **gateway** (retries, fallback, breakers), **one candidate** (retries and breaker, nothing to fall back to) and **direct** (one try, no breaker: an app calling the provider itself). Times are scaled down (timeouts of fractions of a second, a 1 s breaker cooldown) so it runs in about 25 s for free. Full table: [results/resilience.md](results/resilience.md).

| Scenario (200 requests) | Gateway | One candidate | Direct |
|---|---|---|---|
| Healthy | 100%, p50 46 ms | 100%, 46 ms | 100%, 46 ms |
| Primary down | **100%**, p50 47 ms (all by the backup) | 0% | 0% |
| Primary fails 30% at random | **100%**, p95 110 ms | 78% | 64.5% |
| Primary 2 s slow to start | **100%**, p50 46 ms | 0% | 0% (all timed out) |
| Both down | 0%: 190 of 200 refused at once (breakers open) | 0% | 0% |
| Primary down, streamed | **100%** | 0% | 0% |
| Primary drops 30% of streams after 3 words | 64.5% | 64.5% | 64.5% |

What it shows:

- **A hard outage costs almost nothing once the breaker trips.** Only the first few requests pay for the retry and the fallback (slowest 136 ms against 46 ms healthy); after 5 failures the primary is skipped and the backup answers at normal speed. The same holds for a slow primary: without the breaker every request would wait out the timeout first.
- **When nothing can answer, the gateway fails fast**: with both candidates down, 190 of 200 requests got an immediate 503 with `Retry-After` instead of waiting on dead providers.
- **No request ran past its deadline** in any scenario.
- **A mid-stream failure can't be rescued** (decision 10): when the primary drops 30% of streams after a few words, the gateway does no better than calling directly. The fix belongs to the client (retry the whole request) or to fewer streamed routes for critical calls.
- **A breaker on a route with one candidate has a price.** With a flaky primary and nothing to fall back to, one retry lifted success from 64.5% to 78%, but the breaker then failed 15 requests fast that a retry could often have saved. Fail-fast is right when there's a backup or the provider is truly down; for single-candidate routes a higher threshold is worth considering.
- **Recovery waits for the cooldown.** In the timeline below, the primary came back at 3.0 s but traffic returned at 3.7 s, after the next half-open trial. With the real 30 s cooldown, the backup keeps serving for up to 30 s after a provider recovers: the price of not hammering a provider that's still struggling.

| Time | Breaker | Answered by | What happened |
|---|---|---|---|
| 0-1 s | closed | primary | healthy |
| 1.1-1.5 s | closed | backup, after 2-3 attempts | primary down: retry, then fallback (~80-130 ms) |
| 1.6-2.5 s | open | backup | primary skipped, normal speed |
| 2.6 s | half-open | backup | the trial on the primary failed: open again |
| 2.8-3.6 s | open | backup | primary skipped (it recovered at 3.0 s) |
| 3.7 s on | half-open, then closed | primary | the trial succeeded |

### Stage 3: Amazon Bedrock as a second provider

Plain language: Bedrock is a second, independent road to Claude, run by AWS on its own machines with its own quotas and outages. If the Claude API has a bad hour, the gateway can take the other road.

- **Through Bedrock's Converse API** ([src/bedrock_provider.py](src/bedrock_provider.py)), not the Anthropic SDK's Bedrock client: the SDK 2.x speaks API version 2026-10-21, which Bedrock accepts only for Claude 4.7 and later, and this AWS project has Sonnet 4.6 and Haiku 4.5 there (Claude 5.x quotas are 0).
- **boto3 is synchronous**, so each call runs in a worker thread; a stream's events are read in the thread and handed to the gateway's event loop as they arrive, so streaming stays word by word.
- **boto3's own retries are off**, like the Claude API client's: the router decides retries and fallback once, for every provider.
- **Errors map to the same kinds**: throttling, overload and 5xx are `unavailable` (retry once, then fall back); a model timeout is `timeout`; access denied, unknown model or missing/expired AWS credentials are `misconfigured` (fall back, details kept out of the response); a validation error is `rejected` (stop).
- **Routes now have a second road**: `smart` falls back from Sonnet 5.5 on the Claude API to Sonnet 4.6 on Bedrock (a different model, named in the response); `fast` tries Haiku 4.5 on Bedrock first, then on the Claude API.
- **Claude API-only options are checked**: `effort` and `refusal_fallback` on a non-Anthropic candidate stop the gateway at start instead of being ignored.
- Region and credentials come from the environment (`AWS_REGION`, default `us-east-2`; `AWS_PROFILE` locally after `aws login`, an IAM role when deployed). No AWS resources are created.

**Checked live** (2026-10-27, about $0.002): four calls through a local gateway with the official OpenAI client, the Claude API key deliberately broken.

| Route | Answered by | Attempts | Fallback | Time |
|---|---|---|---|---|
| `fast`, plain | Bedrock Haiku 4.5 | 1 | no | 1.3 s |
| `fast`, streamed | Bedrock Haiku 4.5 | 1 | no | first text 1.2 s, done 1.5 s |
| `smart`, plain | Bedrock Sonnet 4.6 | 2 | yes | 3.2 s |
| `smart`, streamed | Bedrock Sonnet 4.6 | 2 | yes | first text 0.9 s, done 1.4 s |

The broken key was a 401 from the Claude API, which the gateway treats as `misconfigured`: no retry, straight to the next candidate, and the caller never saw the 401. Running locally with `aws login` credentials needs `botocore[crt]` (in the dev group; a deployed gateway uses an IAM role instead).

New files: `src/bedrock_provider.py` (the Bedrock adapter: Converse and ConverseStream in a worker thread, error mapping) and `tests/test_bedrock_provider.py` (21 tests against a fake bedrock-runtime client: the request it sends, cache points, stop reasons, streams with errors partway, and every error kind).

### Stage 4: an adapter for OpenAI-compatible servers

[`src/openai_compatible_provider.py`](src/openai_compatible_provider.py) talks to any server that speaks OpenAI's chat completions API: llama-server, which will run the self-hosted model in step 6, or vLLM. The request mostly passes straight through (system prompt back as a system message, `temperature` and `top_p` allowed: these models accept them); streams ask the server for usage, and the server's statuses map to the same error kinds as every other provider (llama-server's 503 while its model is loading counts as `unavailable`, so the router falls back). It's registered as the `local` provider (address from `GATEWAY_LOCAL_URL`, default `http://localhost:8080/v1`), but no route uses it until step 6 brings a model to serve.

New files: `src/openai_compatible_provider.py` and `tests/test_openai_compatible_provider.py` (17 tests against a mock HTTP transport: the request it sends, usage with cached tokens, streams with an error partway, every status and transport failure).

Changed: `src/stub_provider.py` is now a fault injector (fail the first N calls or a seeded share of calls, before output or partway through a stream, with a slow first token or a slow pace); `config/routes.yaml` gains per-candidate `first_token` and `attempt` timeouts.

## Step 4: teams, keys, limits and budgets in PostgreSQL

Built in stages: (1) the database and its migrations; (2) API keys moved from the environment into the database, with admin commands; (3) rate limits, the budget hold and the usage ledger, tested under concurrent requests; (4) readiness checks and the write-up.

**In short**: the gateway now knows who is calling and what they may spend. Teams and keys are managed with admin commands and audited; each key has rate limits that hold across replicas; each team has a monthly budget that 50 simultaneous requests couldn't overspend; and every admitted request leaves a ledger row with the model that answered and what it cost. PostgreSQL 17 holds it all, its schema changed only by numbered migrations, and the gateway refuses to start against a database that isn't migrated to exactly its code.

### Stage 1: the database and its migrations

- **PostgreSQL 17 locally in Docker** ([docker-compose.yml](docker-compose.yml)): the official image pinned by digest, on `127.0.0.1:5433` only (5432 belongs to a PostgreSQL already installed on this PC), with a 256 MB memory cap; the password lives in the gitignored `.env` ([.env.example](.env.example) shows the shape). It uses about 32 MB idle.
- **psycopg 3 and plain SQL**, chosen over an ORM so every query, and in stage 3 the atomic budget update, can be read as written.
- **Migrations are numbered SQL files** ([migrations/](migrations/)) applied by [src/migrate.py](src/migrate.py). Plain language: the database's shape changes only through files in the repo, each applied once, in order, and recorded, so a laptop's, CI's and the cluster's databases end up identical. Each file runs in its own transaction (a failure leaves nothing half done); an advisory lock stops two replicas migrating at once; a file edited after it was applied, or a database that's ahead of the code, stops the runner.
- **Migration 001** creates `teams` (name rules enforced by the database, optional allowed routes) and `api_keys` (only the SHA-256 hash and a short prefix, never the key, plus a revocation time).
- **Database tests** get a fresh, empty database each (created and dropped around the test). Locally they use the Docker database and are skipped if it isn't running; in CI a PostgreSQL service container runs beside the test job, and `REQUIRE_DATABASE` turns a missing database into a failure there.

Two Windows lessons, recorded because they cost time and fail silently:

- **psycopg's async mode hangs on Windows' default event loop** (Proactor) instead of raising an error. Async tests run on the selector loop on Windows; the gateway will need the same when run locally (stage 2). Linux, including the cluster, is unaffected.
- **`localhost` can hang connections** on Windows: the client tries IPv6 `::1` first, where nothing listens, and Windows retries the refused connection for a long time. The URLs use `127.0.0.1`.

New files:

| File | What it does |
|---|---|
| `docker-compose.yml` | The local PostgreSQL container |
| `.env.example` | The shape of the gitignored `.env` (no real secrets) |
| `migrations/001_teams_and_keys.sql` | The first tables: teams and their API keys |
| `src/migrate.py` | Applies the migrations not yet applied, in order, under a lock, each in a transaction, checking checksums |
| `tests/test_migrate.py` | 8 tests: file naming, line endings, apply-once, rollback of a failing file, refusing edited files and newer databases, the schema's rules |

Changed: `tests/conftest.py` (a fresh database per test, the selector event loop on Windows); `.github/workflows/ai-gateway-ci.yml` (a PostgreSQL service container for the test job).

```powershell
docker compose up -d --wait db     # start the local database
uv run python src/migrate.py       # bring its schema up to date
```

### Stage 2: API keys in the database

Plain language: keys used to live in an environment variable, so adding or cancelling one meant restarting the gateway. Now an administrator issues and revokes them with commands, and the gateway checks each request against the database, so a revoked key stops working on its very next request.

```powershell
uv run python src/admin.py team add research --routes smart,fast   # omit --routes for every route
uv run python src/admin.py key issue research                      # the key is printed once, never stored
uv run python src/admin.py key list research                       # prefixes, created, revoked
uv run python src/admin.py key revoke gw_AbC1234                   # by prefix
uv run python src/serve.py                                         # the gateway, on the database from .env
```

- **One interface, two key stores** ([src/keys.py](src/keys.py)): PostgreSQL when `DATABASE_URL` is set, otherwise the `GATEWAY_KEYS` variable (tests, stub-only setups: every route allowed, no revocation).
- **Teams can be limited to routes**: a request for another route gets 403 `route_not_allowed`, and `/v1/models` lists only the team's routes.
- **Every admin change is audited** in the same transaction as the change (migration 002, `audit_log`): who, what, when, and the key's prefix, never the key.
- **A database outage isn't an authentication failure**: if the key check can't reach PostgreSQL the answer is 503 `auth_unavailable` with `Retry-After`, not a 401 that would tell a client its valid key is wrong.
- **The connection pool** ([src/db.py](src/db.py)) opens when the app starts and lends one of up to 10 connections to each key check; a request waits at most 5 s for one.
- **Running it locally** goes through [src/serve.py](src/serve.py), which starts uvicorn on the event loop psycopg needs on Windows.
- **Checked end to end** on the real server: a key issued by the CLI worked on its route, got 403 on another, and got 401 on the next request after `key revoke`; the audit log recorded all three changes.

No cache in front of the key check yet: one indexed lookup per request. Step 8's load test will show whether it needs one.

New files:

| File | What it does |
|---|---|
| `migrations/002_audit_log.sql` | The audit log: who changed what, when |
| `src/admin.py` | The admin commands: add and list teams; issue, list and revoke keys |
| `src/db.py` | The connection pool to PostgreSQL |
| `src/serve.py` | Runs the gateway locally (uvicorn on the selector event loop on Windows) |
| `tests/test_keys_postgres.py` | 8 tests: the admin commands and their audit rows, the key store, revocation and route limits through the API, and a database outage |

Changed: `src/keys.py` (the key stores and `Team`), `src/app.py` (the pool's startup and shutdown, async key checks, 403 for routes, 503 when the key check can't run).

### Stage 3: rate limits, the budget hold and the usage ledger

Every admitted request now goes through four steps ([src/accounting.py](src/accounting.py)):

| Step | What happens | Refused with |
|---|---|---|
| 1. Admit | take one request and the estimated tokens from the key's two buckets (requests and tokens per minute, refilling continuously) | 429 `rate_limited` with `Retry-After`; 400 if one request is bigger than a whole minute's tokens |
| 2. Reserve | hold the most the request could cost (estimated input plus all of `max_tokens`, at the dearest candidate's price) against the team's month | 429 `budget_exceeded` |
| 3. Call | the router tries the candidates (step 3) | |
| 4. Settle | swap the hold for the real cost, give back unused tokens, write one usage row | |

Plain language: the hold is a hotel holding an amount on your card at check-in and charging the real bill at check-out. The token buckets are a tap that refills at a steady rate: each request draws from it, and an empty bucket means waiting.

- **Each check is one SQL statement that tests and changes a row at once** (`... where spent + reserved + hold <= budget returning`, an upsert with a `where` for the buckets), so two replicas, or fifty requests at once, can't both see room that only one of them has.
- **Prices come from a table, not the code** (migration 003): one row per model id a provider reports, dated, so the Claude API's `claude-haiku-4-5-20251001` (found in step 2) has its own row and a price change never reprices old usage. The gateway refuses to start if any route's model has no price, because it couldn't hold a budget for it.
- **What a request is charged**: its real cost when the provider reports usage; nothing when no candidate answered (errors before output aren't billed); its whole hold when a stream breaks partway or the client goes away, because the usage is then unknown. Never under, sometimes over.
- **Admin**: `team budget <name> <usd>` (audited, `none` removes the cap) and `team spend <name>` (this month's spent, held, budget).

**Measured: 50 requests at once against a $0.50 budget** (the stub priced so each request holds $0.051 and costs $0.005): 13 served, 37 refused with `budget_exceeded`, $0.065 spent, nothing left held, and the 37 refused never reached the provider. Five repeats, same invariants. The trade-off shows too: holding the worst case refuses requests the budget could have afforded (it spent 13% of the budget at the peak), so callers that send a realistic `max_tokens` get more through. Never overspending is the point; underspending at a burst is the price.

**Checked on the real server**: with a key limited to 2 requests a minute, the third request got 429 `rate_limited` with `Retry-After: 30`, the two served requests appeared in the ledger with their tokens, and `team spend` reported the month.

Not done here, and why:

- **A hold can outlive a crash.** If a gateway replica dies mid-request, its hold stays in `reserved_usd` until someone clears it. A sweeper for old holds (or holds stored as rows with a time) is the fix; for now `team spend` shows them.
- **Refused requests get no ledger row**; step 7 counts them as metrics instead.

New files:

| File | What it does |
|---|---|
| `migrations/003_limits_budgets_usage.sql` | Per-key limits, team budgets, the token buckets, the dated price list (seeded with today's prices), the usage ledger |
| `src/accounting.py` | Costs, the rate-limit buckets, the budget hold and settlement, the ledger row; `NoAccounting` when there's no database |
| `tests/test_accounting.py` | 13 tests: the cost arithmetic, price lookup, holds and settlement, refusals, both buckets refilling, startup without a price, audit of budget changes, and 50 concurrent requests |

Changed: `src/app.py` (admission before the router, settlement after it, including in streams and when a client disconnects), `src/keys.py` (the key's limits and the team's budget come with the key check), `src/admin.py` (`team budget`, `team spend`).

### Stage 4: readiness and a schema check at startup

- **`/readyz` asks the database** (`select 1`, at most 2 s): 503 `{"database": "unreachable"}` when it can't answer. `/healthz` doesn't, on purpose. Plain language: "alive" and "ready" are different questions. Kubernetes restarts a pod that isn't alive, but only stops sending traffic to one that isn't ready; restarting every gateway replica because the database blinked would make an outage worse.
- **The gateway refuses to start against the wrong schema**: if the database is missing one of the code's migrations (run `src/migrate.py`) or has one the code doesn't know (older code against a newer database), startup fails with the file names, instead of failing later on some request.
- **Checked on the real server**: with the database container stopped, `/readyz` went to 503 within its 2 s limit while `/healthz` stayed 200; when the database came back, the connection pool reconnected by itself and `/readyz` returned to 200.

New file: `tests/test_readiness.py` (6 tests: ready without and with a database, not ready when it stops answering, a quick give-up on an unreachable server, the schema check naming missing and unknown migrations, and refusing to start). Changed: `src/db.py` (`ping`, `check_schema`), `src/app.py` (`/readyz`, the startup check); `docs/design.md` section 5 now describes the tables as built.

## Setup

```powershell
cd ai-gateway
uv sync            # creates .venv with the locked dependencies
uv run pytest      # tests (no network, no providers, no cluster)
uv run ruff check . ; uv run ruff format --check .
bash security/audit.sh   # known vulnerabilities in locked dependencies (Git Bash)
```

Local tools for the later steps: Docker Desktop, kubectl, [kind](https://kind.sigs.k8s.io/) (local Kubernetes clusters), [Helm](https://helm.sh/) (packaging the services for Kubernetes) and [k6](https://k6.io/) (load tests).

## Constraints

- **This PC:** 8 GB RAM (Docker gets about 3.8 GB), a 4-core CPU, no NVIDIA GPU, about 18 GB free disk. The local cluster, PostgreSQL, the monitoring stack and the self-hosted models have to fit in that, so the models are small (about 0.5B parameters, quantized) and GPU serving is measured in Colab.
- **Cost:** load tests and most tests use a stub provider and cost nothing. Paid model calls are small and estimated before each run.
- **Data:** no real personal data. PII tests use made-up records.
