# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `ai-gateway-vX.Y.Z`.

## [Unreleased]

### Added

- Step 0: project scaffolding (uv, ruff, pytest), CI with change detection (lint, tests, dependency audit), weekly Dependabot updates.
- Step 1: design doc (`docs/design.md`): responsibilities, architecture, the OpenAI-compatible API, routing and resilience rules, budgets and rate limits in PostgreSQL, guardrails, self-hosted inference, observability, the memory budget for the local cluster, and 15 numbered decisions.
- Step 2: the minimal gateway. `POST /v1/chat/completions` (OpenAI-compatible, streamed or not), `GET /v1/models`, health checks; API keys checked by SHA-256 hash; routes from `config/routes.yaml` (`smart`: Claude Sonnet 5.5 with server-side refusal fallback, `fast`: Claude Haiku 4.5, `stub`: offline echo); the Claude API adapter on the official SDK (2.x, API version 2026-10-21) and a stub provider; errors in OpenAI's shape with honest statuses; tests including the official OpenAI Python client. Design decision 16: `temperature` and `top_p` only on routes whose every candidate accepts them.
