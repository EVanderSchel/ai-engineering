# grant-review-workflow

Project 5 of the AI Engineer roadmap: a **multi-step agent workflow on AWS** that reviews nonprofit grant applications. A packet (the applicant's Form 990 plus a written narrative) goes through extraction, classification, research and scoring with Claude on Amazon Bedrock; the workflow then declines, fast-tracks, or **pauses for a human reviewer**, and resumes when the reviewer decides.

Plain language: Project 3's agent answered one question in one sitting. Real business processes are longer: several steps, some done by a model, some by plain code, some by a person who may take a day to respond. A **workflow** fixes the order of the steps and lets the model decide only where judgment is needed; **checkpoints** save progress after every step, so a crash, a retry or a week-long wait for a reviewer never loses work or pays for the same model call twice. It's like a loan file moving between desks: each desk adds its part, and the file remembers where it is.

## Why this domain

- **Builds on Projects 2-4.** Extraction from Form 990 page images (Project 2), the ProPublica research tools (Project 3) and NTEE classification (Project 4) become steps in a larger process.
- **Answers can be checked.** The IRS e-file XML gives true figures for every real filing, so the gold set's packets have known correct extractions and known correct routing, not judgments made up after the fact.
- **The shape of real intake work.** Receive documents, triage, research, route, human decision: the same pattern as insurance claims or loan applications, on public data.

Rules: only public IRS data and ProPublica's JSON API (as in Project 3). Applicant narratives in the gold set are written for the project and labeled as such; no real applications or personal data.

## Plan

| Step | What | Concept |
|---|---|---|
| 0 | AWS foundations (sign-in, Region, free plan, budget, Bedrock model access and quotas, a first Converse call) and scaffolding: uv project, ruff, pytest, CI with change detection, dependency audit *(done)* | Calling Claude through Bedrock vs. the Anthropic API |
| 1 | Design doc ([docs/design.md](docs/design.md)): state diagram (steps, branches, where a human decides) and state schema; gold set of 40 packets with known correct outcomes *(done)* | Workflow vs. agent: fixed steps where possible, model judgment where needed |
| 2 | Each step as a plain, tested function on Bedrock: intake, extract, research, read_proposal, cross_check, eligibility, route, draft_letter *(done)* | Steps testable and swappable on their own |
| 3 | Orchestration with LangGraph: shared state, conditional routing, parallel research, the tool-using agent as one step | Graph orchestration; what a framework adds over a hand-built loop |
| 4 | Durability and human-in-the-loop: checkpoints in DynamoDB, pause and resume on a reviewer's decision, idempotent steps, a kill-and-resume test | Durable execution |
| 5 | Evals: per-step and end-to-end (routing accuracy, human escalation rate, cost and latency per application, path taken), gating pull requests | How errors compound across steps |
| 6 | Safety: prompt injection in applicant narratives, Bedrock Guardrails vs. code checks, PII handling, least-privilege roles per step | Governance for untrusted and sensitive input |
| 7 | Infrastructure as code with CDK (TypeScript): S3 uploads trigger the workflow on AgentCore Runtime, DynamoDB, per-component IAM roles, tracing, OIDC deploys from GitHub Actions | IaC, event-driven design, AWS |
| 8 | Reviewer UI in React + TypeScript: queue, evidence with citations, approve or override (which resumes the workflow) | Full-stack delivery |
| 9 | *(Optional)* The same workflow on Step Functions, compared on evidence | Framework vs. AWS-native orchestration |
| Final | Held-out test split scored once; cost report; release `grant-review-workflow-v1.0.0` | |

## Step 1: design and gold set

- **The design** ([docs/design.md](docs/design.md)): a fictional fund, the *Prairie Commons Fund*, with eligibility rules fixed to a round closing 2025-06-30 (so right answers never drift with the calendar). Three steps run in parallel (extract the 990, research the organization, read the proposal), code cross-checks them and applies the rules, and every eligible application pauses for a person. The model never declines anyone: automatic declines come only from rules computed by code.
- **The rules as code** ([src/fund_rules.py](src/fund_rules.py)), tested at every boundary by hand, because the same function labels the gold set and will run in the workflow.
- **The gold set** ([data/gold/](data/gold/)): 40 application packets built on 40 real Form 990s (answer keys copied from Project 2), split 15 dev / 25 test. 15 should be declined (13 by their real figures, 2 by their request), 13 are clean, 12 carry a planted problem (a budget or staff count far from the return, a name that doesn't match the EIN, a project outside the focus areas), 4 clean ones claim figures 10-20% off that must *not* be flagged, and 8 contain prompt injections. Every label is computed (`tests/test_gold_set.py` recomputes them), every narrative was checked by code for exactly its spec's figures and approved by a person, and the forms and narratives are marked as written for this project: the organizations never applied.
- **Checked against reality before building:** the first draft of the rules declined 37 of the 60 real filings and measured "recent" against today; both were fixed before any packet was made.

## AWS setup

- **Project and Region:** one AWS project in `us-east-2` (confirmed in AWS Settings). Every Regional resource is created there.
- **CLI profile:** `agent-toolkit`. Sign in with `aws login --profile agent-toolkit`; sessions expire, so sign in again when a command says so.
- **Plan and spending:** free plan with credits; a monthly AWS Budget emails at 50%, 80% and 100%; a spend limit in AWS Settings is the hard stop.
- **Models:** every Claude model on Bedrock here is called through a `us.` inference profile (for example `us.anthropic.claude-sonnet-4-6`), which keeps requests within US Regions. The account can use Claude Sonnet 4.6, Haiku 4.5 and Opus 4.6 (plus older 4.5 models); Claude 5.x models have a token-per-minute quota of 0, and AWS declined increases for Sonnet 5.5 and Haiku 5.5 on 2026-10-16 (access "may depend on factors such as regional availability, account history, and usage patterns"). The workflow uses Sonnet 4.6 and Haiku 4.5, with Opus 4.6 as the eval judge. The model ID is a setting per step, so moving to a newer model later is a one-line change that the evals then judge.

## Setup

```powershell
cd grant-review-workflow
uv sync            # creates .venv with the locked dependencies
uv run pytest      # tests
uv run ruff check . ; uv run ruff format --check .
bash security/audit.sh   # known vulnerabilities in locked dependencies (Git Bash)

# Calls to Bedrock (need `aws login --profile agent-toolkit` first)
$env:AWS_PROFILE = "agent-toolkit"
uv run python src/bedrock.py   # one small call per model: access, structured output, cost
```

**extract (step 2c)** reads page 1 of each return at 1568 px and transcribes the 12 Part I figures the workflow uses (prompt `prompts/extract/v1.txt`, adapted from Project 2's measured prompt). On the 15 dev packets, scored against the IRS answer keys (`uv run python src/extract.py --model sonnet|haiku`, results in `results/`):

| Model | Fields right | Same eligibility verdict | Revenue not adding up | Cost per return | Median time in model |
|---|---|---|---|---|---|
| Sonnet 4.6 | **165/165** | 15/15 | 0 | $0.017 | 3.4 s |
| Haiku 4.5 | 164/165 | 15/15 | 1 | $0.0056 | 2.5 s |

Haiku's one error (gp-036's total revenue read as $51,306, not $51,308) was caught by the form's own arithmetic, the only return where revenue didn't add up, so it would reach a reviewer as a flag. It also sat $1,306 above the fund's $50,000 revenue minimum: a digit misread near a threshold is how a wrong automatic decline would happen. **Extract uses Sonnet 4.6**: the saving from Haiku is about a cent per application. One run on 15 returns is a small sample; step 5 repeats it.

**draft_letter (step 2g)** writes the decision letter from the decision and its reasons, never from the narrative: an automatic decline quotes the fund's failed rules word for word, a reviewer's decision quotes the reviewer's reason. Code checks every letter (the decision stated, every reason quoted, no dollar amount that isn't the request or in a reason, an approval states the amount); a draft that fails is sent back once with the problems listed, and anything still wrong is kept in the state for a person. Prompt v1 left the reason out of an approval (Sonnet) and the amount out of three approvals (Haiku); v2 says both apply to every letter, and both models then wrote 15/15 correct letters with no revision needed. **Letters use Haiku 4.5** ($0.0023 a letter vs $0.0071): the content is guaranteed by code, so the cheaper model costs nothing in quality.

**Step 2 in sum** (dev split, about $1.50 of model calls in all):

| Step | How | Model | Cost per application |
|---|---|---|---|
| intake | code | | $0 |
| extract | page 1 image to 12 figures | Sonnet 4.6 | $0.017 |
| research | code lookup; model only for names that differ; agent only for different organizations | Haiku 4.5 (names), Sonnet 4.6 (agent) | $0 to $0.02 |
| read_proposal | narrative to focus, claims, rubric, injections | Sonnet 4.6 | $0.012 |
| cross_check, eligibility, route | code | | $0 |
| draft_letter | letter from decision and reasons, checked by code | Haiku 4.5 | $0.002 |

About 3-4 cents per application before the reviewer decides. Step 3 joins the steps into one workflow.

**cross_check, eligibility and route (step 2f)** are plain code (`src/checks.py`). cross_check turns the three parallel steps' findings into flags with evidence: name mismatch, a stated budget or staff count more than 25% off the return, out of focus, injection, not found, revenue that doesn't add up as read, and the return disagreeing with the public record *for the same period* (ProPublica's latest figures are often for a newer return). eligibility applies the fund's rules to the extracted figures, with any reviewer corrections laid over them. route declines only when a rule fails and the figures are trusted (design decision 6). Given the right inputs, all 15 dev packets get exactly their labeled flags, failed rules and route (`tests/test_checks.py`), so errors in a full run are the models', not the logic's.

**read_proposal (step 2e)** reads the applicant's narrative, which is untrusted, into a focus area, the figures it states, rubric scores with quotes, and any text addressed to a reviewer or an AI. The narrative goes in as `unverified_text`, injected instructions are quoted rather than followed, stated figures are recorded rather than judged, and code checks the answer: every quote must be found in the narrative and every stated figure written in it (`grounding_problems`). On the 15 dev packets (`uv run python src/read_proposal.py --model sonnet|haiku`):

| Prompt, model | Focus area | Stated figures | Injections caught | False alarms | Answers with an unfindable quote | Cost (15) |
|---|---|---|---|---|---|---|
| v1, Sonnet 4.6 | 13/15 | 15/15 | 3/3 | 0 | 10 | $0.189 |
| v2, Sonnet 4.6 | **15/15** | 15/15 | 3/3 | 0 | 0 | $0.184 |
| v2, Haiku 4.5 | 15/15 | 15/15 | 3/3 | 0 | 0 | $0.065 |

v1's focus misses were boundaries in the prompt's own definitions (home help as housing, a pre-apprenticeship course as education); v2 draws them. v1's unfindable quotes were real lines stitched together; v2 asks for one passage, and the check accepts verbatim pieces joined by " / ", "...", or punctuation while still failing any invented piece. A Haiku run once returned the rubric as a JSON string; `bedrock.structured` now asks once more when an answer doesn't fit its schema, and counts both attempts' cost. **read_proposal uses Sonnet 4.6**: the two tie on everything the gold set labels, but Haiku scored nearly everything 4-5 on the rubric, which ranks nothing. The rubric itself isn't labeled (and every gold budget adds up by construction), so step 5 decides whether to evaluate it or drop it.

**research (step 2d)** checks the EIN against the public record (ProPublica's API, from IRS data) as a hybrid: code does the lookup; names that differ after normalizing ("CO 1" vs "Company No 1 Incorporated") go to one Haiku call; names of truly different organizations go to a Bedrock tool-use agent (`bedrock.tool_loop`) that searches by the form's name. On the 15 dev packets every organization was found and every name correctly matched (11 in code, 4 by Haiku, $0.006 in all). A made-up mismatch (Hiway Credit Union's name on another charity's EIN) was traced to the right organization in 3 agent turns for $0.020. The two planted name mismatches are in the test split, so they weren't used while building. What the run taught step 2f: ProPublica's latest year often isn't the return's year (compare like years only), some records have no figures (missing means unchecked, not wrong), and newer returns than the one submitted can exist.

`src/bedrock.py` is the only place the workflow calls a model: Bedrock's Converse API with an explicit `maxTokens`, adaptive retries, structured output through a forced tool, and a cost and latency record for every call. First live calls (2026-10-20): a structured one-sentence answer cost $0.0039 on Sonnet 4.6 (2.0 s in the model) and $0.0012 on Haiku 4.5 (0.9 s). Each used about 727 input tokens for a 15-word question: defining a tool adds about 600 tokens of instructions to every structured call.

## Layout

- `src/`: flat modules (`pytest.ini` puts `src` on the import path).
- `tests/`: unit tests, no network and no AWS calls (Bedrock calls are checked against Bedrock's API definition with botocore's Stubber).
- `docs/design.md`: the design: fund rules, steps, state, principles, metrics, gold set plan.
- `data/filings/`: answer keys of the 60 real Form 990s, copied from Project 2 by `src/import_filings.py`.
- `data/gold/`: the 40 application packets (`src/build_gold.py` writes forms, specs and labels; narratives are written and approved separately). See its README.
- `data/raw/`: IRS downloads (gitignored, re-downloadable).
- `security/`: the dependency audit and the list of reviewed, accepted vulnerabilities.
