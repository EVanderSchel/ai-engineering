# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `grant-review-workflow-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- Design doc (`docs/design.md`): the fictional fund's rules, the workflow's steps and state, principles, metrics, and the gold set plan
- The fund's eligibility rules as code (`src/fund_rules.py`), tested at every boundary
- The 60 real Form 990 answer keys, copied from Project 2 (`data/filings/`)
- Bedrock client (`src/bedrock.py`): Converse API calls to Claude Sonnet 4.6 and Haiku 4.5 through `us.` inference profiles, explicit `maxTokens`, adaptive retries, structured output through a forced tool with Pydantic validation, errors for cut-off or malformed output, and a cost and latency record per call; tested against Bedrock's API definition with botocore's Stubber
- State models (`src/state.py`): the application state from the design, one owner per field, validated values, exact JSON round trip for checkpoints; checked against the fund's rules and all 60 answer keys
- Intake (`src/intake.py`): turns a packet into a state or rejects it with every problem listed, before any model call
- extract (`src/extract.py`, prompt `prompts/extract/v1.txt`): page 1 of the return at 1568 px to the 12 Part I figures the workflow uses, with the revenue arithmetic checked; on dev, Sonnet 4.6 read 165/165 fields right, Haiku 4.5 164/165 (its one misread caught by the arithmetic check)
- research (`src/research.py`, prompts `prompts/research/`): ProPublica lookup in code, a Haiku name judgment only when names differ after normalizing, and a tool-use agent only for truly different organizations; ProPublica client and tools copied from Project 3 (`src/propublica.py`, `src/propublica_tools.py`), names in tool results marked as unverified text
- `bedrock.tool_loop`: a Converse tool-use loop with a forced finish tool, error results returned to the model, and turn and cost limits
- Return PDFs (`src/returns.py`): lists missing returns and, with `--download`, fetches them politely from the IRS; pymupdf pinned below 2.0, which adds ML layout packages rendering doesn't need
- Gold set of 40 application packets (`data/gold/`): forms, specs and computed labels; narratives for all 40, checked by code for their stated figures and approved in three batches; a test keeps the set complete and approved
