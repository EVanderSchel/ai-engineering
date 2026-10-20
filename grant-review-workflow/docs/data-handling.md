# Data handling (step 6c)

What an application contains, where it goes, how long it stays, and the rules that keep it that way. The gold
set is fictional (narratives written for this project; returns are public IRS filings), but the workflow is built
as if the applications were real.

## What an application contains

| Input | Personal data? | Notes |
|---|---|---|
| Form: EIN, organization name, amount, project title | No (organizations, not people) | Name and title are free text the applicant types: untrusted |
| Narrative | Sometimes: staff names, the people served described as groups | Untrusted. Should never hold identifiers (below) |
| Form 990 PDF | Officers' names and pay | Public by law (the IRS publishes every 990) |

**Identifiers the fund never needs**: Social Security numbers, payment card numbers, bank routing and account
numbers. If one appears in the name, title or narrative, `cross_check` raises a `sensitive_data` flag
(`src/sensitive.py`). The flag sends the application to a person, says which field and where, and never repeats
the value. The reviewer asks the applicant to resubmit without it. Contact details (names, emails, phones) aren't
flagged: narratives name staff, and the guardrail's contact-detail findings on dev all described groups ("ages 8
to 14") or places.

## Where it goes

| Where | What | How long | Protection |
|---|---|---|---|
| Amazon Bedrock (`us.` inference profiles, from us-east-2) | Page-1 image, narrative, names, letter inputs | Not stored by Bedrock; not used for training | Requests stay within US Regions (the `us.` profile's boundary); TLS |
| Bedrock guardrail `grant-review-screen` v1 (Classic, us-east-2) | Name, title, narrative (step 6b's comparison only) | Not stored | Matched personal data in its responses is dropped in `guardrail.screen_text` |
| ProPublica Nonprofit Explorer API | The EIN (public), and search terms for an organization name | ProPublica's own logs | Organization data only: never the narrative |
| DynamoDB `grant-review-checkpoints` (us-east-2) | The whole application state: form, narrative, readings, flags, letter, reviewer's decision | 30-day TTL from the last checkpoint, then deleted by DynamoDB (decision 8) | Encrypted at rest (AWS-owned key); on-demand; no backups |
| Local `data/` | Return PDFs, ProPublica cache, red-team PDFs | Until deleted | Gitignored |
| `results/` (committed) | Scores, flags, routes, costs; short rubric quotes from fictional narratives | In git history | Never holds real applicants' text (rule 4) |

## Logging (checked 2026-10-20, us-east-2)

- **Bedrock model invocation logging is off** (`get-model-invocation-logging-configuration` returns no
  configuration), so no prompt or answer is written to CloudWatch Logs or S3. Turning it on would copy every
  narrative into the logs, including any identifier the `sensitive_data` flag catches: the guardrail's PII
  masking doesn't apply to those logs.
- No CloudWatch log groups and no CloudTrail trails. CloudTrail's default event history keeps 90 days of
  management events (who created or changed a table or guardrail), not request contents.
- The scripts print flags, routes and costs, never narratives. Flag evidence quotes a suspected injection, which is
  the attacker's text, by design: the reviewer needs to see it.

## Rules

1. **Collect only what the decision needs.** The form asks for no personal data; identifiers are flagged, not
   stored on purpose.
2. **Never repeat an identifier.** Flags name the field and position; guardrail results keep types only.
3. **Keep invocation logging off.** If it's ever needed for debugging: a dedicated log group with a short
   retention (7 days), encrypted with a KMS key, readable only by the debugging role, and turned off afterwards.
4. **Real applications never go in the repo.** Results from real runs stay out of `results/`; evals use the
   gold set.
5. **Stay in US Regions.** `us.` model and guardrail profiles only (the guardrail's Standard tier would need
   `us.guardrail.v1:0`; v1 avoids it). `global.` profiles would send data to any commercial Region: a decision to
   make with the fund, not a tuning knob.
6. **Checkpoints expire.** 30 days covers a review round; nothing else keeps the narrative. A decided application's
   letter is the record (step 7 decides where letters are kept, and for how long).
7. **Least privilege.** The workflow's role may call only the two model profiles, this guardrail and this table
   (step 6d).
