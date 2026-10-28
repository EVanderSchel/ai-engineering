# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `ai-gateway-vX.Y.Z`.

## [Unreleased]

### Added

- Step 0: project scaffolding (uv, ruff, pytest), CI with change detection (lint, tests, dependency audit), weekly Dependabot updates.
- Step 1: design doc (`docs/design.md`): responsibilities, architecture, the OpenAI-compatible API, routing and resilience rules, budgets and rate limits in PostgreSQL, guardrails, self-hosted inference, observability, the memory budget for the local cluster, and 15 numbered decisions.
- Step 2: the minimal gateway. `POST /v1/chat/completions` (OpenAI-compatible, streamed or not), `GET /v1/models`, health checks; API keys checked by SHA-256 hash; routes from `config/routes.yaml` (`smart`: Claude Sonnet 5.5 with server-side refusal fallback, `fast`: Claude Haiku 4.5, `stub`: offline echo); the Claude API adapter on the official SDK (2.x, API version 2026-10-21) and a stub provider; errors in OpenAI's shape with honest statuses; tests including the official OpenAI Python client. Design decision 16: `temperature` and `top_p` only on routes whose every candidate accepts them.
- Step 3, stage 1: retries, fallback and circuit breakers (`src/router.py`). Temporary errors retried once with jittered backoff, then the next candidate; timeouts per candidate (`first_token`, `attempt`); streams switch only before their first byte; a breaker per provider model; response headers `x-gateway-provider`, `x-gateway-attempts`, `x-gateway-fallback`; 503 `all_candidates_failed` / `all_candidates_paused` and 504 when nothing answers. The stub provider became a fault injector.
- Step 3, stage 2: `src/measure_resilience.py` and `results/resilience.{json,md}`: seven injected-outage scenarios, each with the gateway, one candidate and direct calls, plus a recovery timeline. With a hard outage or a slow primary the gateway served 100% (direct: 0%), with a 30% flaky primary 100% (direct: 64.5%); mid-stream drops can't be rescued; no request passed its deadline.
- Step 3, stage 3: Amazon Bedrock provider (`src/bedrock_provider.py`, Converse and ConverseStream through boto3 in a worker thread, no boto3 retries). `smart` falls back to Sonnet 4.6 on Bedrock; `fast` tries Haiku 4.5 on Bedrock first. Claude API-only route options are refused on other providers.
- Step 3, stage 4: an adapter for OpenAI-compatible servers (`src/openai_compatible_provider.py`), registered as the `local` provider for llama-server (step 6). `httpx2` is now a declared dependency.
