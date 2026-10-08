# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `nonprofit-agent-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- ProPublica Nonprofit Explorer API client (`src/propublica.py`): one request per second, retries, on-disk cache
- Agent tools as plain functions (`src/tools.py`): `search_organizations`, `get_organization`, `get_financials`, `compute_ratios`, tested on saved real API responses
