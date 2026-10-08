# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `nonprofit-agent-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- ProPublica Nonprofit Explorer API client (`src/propublica.py`): one request per second, retries, on-disk cache
- Agent tools as plain functions (`src/tools.py`): `search_organizations`, `get_organization`, `get_financials`, `compute_ratios`, tested on saved real API responses
- Hand-built agent loop (`src/agent.py`, Claude Sonnet 5): tool descriptions, tool errors returned to the model, step and cost limits, prompt caching, saved trajectories in `data/runs/`
- `compute_ratios` compares any two years (`compare_with_year`); missing years say whether they were filed as PDF only or not filed

### Fixed

- A search with no matches (HTTP 404 from the API) was reported as the data source being unavailable; it now returns zero results with a hint
