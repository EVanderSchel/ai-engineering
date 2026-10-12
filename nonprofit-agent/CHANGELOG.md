# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `nonprofit-agent-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- ProPublica Nonprofit Explorer API client (`src/propublica.py`): one request per second, retries, on-disk cache
- Agent tools as plain functions (`src/tools.py`): `search_organizations`, `get_organization`, `get_financials`, `compute_ratios`, tested on saved real API responses
- Hand-built agent loop (`src/agent.py`, Claude Sonnet 5): tool descriptions, tool errors returned to the model, step and cost limits, prompt caching, saved trajectories in `data/runs/`
- `compute_ratios` compares any two years (`compare_with_year`); missing years say whether they were filed as PDF only or not filed
- Trajectory eval: 22 user-approved questions (`evals/cases.jsonl`) with expected answers recomputed from a frozen API snapshot (`evals/snapshot/`); grading by code (figures, grounding) and a Claude Opus 5 judge for written criteria (`src/grade.py`); a runner with repeated runs, resume, separate infrastructure errors, served-model check and re-grading (`src/run_eval.py`); a judge check with answers that must pass or fail (`src/check_judge.py`). Baseline, v1 and v2 results in `evals/runs/`
- Grounding guardrail: before an answer is returned, any dollar amount or percentage that no tool returned sends the draft back once to retrieve or remove it; a second failure is returned with the figures flagged (`src/grounding.py`, shared with the eval's grader). Eval: grounded 92.4% to 100%, correct 98.5% to 100%
- The same agent on the SDK's Tool Runner (`src/runner_agent.py`), sharing the model, prompt, tool specs, limits, tool execution and grounding guardrail with the hand-built loop; `run_eval.py --agent runner` evaluates it. Eval: same behavior and cost within noise; the runner version needs more code (85 vs 76 lines) for the cost limit, error translation, final-answer check and trajectory

### Changed

- Tools report the period of the latest return filed (`latest_return_period_end`) and note when it's newer than the latest figures; a year without figures is described as "filed, but no extracted figures in this data source yet" (not "only as a PDF"), "not filed yet", or "no filing with data"
- System prompt: the data's 1-2 year lag; when no year is named, use and name the most recent year with figures; compare organizations on the same tax year

### Fixed

- A search with no matches (HTTP 404 from the API) was reported as the data source being unavailable; it now returns zero results with a hint
- The API client is safe to use from several threads at once (shared one-request-per-second spacing, cache files written atomically), and caches 404s as answers
