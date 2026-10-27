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
| 3 | Providers and resilience: Bedrock and self-hosted models behind one interface; routing rules; timeouts, retries with backoff, a circuit breaker, fallback; fault-injection tests | Failure handling as code, measured under injected outages |
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
