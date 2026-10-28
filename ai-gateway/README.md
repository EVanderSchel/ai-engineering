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
| 4 | Governance in PostgreSQL: teams, hashed keys, rate limits (token bucket), monthly budgets, a usage ledger with the cost of every call, an audit log; migrations | Multi-tenant limits that hold under concurrency |
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
