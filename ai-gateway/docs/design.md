# Design: ai-gateway

Step 1 (amended in step 2: decision 16; in step 3: timeouts per candidate; in step 4: the tables as built; in step 5: decisions 17-18). What the gateway does, what it deliberately doesn't, and how each step will be judged. Numbered decisions are referred to from code and later steps; when one changes, it's amended here with the reason, not silently rewritten.

## 1. The problem

Plain language: when several teams each call AI models directly, each one stores its own provider keys, picks its own models, handles outages its own way (or not at all), and nobody can say what the company spent last week or stop one team's runaway script before the bill arrives. A gateway puts one front door in front of every model, so those jobs are done once, the same way, for everyone.

What it is responsible for:

| Job | Question it answers |
|---|---|
| Authentication | Who is calling? (a team's API key) |
| Routing | Which model answers this request, and which ones stand in if it fails? |
| Resilience | What happens when a provider is slow, throttling or down? |
| Limits and budgets | Is this team over its rate limit or its monthly spend? |
| Guardrails | Does the request or the answer carry personal data or an injection attempt? |
| Accounting | What did this call cost, and whose budget pays for it? |
| Observability | How fast, how reliable and how expensive is each route right now? |
| Quality | Did a prompt or model change make answers worse? |

What it is not: an application. It holds no business logic, no conversation history and no documents; apps send complete requests and get complete answers. (Decision 1.)

## 2. Architecture

```
 apps / SDKs / LangChain                                   (OpenAI-compatible clients, base URL changed)
          |
          v
 +-------------------- gateway (FastAPI, 2+ replicas) --------------------+
 | auth -> rate limit -> budget reserve -> input guardrails -> router ---+ |
 |                                                                      | |
 |   provider adapters:  anthropic | bedrock | llama-server | stub      | |
 |                                                                      | |
 | <- output guardrails <- usage + cost <- (stream or full response) <--+ |
 +------|---------------------------|--------------------------|----------+
        | keys, limits, budgets,    | traces, metrics          | model calls
        v usage ledger, audit log   v                          v
   PostgreSQL               OpenTelemetry collector     Anthropic API, Amazon Bedrock,
                            -> Prometheus -> Grafana     llama-server (Qwen2.5-0.5B-Instruct, GGUF),
                                                         classifier (NTEE, ONNX Runtime)
```

Everything except the hosted providers runs in one local Kubernetes cluster (kind) from step 8; before that, the same containers run under Docker Compose or straight from `uv run`.

- **Stateless gateway replicas** (decision 2). All shared state (keys, limits, budgets, ledger) lives in PostgreSQL, so any replica can serve any request and Kubernetes can add or remove replicas freely. Rate-limit counters are the exception worth naming: they're updated in PostgreSQL too, not in each replica's memory, because per-replica counters would let a team get N times its limit with N replicas.
- **Self-hosted models are separate services**, not code inside the gateway (decision 3). They need far more memory than the gateway, scale differently, and llama-server already speaks the OpenAI format, so the gateway treats it like any other provider.

## 3. The API (decision 4: OpenAI-compatible)

Chosen in step 0 because nearly every SDK, LangChain and LiteLLM can use it by changing one base URL.

| Endpoint | What |
|---|---|
| `POST /v1/chat/completions` | Text chat, streaming (server-sent events, ending `data: [DONE]`) or not |
| `GET /v1/models` | The routes this key may use |
| `POST /v1/classifications` | NTEE classification (not part of the OpenAI API; documented as the gateway's own) |
| `GET /healthz`, `GET /readyz` | Liveness (the process runs) and readiness (a background check reached PostgreSQL in the last few seconds, decision 19); Kubernetes uses both |
| `GET /metrics` | Prometheus metrics (cluster-internal only) |

- **`model` names a route, not a provider model** (decision 5): `smart`, `fast`, `local`, `classify`. The route decides the actual model and its fallbacks (section 4). A client asking for `smart` doesn't change when the route moves to a newer model, which is what lets step 9's eval canary gate model changes.
- **Request fields supported in v1:** `messages` (system, user, assistant; text content), `max_tokens` or `max_completion_tokens` (required by the gateway, with a per-route ceiling: it bounds the cost reservation, section 5), `temperature` and `top_p` (only on some routes, decision 16), `stop`, `stream`, `stream_options.include_usage`, `response_format` (`json_schema` validated on output, section 6; added in step 5), `user`, and `n` only as 1. Unsupported fields (tools, images, `n` > 1, logprobs) are rejected with a 400 naming the field, never silently dropped (decision 6).
- **Sampling settings depend on the route** (decision 16, added in step 2). From API version 2026-10-21 (the Anthropic SDK's default from release 2.0.0), the Claude API rejects `temperature`, `top_p` and `top_k` on every model, and Claude models from 4.7 on accept only their defaults anyway. A route therefore accepts `temperature` and `top_p` only if every one of its candidates does (today, the self-hosted and stub ones); on a route with a Claude candidate they're a 400 that says so, rather than being dropped and silently changing what the caller asked for.
- **Claude-specific options** go in an `extra_body`-style `gateway` object: `{"gateway": {"cache_system_prompt": true}}`. Prompt caching is off unless asked, because a cache write costs 25% more than plain input and only pays off when the same prompt repeats.
- **Translation to Anthropic's format** (for the Anthropic API and Bedrock's Converse): system messages move to the `system` field, consecutive same-role messages are merged, stop reasons map to OpenAI's `finish_reason` (`end_turn` -> `stop`, `max_tokens` -> `length`, `refusal` -> `content_filter`), and usage maps to `prompt_tokens` / `completion_tokens`.
- **Errors use OpenAI's error shape** (`{"error": {"message", "type", "code"}}`) with honest status codes: 401 bad key, 403 route not allowed, 400 invalid request, 413 too large, 429 rate limit or budget (with `Retry-After`; `code` says which), 502 a provider answered wrongly, 503 every candidate failed, 504 timed out.
- **Every response carries `x-request-id`**, and the response's `model` field names the provider model that actually answered, so a fallback is never invisible to the caller.

## 4. Routing and resilience

Routes live in a versioned YAML file (`config/routes.yaml`), loaded at start and validated by a schema; a bad file stops the gateway from starting rather than routing wrongly (decision 7).

```yaml
smart:
  candidates:              # tried in order
    - {provider: anthropic, model: claude-sonnet-5-5}
    - {provider: bedrock,   model: us.anthropic.claude-sonnet-4-6}
  max_tokens_ceiling: 4096
  timeout: {first_token: 20, attempt: 90, total: 120}   # seconds
fast:
  candidates:
    - {provider: bedrock,   model: us.anthropic.claude-haiku-4-5-20251001-v1:0}
    - {provider: anthropic, model: claude-haiku-4-5}
    - {provider: local,     model: qwen2.5-0.5b-instruct}
local:
  candidates:
    - {provider: local,     model: qwen2.5-0.5b-instruct}
```

- **A fallback can be a different model** (decision 8). On this account Bedrock serves Claude 4.x only (Claude 5.x quotas are 0), so `smart` falls back from Sonnet 5.5 to Sonnet 4.6. That's a quality change, so it is recorded on the response (`model`), in the ledger and in metrics, and each route's candidates are checked against the eval set in step 9.
- **What is retried** (decision 9): connection errors, timeouts, 429 and 5xx from a provider. Never 400-class errors (the request itself is wrong and will fail everywhere) and never a guardrail block.
- **How**: one retry on the same candidate with jittered exponential backoff for 429/5xx (honoring `Retry-After` up to a cap), then the next candidate. A request has a total deadline; no retry starts if it can't finish before it. A timeout moves straight to the next candidate (a slow provider is unlikely to be fast a moment later), as does a misconfigured one.
- **Timeouts per candidate** (amended in step 3): `first_token` for a stream's first event and `attempt` for a whole non-streamed answer, both per candidate, so one slow candidate can't use up the whole `total`. (The step 1 draft had a `connect` timeout too; the provider clients' own connect timeouts cover it.)
- **Streaming can't be retried once it has started** (decision 10). After the first token reaches the client, a failure ends the stream with an error event; switching providers mid-answer would splice two different answers together. Fallback therefore happens only before the first byte, which is why `first_token` has its own timeout.
- **Circuit breaker per provider and model** (decision 11): after 5 failures in 30 seconds the breaker opens and that candidate is skipped (no waiting on a dead provider) for 30 seconds, then one trial request is let through (half-open); success closes it. Breaker state is per replica, which is acceptable: each replica learns about an outage within a few requests.
- **Measured in step 3** with a fault-injecting stub provider (errors, slow first tokens, dropped connections at chosen rates): requests served during an injected outage of the primary, added latency of a fallback, and that no request exceeds its deadline.

## 5. Teams, keys, limits and budgets (PostgreSQL)

As built in step 4 (migrations 001-003; the step 1 draft's separate `rate_limits` table became limit columns on `api_keys` plus a `rate_buckets` table, and guardrail policy moves to step 5):

| Table | Holds |
|---|---|
| `teams` | name, allowed routes, monthly budget (USD; null: no cap) |
| `api_keys` | team, SHA-256 hash of the key, a short visible prefix, created/revoked times, requests and tokens per minute (the key itself is shown once and never stored) |
| `rate_buckets` | per key: requests and tokens left, as of when they were last refilled |
| `budgets` | per team and month: spent and reserved (USD) |
| `prices` | per provider and reported model id: input, output, cache read/write per million tokens, with the date they took effect |
| `usage` | one row per admitted request: time, request id, team, key prefix, route, provider model that answered, attempts, tokens, held and charged cost, latency, outcome |
| `audit_log` | team creation, key issue and revocation, budget changes (guardrail blocks from step 5) |
| `schema_migrations` | which migration files have been applied, with their checksums |

- **Budgets are reserved before a call and settled after it** (decision 12). Before calling a provider, the gateway reserves the most the request could cost (input tokens estimated generously plus `max_tokens` of output, at the dearest candidate's price) in one atomic statement that fails if spent + reserved + this reservation exceeds the budget. After the call, the reservation is replaced by the actual cost. Plain language: like a hotel holding an amount on your card at check-in and charging the real bill at check-out. Without the hold, fifty requests arriving at once could each see room in the budget and together overspend it.
- **Rate limits are token buckets** per key (requests and tokens per minute), each checked and taken in one atomic statement, so they hold across replicas. Unused estimated tokens are given back when the request settles.
- **Costs use the prices table, not hard-coded numbers**, so a price change is a new row with a date and old ledger rows keep the price that applied. Providers can report a different model id from the one a route names (the Claude API reports dated snapshots), so prices are keyed on the reported id, falling back to the configured one; the gateway refuses to start if a route's model has no price. Self-hosted models get a cost too (their share of the node's hourly price at measured throughput, from step 6), so cheaper routing is visible in dollars.
- **What is charged** when usage is unknown: nothing if no candidate answered; the whole hold if a stream broke partway or the client left. Never under, sometimes over.
- **The schema is checked at startup**: the gateway refuses to run against a database that isn't migrated to exactly its migrations, and `/readyz` reports the database unreachable (503) while `/healthz` stays up.
- **Measured in step 4**: 50 concurrent requests against a $0.50 budget never overspent it; the ledger's sum equals the budget's spent column, and refused requests never reached a provider.
- **Known gap**: a hold whose replica crashes mid-request stays in `reserved_usd` until cleared.

## 6. Guardrails

Policy per team and route: `off`, `flag` (allow, record in the audit log and metrics) or `block` (refuse with a 400 explaining which check). (Decision 13.)

- **Input:** personal-data detection (US SSNs, card numbers that pass the Luhn check, email addresses, phone numbers) with `redact` as an extra option that replaces them with placeholders before the request leaves; a prompt-injection screen reusing the patterns and labeled attacks from Projects 3 and 5.
- **Output:** the same personal-data check, and `json_schema` validation when the request asked for it (an answer that doesn't match is a 502, not a silently wrong 200).
- **Streaming and output checks conflict** (decision 14): a streamed answer has been shown before it's complete. Routes with output policy `block` therefore buffer the answer and stream it only after it passes; routes with `flag` stream immediately and check at the end. The trade-off (time to first token) is measured in step 5.
- **Prompts and answers aren't logged by default** (decision 15). Traces and the ledger hold sizes, tokens and outcomes, not content; a team can turn on content logging for debugging, and the audit log records who did.
- Measured with labeled cases: detection rate on planted PII and injections, false alarms on clean text, added latency per check.

As built in step 5: the actions are `off`, `flag`, `redact` (personal data) and `block`, set per route in `routes.yaml` and per team in `teams.guardrails` (decision 17); the injection screen is patterns plus an opt-in model judge (decision 18); a blocked request gets a 400 (or a 503 if the judge is down and the policy says so), a `blocked` ledger row and an audit row with the check, action and kinds or rules, never text. Not built: `json_schema` validation of answers and the per-team content-logging switch; both are left for a later step.

## 7. Self-hosted inference (step 6)

Two models the project serves itself, chosen to fit this PC (no NVIDIA GPU, Docker limited to about 3.8 GB):

- **Qwen2.5-0.5B-Instruct on llama-server** (llama.cpp's server: OpenAI-compatible, continuous batching, parallel slots, `/health` and Prometheus `/metrics`; CPU image `ghcr.io/ggml-org/llama.cpp:server`). Quantizations compared: Q4_K_M (491 MB), Q8_0 (676 MB) and F16 (1.27 GB) from Qwen's official GGUF repository (downloads asked for first).
- **The NTEE classifier from Project 4** (LoRA Qwen2.5-0.5B with a classification head), exported to ONNX in Colab and served with ONNX Runtime on CPU, fp32 vs. int8. Accuracy is measured on Project 4's dev split only: its test split has been used.
- **Measured:** time to first token, tokens per second, latency p50/p95 and throughput at 1, 4, 8 and 16 concurrent requests; memory; accuracy or answer quality per quantization; cost per 1,000 requests at the measured throughput. The same instruct model on vLLM on a Colab T4 gives the GPU comparison point.
- **As measured** (step 6 stage 2, `results/quantization.md`): on this CPU Q8_0 beats Q4_K_M on speed under load and on accuracy (ARC-Easy 65.2% vs 63.0%, the same as F16), for 148 MB more memory; throughput stops growing past the server's 4 slots. The plain model can't do the NTEE task (below the majority class at every precision), so ARC-Easy (400 test questions) is the accuracy measure, alongside agreement with F16.
- **The GPU point** (step 6 stage 4, the same file): the same fp16 weights on vLLM on a Colab T4 generate 181 tokens/s for one request (CPU Q8_0: 31) and 3,700 tokens/s in total at 64 at once (CPU: 61 at best), with the first token in 26 ms alone and 294 ms at 64 at once, at about the same hourly price. ARC-Easy accuracy is the same within noise. The gateway keeps the CPU server, because this project's cluster has no GPU, but a GPU serving engine is the answer once requests arrive concurrently. vLLM must be started with `--generation-config vllm`, or it applies the model's own default repetition penalty to requests that don't set one.
- What a 0.5B model is for, honestly: short, simple tasks (classification, extraction from short text, routing). It's the `local` route, not a substitute for Claude. (Planned as the last fallback of `fast` too; as built in stage 1, `local` stands alone, and that fallback waits until stage 2 has measured the model's answers: a silent fallback to a much weaker model is a quality decision, not just a resilience one.)

## 8. Observability

- **Traces** (OpenTelemetry): one span per request, with child spans for auth, limits, each guardrail, each provider attempt (provider, model, outcome, retry number) and the ledger write. Attributes carry team, route, tokens and cost; never content (decision 15). As built (step 7 stage 3, `src/tracing.py`): the request span is FastAPI's native one (HTTP conventions, `traceparent` continued), annotated by the gateway; provider calls are `chat <model>` spans with `gen_ai.*` attributes; exported over OTLP only when an endpoint is set (Jaeger locally, a collector in the cluster); the log line carries the trace id.
- **Metrics** (Prometheus): requests, errors and latency by route, provider and outcome; time to first token; tokens and cost by team; fallbacks and open breakers; budget used per team; guardrail flags and blocks. As built (step 7 stage 2, `src/metrics.py`): served on a separate port (9464) because they name teams and spend; counted from each request's log fields; label values only from the config, the database or fixed words (a made-up route is `unknown`); breakers read at scrape time, budgets from PostgreSQL every 30 s.
- **Alerts** (step 7): error rate, p95 latency, a breaker open for more than 5 minutes, and a cost spike (a team's spend in the last hour well above its usual hour). As built (step 7 stage 4, `observability/alerts.yml`): those four plus a first-token latency rule and a budget at 90%. They're unit-tested with promtool in CI, and shown in Prometheus and on the Grafana dashboard. There's no Alertmanager, so nothing is sent to a person yet. Seen firing and resolving against injected faults (step 7 stage 5, `results/alerts.md`). Instrumentation costs about 1.2 ms a request, 1 ms of it traces (`results/observability.md`); sample traces if that ever matters.
- **Logs**: one JSON line per request with the request id, matching the trace. As built (step 7 stage 1, `src/logs.py`): written on stdout when the response has been sent (a stream's after its last event), with the attempts, outcome, model, tokens, cost and guardrail kinds; other lines during the request carry its id; health checks aren't logged.

## 9. Kubernetes (step 8) and the memory budget

The cluster has to fit in Docker's 3.8 GB (WSL capped at 4 GB in `.wslconfig`: Docker sees 3.83 GiB). The estimate from step 1, against what step 8 measured (working set per container, MiB, at idle with 2 gateway pods, and with 4):

| Component | Estimated | Measured in step 8 |
|---|---|---|
| kind node: control plane and system pods | ~700 MB | 680-695 MiB in its pods, plus about 400 MiB outside any pod (kubelet, containerd) |
| gateway | ~300 MB for 2 | 263 MiB for 2, 540 MiB for 4 (about 135 each) |
| PostgreSQL | ~150 MB | 51-58 MiB |
| llama-server, Qwen2.5-0.5B Q8_0 | ~850 MB (836 MB peak in step 6) | 162 MiB idle, 466-527 MiB after load: the model is memory-mapped, so unused pages count as page cache (see below) |
| classifier, ONNX 8-bit weight-only | ~1.4 GB | left out (decided 2026-11-04): it didn't fit and isn't a service yet |
| OpenTelemetry collector | ~100 MB | not run: Jaeger v2 is built on the collector and receives OTLP itself, 77-80 MiB |
| Prometheus + Grafana | ~450 MB | 83-91 + 441-470 MiB |
| metrics-server (for the autoscaler) | not in the estimate | 48-54 MiB |
| **The whole node** (Docker's count) | ~3.95 GB with the classifier, ~2.55 GB without | **2.19 GiB with 2 gateway pods, 2.46 GiB with 4**, at idle; with the model resident under load, about 2.8 GiB with 4 |

- **It fits, with about 1 GiB to spare,** only because the classifier is out. With it, the estimate was over, and that held.
- **A memory-mapped model hides part of its size.** Unused pages count as page cache, which the kernel drops under pressure without charging the pod. With Docker at 2.84 GiB (step 8 stage 4), that is what happened: the model's pages were evicted and re-read from the Windows mount for every token, and a short prompt took over 60 s. Budget it at the size it needs resident (836 MB), not at the working set it reports.
- **The pieces came in under estimate, except Grafana and the node's own processes.** Grafana's cold start needed its limit raised to 640 MiB.
- The fp32 and F16 measurements in step 6 run outside the cluster, one at a time.
- Every pod sets memory requests and limits; a pod over its limit is killed and restarted rather than starving the node. Limits protect pods from each other, not the node from being over-full (step 8 stage 4). The autoscaler (HPA) scales the gateway on CPU between 2 and 4 replicas, at 50% of a 200m request; the model servers stay at one replica here (on a real cluster they would scale on queue depth).
- Secrets (Anthropic key, AWS credentials) come from the local environment into Kubernetes Secrets at deploy time; nothing secret is in the chart or the repo. AWS credentials in the cluster are short-lived ones exported from the `agent-toolkit` login.

## 10. Quality over time (step 9)

- **Prompt registry** (built in step 9 stage 1): named prompts as versioned, immutable files with fingerprints, the pattern from the rag project. Requests name one in `gateway.prompt` (`name` or `name@version`); its rendered text is the whole system prompt, and variable values are screened like user messages. The usage ledger, log line, trace and response headers record `name@version`.
- **Eval canary** (built in step 9 stages 2-3: `src/canary.py`, `config/canary.yaml`, CI job `ai-gateway-canary`, which compares with the baselines on main and runs real models only when a check's route or prompt changed): a change to a route's candidates or a prompt runs a fixed eval set (drawn from the earlier projects' dev data with known answers) through the gateway and is rejected if accuracy drops beyond a set margin; it runs in CI on changes to `config/routes.yaml` and the prompt files.
- **Drift monitor**: free checks on every answer (built in step 9 stage 4a: its format against the registered prompt's `answer_format`, and whether it was cut off), counted in `gateway_answers_total` and alerted on against the rates the canary measured (`observability/drift.yml`, generated by `src/drift.py`). Built in stage 4b: a small sample of answers on routes that opt in (`drift.judge_rate`) is graded 1-5 by Claude Haiku 5.5 in the background, and a mean grade well below the judge's grade of the canary's answers alerts. The judge has no answer key, so it catches broken formats and off-task answers, not plausible wrong ones (`src/drift_judge.py`).

## 11. Testing

- Tests never reach a provider, a database server or a cluster: the stub provider stands in for models, and PostgreSQL logic is tested against a disposable database in CI (a service container) and locally under Docker, with pure-logic tests (cost, token buckets, translation, breaker states) running everywhere.
- Load tests (k6) run against the stub provider, so they cost nothing and measure the gateway, not the provider.

## 12. Not in scope (v1)

Tool/function calling, images, embeddings, the Responses API, user sign-in (keys only; teams are created by an admin command), an admin UI, multi-region deployment, and semantic caching. Each could be a later step; leaving them out keeps the core measurable.

## 13. Decisions

Decisions 1-15 were made in step 1; later ones say which step added them.

| # | Decision | Why |
|---|---|---|
| 1 | The gateway holds no application state or business logic | It serves every app; logic belongs in the apps |
| 2 | Gateway replicas are stateless; shared state is in PostgreSQL, rate limits included | Replicas can scale freely without multiplying limits |
| 3 | Self-hosted models run as separate services | Different memory and scaling; llama-server is already OpenAI-compatible |
| 4 | OpenAI-compatible API | Every common client works by changing the base URL |
| 5 | `model` names a route, not a provider model | Models change behind a stable name, gated by evals |
| 6 | Unsupported request fields are rejected, not dropped | Silent drops change behavior without telling the caller |
| 7 | Routes are a validated, versioned YAML file | A bad config fails at start, not on live traffic |
| 8 | A fallback may be a different model, and the response says which answered | Quality changes must be visible |
| 9 | Retry only connection errors, timeouts, 429 and 5xx | A bad request fails everywhere; retrying it wastes money |
| 10 | No fallback after the first streamed byte | Two providers' answers can't be spliced together |
| 11 | Circuit breaker per provider model, per replica | Stop waiting on a dead provider; simple and good enough |
| 12 | Budgets are reserved before a call and settled after | Concurrent requests can't overspend together |
| 13 | Guardrail policy per team and route: off, flag or block | Teams differ in what they handle |
| 14 | Output-blocking routes buffer before streaming | A shown answer can't be taken back |
| 15 | No prompt or answer content in logs or traces by default | They may hold personal data |
| 16 | `temperature` and `top_p` only on routes whose every candidate accepts them | The Claude API removed them in version 2026-10-21; dropping them silently would change the request (added in step 2) |
| 17 | A team's guardrail settings can only tighten its routes' (each setting at the stricter of the two; only settings the team chose count) | The route is the platform's floor; a team handling untrusted text can raise it, no team can lower it (added in step 5) |
| 18 | Guardrails run after admission, personal data before injection; with the judge on, the judge decides and patterns are the fallback | Admission first, so nobody can make the gateway pay for unlimited judge calls; redaction first, so the judge never sees what was redacted; the judge decides because on held-out cases it caught everything the patterns did plus the data-shaped attacks, and cleared the patterns' false alarm (added in step 5) |
| 19 | Readiness answers from a background database check on its own connection, not a query through the request pool | Under load every pool connection was lent out, so busy pods failed readiness and left the Service just when their capacity was needed: 5.5% of requests failed during a scale-up. Being busy isn't being broken (added in step 8) |

## 14. Open questions

- **Bedrock from inside kind**: short-lived credentials exported into a Secret need refreshing (they expire within hours). Fine for demos; step 8 decides whether to automate the refresh or keep Bedrock calls to local runs.
- **The NTEE export**: exporting the classification head to ONNX in Colab needs the adapter from Google Drive; if export fails, the classifier runs on plain PyTorch on CPU in the cluster, at a larger memory cost.
- **Docker's memory**: if measurements in step 6 exceed the budget above, Docker's WSL2 limit can be raised (a setting the user changes), at the cost of the rest of the PC.
- **The classifier's memory** (step 6 stage 3): standard dynamic int8 broke the model (19% vs 58.7%; outlier activations). The classifier is now 8-bit weight-only (58.8%, 1.46 GB peak, about 1 s per mission on this CPU), which puts the budget above at about 3.95 of 3.8 GB. Step 8 decides how to find about 150 MB: a lighter monitoring stack, one gateway replica at rest, or the classifier outside the cluster. The 4-bit version (55.9%, 1.1 GB) is the fallback if nothing else gives.
