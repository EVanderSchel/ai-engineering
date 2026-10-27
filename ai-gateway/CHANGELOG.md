# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `ai-gateway-vX.Y.Z`.

## [Unreleased]

### Added

- Step 0: project scaffolding (uv, ruff, pytest), CI with change detection (lint, tests, dependency audit), weekly Dependabot updates.
- Step 1: design doc (`docs/design.md`): responsibilities, architecture, the OpenAI-compatible API, routing and resilience rules, budgets and rate limits in PostgreSQL, guardrails, self-hosted inference, observability, the memory budget for the local cluster, and 15 numbered decisions.
