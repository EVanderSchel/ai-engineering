# Costs

What a request costs to serve, what the project cost to build, and what it costs to run when nobody uses it. Figures as of 2026-11-16, from the project's own run records and measurements. Model prices are the gateway's own price list (`migrations/`), and AWS prices are us-east-2 on-demand. AWS charges were paid from the free plan's credits.

Plain language: the gateway itself is cheap; the models behind it are what cost money. A request through the gateway costs what the model charges, plus a few milliseconds and a fraction of a cent of machine time. Building it cost about a dollar and a half, because almost every test runs against a free stand-in for the model.

## Per request

| | Time | Money |
|---|---|---|
| The gateway itself: keys, limits, budget hold and settlement, ledger, logs, metrics (step 10, one CPU) | about 30 ms at the median, nearly all of it PostgreSQL; 6 ms without a database | the machine time below |
| Free guardrails: personal data, injection patterns (step 5) | about 1 ms | none |
| Traces (step 7) | about 1 ms | none |
| The model, through `smart` (Claude Sonnet 5.5, falling back to Sonnet 4.6 on Bedrock) | the model's | $2 per million input tokens and $10 per million output; $3.30 / $16.50 after a fallback |
| The model, through `fast` (Claude Haiku 4.5, Bedrock first) | the model's | $1.10 / $5.50 on Bedrock, $1 / $5 on the Claude API |
| The opt-in injection judge (Claude Haiku 5.5, step 5) | about 1.3 s (p95 2.9 s) | about $0.00013 per screened request |
| The opt-in drift judge (step 9), on the share of answers sampled | after the answer, in the background | about $0.0003 per judged answer |

Model costs reach the team's ledger as they're charged. The gateway holds the worst case (`max_tokens`) against the budget first, so a team never goes over (step 4, step 10).

**The self-hosted models**, at $0.357 an hour (a c7i.2xlarge, the comparable cloud CPU) and fully busy:

| | Per 1,000 |
|---|---|
| `local`, Qwen2.5-0.5B Q8_0 on llama.cpp, 128-token answers (step 6) | $0.21 |
| The same model on vLLM on a T4 GPU, at about the same hourly price | $0.0034 |
| The NTEE classifier, 8-bit weight-only on ONNX Runtime, one at a time | $0.11 |
| Claude Haiku 4.5 on the same classification task, for comparison | $0.72 |

A self-hosted model is paid by the hour whether it's busy or not, so these hold only at full load.

**The gateway's own machines on EKS** (step 11): two `c7i-flex.large` nodes and the control plane, about $0.30 an hour, served 60 requests a second with room to spare. That's about $0.0014 per 1,000 requests at that rate.

## Building it

| Step | Paid runs | About |
|---|---|---|
| 2 | four calls through the first gateway (Claude API) | $0.002 |
| 3 | the Bedrock adapter and fallback, four calls | $0.002 |
| 5 | the injection judge on 60 labelled messages, dev and held-out (`results/injection-judge-*.json`), and a live check | $0.008 |
| 6 | vLLM on a free Colab T4 | $0 |
| 9 | the canary's baselines and noise runs, 8 runs of 60 questions (priced from their token counts in `results/canary/noise/`) | $1.09 |
| 9 | the drift judge's reference (`results/canary/judge/`) | $0.034 |
| 11 | the EKS cluster for 92 minutes: control plane, nodes, IPs, a volume, ECR (estimated from run times; see the README, step 11 stage 4) | $0.42 |
| **Total** | | **about $1.55** |

Everything else was free: steps 1, 4, 7, 8 and 10 used the stub provider or the fake upstream, and the load tests, alert drills and the LiteLLM comparison ran on this PC. The AWS figure is the only estimate; the rest are from recorded token counts and the price list.

## Idle

| | Per month |
|---|---|
| As left now: nothing deployed (the EKS cluster and its ECR repository were deleted) | $0 |
| The kind cluster and Docker Compose on this PC | $0 (local) |
| The CI canary, on a pull request that changes a checked route or prompt (only once the repository secret is added) | about $0.27 per push |
| If the EKS cluster had been left running (estimate: control plane $73, two nodes with Auto Mode's fee about $140, public IPs $7) | about $220 |

The budget alert `ai-gateway-eks-5` ($5 a month, credits excluded) stays on the account as a guard.
