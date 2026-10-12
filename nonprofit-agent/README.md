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
| `get_organization(ein)` | Profile, which tax years have financial figures, which were filed without figures in this data source, and the period of the latest return filed |
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
.\.venv\Scripts\python.exe src\agent.py "How did the American Red Cross's revenue change from 2021 to 2023?"
```

**First runs** (3 questions, then again after fixing what the trajectories showed; $0.16 in total):

| Question | First run | Cause | After the fix |
|---|---|---|---|
| Red Cross revenue 2021 to 2023 | Right (+4.1%), but computed by the model, against instructions | `compute_ratios` could only compare with the previous year | One `compute_ratios` call with `compare_with_year=2021` |
| Which of Iowa's largest food banks depends most on donations? | Right winner, but claimed its 3 were "by far" the largest; missed River Bend Food Reservoir, the 2nd largest ($39.1M) | Searched only "food bank"; search matches words in names | Searched "food bank", "foodbank", "food reservoir"; compared 4; stated its method and that the list may not be exhaustive |
| Red Cross revenue in 2024 (filed as a PDF only) | Didn't guess, but said 2023 was "the most recent filing" | The error message said only "no data" | "Filed, but only as a PDF image: no figures yet" |

Also found: a search with no matches returns HTTP 404 from the API, which the tool reported as an outage; it now returns zero results with a hint. One run per question shows a change, not that it holds every time: step 3 runs each question repeatedly.

## Trajectory evals (step 3)

An eval that scores the agent's answers *and* how it got them, used to measure every change since.

**Questions** ([`evals/cases.jsonl`](evals/cases.jsonl)): 22, reviewed and approved before anything ran, on real and mostly small Iowa nonprofits (so the model can't answer from memory). They cover lookups, changes between years, ratios, comparisons, group questions ("the two largest food banks"), questions the data can't answer (a year with no figures, program spending, a made-up organization), an ambiguous name (Habitat for Humanity: 27 Iowa affiliates), and one that's hard to find by name (The Beacon in Des Moines, filed as Beacon Of Life). Expected figures come from the API data via the tools' own code, not from a model, and each records the tool call that produces it. A test recomputes all of them from a **frozen copy of the API responses** ([`evals/snapshot/`](evals/snapshot/)) with the network blocked, so the answer key can't drift when ProPublica adds data.

**Grading** ([`src/grade.py`](src/grade.py)), two pass/fail metrics per run:

- **correct**: every expected figure appears in the answer (code; rounding allowed), and, for questions with written criteria, a judge model (Claude Opus 5, not the model under test) agrees the answer meets them. Its one-line reason is saved.
- **grounded**: every dollar amount and percentage in the answer appears in some tool result (code, [`src/grounding.py`](src/grounding.py)). Catches figures the model computed or invented, even correct ones.

**Trusting the judge.** [`src/check_judge.py`](src/check_judge.py) feeds it answers that must fail (a made-up figure, "$0" for unreported pay, the wrong year, a non-answer) and correct answers that must pass, twice each. The first version passed "I don't know" once; adding that a criterion to "say" something means saying it explicitly fixed that (8/8, consistent). Then a blind check: the user graded 15 judged answers without seeing the verdicts and agreed with the judge on all 15. On a borderline answer the judge can still flip, so q14's criterion was rewritten to be checkable after that happened.

**Runner** ([`src/run_eval.py`](src/run_eval.py)): calls the real `agent.run`, 3 runs per question, 4 at a time. It saves each row as soon as it's graded (a crashed run resumes), records infrastructure failures separately instead of scoring them as wrong answers, checks that the model that answered is the one requested, and can re-grade saved answers after a criteria change (judge cost only).

```powershell
.\.venv\Scripts\python.exe src\run_eval.py --variant v2 --reps 3
```

**Results** (66 runs each; `evals/runs/<variant>/results.jsonl` holds every answer, trajectory, grade and reason):

| Version | What changed | correct | grounded | Agent cost |
|---|---|---|---|---|
| baseline | Step 2's agent | 97.0% | 92.4% | $0.95 |
| v1 | State the year; compare organizations on the same year; say when newer returns exist than the figures | 98.5% | 92.4% | $1.01 |
| v2 | + grounding guardrail | **100%** | **100%** | $1.06 |

What the runs showed:

- **Prompt rules fixed what they targeted, and the habit moved elsewhere.** v1 stopped the overclaiming about unchecked food banks (q14: 1/3 to 3/3) and fixed the year wording. But the model kept computing or recalling figures in other places (5 ungrounded answers in both versions), and in one run it **invented** a revenue figure ("$17,795,524"; the real one is $12,797,138) that the judge passed, because that question's criteria were about something else.
- **So v2 checks in code.** Before an answer is returned, the agent runs the same grounded check. If any figure isn't in a tool result, the draft goes back once ("retrieve it with a tool or remove it"); a second failure is returned with the figures flagged, never silently. In 66 runs it sent back 5 drafts: 2 fixed by fetching the figure, 3 by removing a self-computed one. None lost a correct figure, and each retry cost about $0.005.
- **Caveats.** v2's grounded score is partly by construction (the guardrail and the metric share one check), and they share its blind spots: a whole-number percentage that happens to match some tool number, and wrong claims that aren't numbers. The eval is now saturated (100/100), so it serves as a regression test; showing further gains needs harder questions. Single runs vary (in v1, q13 once missed River Bend by searching differently), which is why every version runs each question 3 times.

Step 3 cost $6.55 in API calls (pilot, judge checks, baseline with re-grades, v1, v2).

## Future enhancements

New data sources, each a new tool to measure against the eval baseline. In order of value:

1. **Figures from the IRS e-file XML, to close the 1-2 year lag.** ProPublica's extracted figures stop at tax year 2023 for most organizations, while their returns through 2025 are already filed (e.g. Northeast Iowa Food Bank: figures through 2023, latest return June 2025, as of 2026-10-12). The IRS publishes those returns as XML long before the extract catches up, and Project 2 already reads them: `irs-990-extraction/src/sources.py` fetches single returns from the IRS's archives, and `irs_xml.py` pulls the same Part I fields (revenue, expenses, contributions, ...). Locating a return needs the IRS's yearly index (~90 MB). For now the tools only *say* newer returns exist.
2. **Single Audits from the Federal Audit Clearinghouse** ([API](https://www.fac.gov/api/), free api.data.gov key): federal money spent and from which programs, plus audit findings, material weaknesses and going-concern doubts. Covers organizations spending more than $750,000 of federal awards a year ($1M from 2025). Use the FAC API, not ProPublica's audit pages. `findings_text` is free text from outside parties, so it's also input for prompt-injection testing.
3. **Form 990-T (unrelated business income):** income from activities outside the mission and the tax paid on it. Not in ProPublica's API, but in the IRS e-file XML (`RETURN_TYPE` 990T in the IRS index; 23,282 in 2024 vs 363,100 Form 990s), so it shares the work of item 1.

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
- `evals/`: the questions, the frozen API snapshot, and saved eval runs (transcripts in `traces/` are regenerated, not committed).
- `data/cache/`: cached API responses (gitignored, re-downloadable).
- `security/`: the dependency audit and the list of reviewed, accepted vulnerabilities.
