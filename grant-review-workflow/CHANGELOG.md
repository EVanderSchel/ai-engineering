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
- Evals (`src/evaluate.py`, `tests/test_eval_gate.py`): dev 3x15 and test 3x25 (run once). Test: no wrong automatic declines; route, rules, extraction, stated figures 100%; 14/14 injections intact; flags exact 89.6% (false name mismatches and a true source disagreement), focus 97.0%; $0.034 per application; 8 of 75 runs lost to Bedrock outages, every packet run at least twice. Findings and a proposed (unverified) fix in docs/design.md
- Pausing for the reviewer and durable checkpoints: `human_review` with `interrupt()`, `start`/`resume`/`outcome`, corrections looping back to eligibility, checkpoints in DynamoDB (`src/checkpoint_table.py`, 30-day TTL) through `langgraph-checkpoint-aws`, a checkpoint class allowlist, crash recovery without rerunning finished steps, stage timings from the checkpoint history, `sdk_retries` in call records, and a refusal to start an application ID twice
- The workflow as a LangGraph graph (`src/workflow.py`): extract, research and read_proposal in parallel, joined at cross_check; eligibility, route, and a letter for declines by the rules; reducers on the state's shared lists; ungrounded proposal readings become an `unverified_reading` flag; the README's diagram is drawn from the compiled graph and tested. First end-to-end dev run: route, rules and flags 15/15, no wrong automatic declines, $0.030 per application
- draft_letter (`src/draft_letter.py`, prompt `prompts/draft_letter/v2.txt`): the decision letter from the decision and its reasons only, checked by code (decision, reasons quoted word for word, amounts), revised once with the problems listed, and any remaining problems kept in the state (`letter_problems`); on dev, Sonnet 4.6 and Haiku 4.5 both 15/15 correct letters; Haiku chosen
- cross_check, eligibility and route (`src/checks.py`): flags with evidence, the public record compared for the same period only, rules applied with reviewer corrections, and no automatic decline when the figures are in doubt; research now keeps the record's figures for every period
- read_proposal (`src/read_proposal.py`, prompt `prompts/read_proposal/v2.txt`): the untrusted narrative to focus area, stated figures, rubric with quotes, and quoted injection attempts, with a code check that every quote and figure is in the text; on dev, Sonnet 4.6 and Haiku 4.5 both 15/15 focus and figures, 3/3 injections, no false alarms; Sonnet chosen for its rubric spread
- `bedrock.structured` asks once more when an answer doesn't fit its schema; the call record counts both attempts
- research (`src/research.py`, prompts `prompts/research/`): ProPublica lookup in code, a Haiku name judgment only when names differ after normalizing, and a tool-use agent only for truly different organizations; ProPublica client and tools copied from Project 3 (`src/propublica.py`, `src/propublica_tools.py`), names in tool results marked as unverified text
- `bedrock.tool_loop`: a Converse tool-use loop with a forced finish tool, error results returned to the model, and turn and cost limits
- Return PDFs (`src/returns.py`): lists missing returns and, with `--download`, fetches them politely from the IRS; pymupdf pinned below 2.0, which adds ML layout packages rendering doesn't need
- Gold set of 40 application packets (`data/gold/`): forms, specs and computed labels; narratives for all 40, checked by code for their stated figures and approved in three batches; a test keeps the set complete and approved
