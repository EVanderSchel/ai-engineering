# grant-review-workflow infrastructure (CDK, TypeScript)

Step 7: the workflow's AWS resources as code, in us-east-2. The account comes from the signed-in profile
(`CDK_DEFAULT_ACCOUNT`); no account ID is committed.

| Stack | What | Notes |
|---|---|---|
| `GrantReviewData` | Checkpoint table `grant-review-checkpoints`, intake bucket | Stateful: termination protection, RETAIN, table deletion protection |

```powershell
cd grant-review-workflow/infra
npm ci
npm test                      # assertion tests, including cdk-nag (AWS Solutions checks)
npm run build                 # type-check
$env:AWS_PROFILE = "agent-toolkit"
npx cdk synth --strict        # template; any unacknowledged cdk-nag finding fails here
npx cdk diff GrantReviewData  # always before a deploy
```

**Adopting the existing table.** `src/checkpoint_table.py` created the table in step 4. CDK takes it over once with
`npx cdk import GrantReviewData -c adopt=true`: in that mode the stack is an exact copy of the live table and
nothing else (an import change set may only add the imported resource). The next normal deploy turns on deletion
protection and adds the intake bucket.
