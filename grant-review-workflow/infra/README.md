# grant-review-workflow infrastructure (CDK, TypeScript)

Step 7: the workflow's AWS resources as code, in us-east-2. The account comes from the signed-in profile
(`CDK_DEFAULT_ACCOUNT`); no account ID is committed, and the app refuses to run without one.

| Stack | What | Deployed by |
|---|---|---|
| `GrantReviewData` | Checkpoint table `grant-review-checkpoints`, intake bucket `grant-review-intake-<account>` | The pipeline |
| `GrantReviewWorkflow` | `start` and `resume` functions (one image), upload rule, failed-applications queue | The pipeline |
| `GrantReviewPipeline` | GitHub connection, CodePipeline `grant-review-workflow` (Source, Diff, Approve, Deploy) | A person, once |
| `GrantReviewCdkAccess` | The CDK execution policy and the permissions boundary | A person, with their own credentials |

```powershell
cd grant-review-workflow/infra
npm ci
npm test                      # jest: assertion tests, cdk-nag (AWS Solutions), CloudTrail coverage of the execution policy
npm run build                 # type-check
$env:AWS_PROFILE = "agent-toolkit"
npx cdk synth --strict        # any unacknowledged cdk-nag finding fails here
npx cdk diff GrantReviewData GrantReviewWorkflow --exclusively
```

**Deploys** happen in the pipeline: merge to main (touching `grant-review-workflow/`), read the Diff stage's
`cdk diff` log, approve in the CodePipeline console, and the Deploy stage runs
`cdk deploy GrantReviewData GrantReviewWorkflow --exclusively`. The pipeline and access stacks are deployed by hand
(`npx cdk deploy GrantReviewPipeline --exclusively`, `npx cdk deploy GrantReviewCdkAccess --exclusively`).

**What deploys may do.** CloudFormation's execution role has only `grant-review-cdk-execution` (the bootstrap was
re-run with `--cloudformation-execution-policies arn:aws:iam::<account>:policy/grant-review-cdk-execution`). It
allows the calls our deploys make (`test/cloudtrail-actions.json`, from CloudTrail) on `GrantReview*` /
`grant-review-*` names, and creates roles only with the `grant-review-workload-boundary` permissions boundary.
A new resource type, or a name outside those prefixes, needs the policy extended first: change
`lib/access-stack.ts` and deploy `GrantReviewCdkAccess` yourself. To undo the narrowing:
`npx cdk bootstrap aws://<account>/us-east-2 --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess`.

**Adopting the existing table.** `src/checkpoint_table.py` created the table in step 4. CDK took it over once with
`npx cdk import GrantReviewData -c adopt=true --no-version-reporting --resource-mapping-inline
'{"Checkpoints620E2F20":{"TableName":"grant-review-checkpoints"}}'` (in that mode the stack is an exact copy of the
live table and nothing else; the CDKMetadata resource can't be part of an import change set), on 2026-10-21. Drift
detection was IN_SYNC; the next deploy turned on deletion protection and added the intake bucket (1,092 items kept).

**The GitHub connection** was created pending and completed once in the console (CodePipeline, Settings,
Connections), with the "AWS Connector for GitHub" app installed on this repository only.
