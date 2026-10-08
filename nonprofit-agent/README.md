# nonprofit-agent

Project 3 of the AI Engineer roadmap: a **tool-using agent** that answers research questions about US charities, such as "How did the American Red Cross's revenue change over the last three years?" or "Which of these two food banks spends a larger share on programs?". It works from public data only.

Plain language: a regular Claude call answers from memory. An agent is given **tools** (functions it can ask us to run, such as "look up this charity's filings") and works in a loop. It decides which tool to call, reads the result, decides what to do next, and stops when it can answer. It's like a research assistant with a phone and a calculator: it doesn't know the numbers, but it knows who to call and what to ask.

## Why this domain

- **Public, free, real data.** The [ProPublica Nonprofit Explorer API](https://projects.propublica.org/nonprofits/api) serves organization profiles and financials from IRS filings. No key needed. This builds on Project 2 (`irs-990-extraction`), whose deployed API can be one of the agent's tools.
- **Answers can be checked.** Most questions have a numeric answer that can be computed from the same data. That allows evals of both the final answer and the path the agent took to reach it (the *trajectory*: which tools, in what order, how many calls).
- **Social benefit.** Nonprofit transparency, the same reason Project 2 used Form 990s.

Rules: only ProPublica's JSON API and the IRS's own downloads are used. ProPublica's website and download links have bot protection, which this project does not work around. No private or customer data is used.

## Plan

| Step | What | Concept |
|---|---|---|
| 0 | Scaffolding: uv project, ruff, pytest, CI with change detection, dependency audit | Same foundation as Projects 1-2 |
| 1 | Tools as plain Python functions, with caching and tests: search organizations, get an organization's profile, get its financials by year, compute ratios | A tool is just a function plus a description the model reads |
| 2 | Agent loop built by hand on the Claude API: send tools, run the ones Claude asks for, return results, repeat, with step and cost limits | What every agent framework does under the hood |
| 3 | Trajectory evals: a task set with checked answers, scoring answer correctness, tool choice, and number of steps/cost | Evaluating behavior, not only output |
| 4 | Same agent with a framework (the SDK's Tool Runner), compared with the hand-built loop | What a framework saves and what it hides |
| 5 | MCP server exposing the tools, so any MCP client (Claude Code, Claude Desktop) can use them | Tools as a reusable, standard interface |
| 6 | Prompt-injection testing: hostile text inside tool results (e.g. an organization's mission statement) | Tool output is untrusted input |
| 7 | Guardrails and release (`nonprofit-agent-v1.0.0`) | Shipping an agent safely |

## Setup

```powershell
cd nonprofit-agent
uv sync            # creates .venv with the locked dependencies
uv run pytest      # tests
uv run ruff check . ; uv run ruff format --check .
bash security/audit.sh   # known vulnerabilities in locked dependencies (Git Bash)
```

## Layout

- `src/`: flat modules (`pytest.ini` puts `src` on the import path).
- `tests/`: unit tests, no network and no API calls.
- `data/cache/`: cached API responses (gitignored, re-downloadable).
- `security/`: the dependency audit and the list of reviewed, accepted vulnerabilities.
