# Costs

What the project cost to build, what one application costs to review, and what the deployment costs when nobody uses it. Figures as of 2026-10-27, Region us-east-2, on AWS's free plan (every charge is paid from credits; the `20Monthly` budget shows $0.00 actual spend).

Plain language: almost all of the money is the model reading each application. The machinery around it (functions, tables, the website) costs fractions of a cent per application and close to nothing while idle.

## Per application

| | Model calls | Everything else |
|---|---|---|
| Dev eval (15 packets x 3, step 5) | $0.031 mean, $0.034 max | |
| Test eval (25 x 3, run once) | $0.034 mean, $0.083 max | |
| Held-out eval (20 x 3, run once, step 6.5) | $0.037 mean, $0.095 max | |
| Live, through the reviewer UI (step 8) | $0.035 | |
| Lambda (1 GB: Start about 10 s, Resume and the API a few seconds each) | | about $0.0003 |
| DynamoDB (checkpoint and status writes, on demand) | | under $0.0001 |
| Step Functions instead of LangGraph (step 9, not deployed) | about the same | $0.0003 in state transitions |

- **A typical application costs about $0.03-0.04**, nearly all of it Sonnet 4.6 reading the return's page image (extract) and the narrative (read_proposal). The maxima are applications whose names differ from the public record: research then runs a tool-using agent.
- **Model prices are the `us.` inference profiles'**, 10% above the `global.` ones (`bedrock.MODELS`, from AWS's public price list). `global.` profiles would cut the model cost by about a tenth, at the price of requests leaving US Regions (a data-residency choice, not made here).
- **At 1,000 applications a month**: about $35 in model calls and well under $1 for everything else.

## Building it

Model calls from the project's own records (each run saves its cost):

| Step | Model calls |
|---|---|
| 2: each step built and compared on dev (extract, research, read_proposal, draft_letter; two models each) | about $1.50 |
| 3-4: the graph and checkpoints, end-to-end dev runs | $0.92 |
| 5: dev eval 3x15, test eval 3x25 (run once) | $3.69 |
| 6: red team, guardrail comparison | $0.80 |
| 6.5: read_proposal v3 on dev, dev eval, red team rerun, held-out eval (run once) | $4.52 |
| 8: one real review through the deployed UI | $0.04 |
| 9: LangGraph vs Step Functions (a stopped run $0.65, the full run $0.98) | $1.63 |
| **Total** | **about $13.10** |

Everything else (Lambda, DynamoDB, S3, CodeBuild, CloudFront, Cognito, CloudWatch) stayed within the free tiers or rounded to cents. The free plan's credits have grown over the project (earned credits outweighed the spend): $126.57 remain.

## Idle, per month

| What | About |
|---|---|
| ECR: the CDK image repository holds every image deployed so far (8 images, 2.4 GB; only the latest runs) | $0.24 |
| CloudWatch alarm `GrantReview-decision-not-finished` | $0.10 |
| DynamoDB, S3, CloudWatch Logs (30-day retention), CloudFront, Cognito (Essentials, under 10,000 users), Lambda, CodePipeline V2 (billed per action-minute), API Gateway | $0.00 |
| **Total** | **about $0.35** |

- Data expires on its own: checkpoints, status records and intake objects after 30 days, logs after 30 days.
- The guardrail `grant-review-screen` costs nothing while unused.
- The ECR figure grows by about 0.3 GB ($0.03 a month) with every new image; a lifecycle rule keeping the last few would cap it.

## What would change the bill

- **More applications:** linear in model calls, about $0.035 each.
- **Bedrock outages:** a failed call that boto3 retries costs nothing extra; a step that fails after its retries is rerun from the last checkpoint, so only that step is paid twice.
- **Repeats:** an application ID is never run twice (`workflow.start` refuses), so a duplicate upload can't double the cost.
