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
| 2 | Each step as a plain, tested function on Bedrock: extract, classify, research, score, draft | Steps testable and swappable on their own |
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
- **Models:** every Claude model on Bedrock here is called through a `us.` inference profile (for example `us.anthropic.claude-sonnet-4-6`), which keeps requests within US Regions. Claude 5.5 models need a token-per-minute quota above the account's starting value of 0 (requested on 2026-10-16); until then development uses Sonnet 4.6 and Haiku 4.5. The model ID is a setting, so switching is a one-line change that the evals then judge.

## Setup

```powershell
cd grant-review-workflow
uv sync            # creates .venv with the locked dependencies
uv run pytest      # tests
uv run ruff check . ; uv run ruff format --check .
bash security/audit.sh   # known vulnerabilities in locked dependencies (Git Bash)
```

## Layout

- `src/`: flat modules (`pytest.ini` puts `src` on the import path).
- `tests/`: unit tests, no network and no AWS calls.
- `docs/design.md`: the design: fund rules, steps, state, principles, metrics, gold set plan.
- `data/filings/`: answer keys of the 60 real Form 990s, copied from Project 2 by `src/import_filings.py`.
- `data/gold/`: the 40 application packets (`src/build_gold.py` writes forms, specs and labels; narratives are written and approved separately). See its README.
- `data/raw/`: IRS downloads (gitignored, re-downloadable).
- `security/`: the dependency audit and the list of reviewed, accepted vulnerabilities.
