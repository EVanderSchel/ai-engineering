# nonprofit-agent

Project 3 of the AI Engineer roadmap: a **tool-using agent** that answers research questions about US charities, such as "How did the American Red Cross's revenue change over the last three years?" or "Which of Iowa's largest food banks depends most on donations?". It works from public data only.

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

## The tools (step 1)

Plain Python functions in [`src/tools.py`](src/tools.py), over a polite, caching API client ([`src/propublica.py`](src/propublica.py)). No Claude calls yet: a tool is ordinary code, and the model only ever sees its description and its output.

| Tool | Returns |
|---|---|
| `search_organizations(query, state=None, page=0)` | Up to 25 matches per page: EIN, name, city, state, NTEE category code |
| `get_organization(ein)` | Profile, plus which tax years have financial data and which were filed only as PDFs |
| `get_financials(ein, tax_year=None)` | Per year: form type, revenue, expenses, assets, liabilities; on the full Form 990 also contributions, program revenue, officer pay, other salaries, fundraising fees |
| `compute_ratios(ein, tax_year, compare_with_year=None)` | Surplus and margin, liabilities to assets, contributions share, officer pay share, and change from another year (default: the previous year with data) |

Design choices, and why:

- **Small, plain-named output.** A raw filing has 60+ cryptic fields (`totfuncexpns`); every byte a tool returns is input tokens the model pays for and has to interpret. Tools return a handful of named fields.
- **Blank is not zero.** Fields a form doesn't have (a 990-PF has no officer-pay line here) come back as `null`, never 0, the same rule as Project 2.
- **Arithmetic happens in code.** `compute_ratios` exists so the model reports numbers instead of calculating them.
- **Errors tell the model what to do next.** E.g. "No financial data for tax year 2024. Years with data: [2023, 2022, ...]" rather than a stack trace.

What the data can't answer (found while exploring the API): **program vs. overhead spending** isn't in ProPublica's extracted fields, and the **newest filings** are often PDF-only (the Red Cross's 2024 return, as of 2026-10-08). The agent has to say so rather than guess.

## The agent loop (step 2)

[`src/agent.py`](src/agent.py) is the loop by hand, on the Claude API (Claude Sonnet 5): send the question, system prompt and tool descriptions; when Claude asks for tools, run them and send the results back; repeat until it answers. Limits: 10 model calls and $0.25 per question. Tool failures (bad EIN, unknown year, network) go back to Claude as error results instead of crashing the loop. The prompt and tools are cached, and every run's full trajectory (each tool call and result) is saved in `data/runs/`.

```powershell
.\.venv\Scripts\python.exe srcgent.py "How did the American Red Cross's revenue change from 2021 to 2023?"
```

**First runs** (3 questions, then again after fixing what the trajectories showed; $0.16 in total):

| Question | First run | Cause | After the fix |
|---|---|---|---|
| Red Cross revenue 2021 to 2023 | Right (+4.1%), but computed by the model, against instructions | `compute_ratios` could only compare with the previous year | One `compute_ratios` call with `compare_with_year=2021` |
| Which of Iowa's largest food banks depends most on donations? | Right winner, but claimed its 3 were "by far" the largest; missed River Bend Food Reservoir, the 2nd largest ($39.1M) | Searched only "food bank"; search matches words in names | Searched "food bank", "foodbank", "food reservoir"; compared 4; stated its method and that the list may not be exhaustive |
| Red Cross revenue in 2024 (filed as a PDF only) | Didn't guess, but said 2023 was "the most recent filing" | The error message said only "no data" | "Filed, but only as a PDF image: no figures yet" |

Also found: a search with no matches returns HTTP 404 from the API, which the tool reported as an outage; it now returns zero results with a hint. One run per question shows a change, not that it holds every time: step 3 runs each question repeatedly.

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
