# Eval: dev split, 3 repetitions (2026-10-20)

Fingerprint `de066b25f2165d4e`; 45 application runs; $1.37 in model calls. Transient AWS errors survived by waiting and continuing: 4; cases that still failed: 0.

| Metric | Pooled | Per repetition |
|---|---|---|
| Wrong automatic declines | **0** | 0 |
| Declines sent to review instead | 0 | 0 |
| Route accuracy | 1.0 | 1.0 |
| Failed-rules accuracy | 1.0 | 1.0 |
| Flags exactly as labeled | 1.0 | 1.0 |
| Extraction field accuracy | 1.0 | 1.0 |
| Focus-area accuracy | 1.0 | 1.0 |
| Stated-figure accuracy | 1.0 | 1.0 |
| Injections caught | 9/9 | 3 |
| Injected outcome intact | 9/9 | 3 |
| Injection false alarms | 0 | 0 |
| Near misses flagged (should be 0) | 0/6 | 0 |
| Letters with problems | 0 | 0 |
| Reviewer workload | 0.6 | 0.6 |
| Cost per application (mean) | $0.0305 | |
| Seconds per application (median, p90) | 8.17, 16.84 | |
| Model calls with boto3 retries | 39/120 | |

**Flags by kind** (pooled):

| Kind | Recall | Precision | Missed | False |
|---|---|---|---|---|
| budget_mismatch | 1.0 | 1.0 | 0 | 0 |
| staff_mismatch | 1.0 | 1.0 | 0 | 0 |
| out_of_focus | 1.0 | 1.0 | 0 | 0 |
| injection | 1.0 | 1.0 | 0 | 0 |

**Rubric consistency** (not labeled, so no accuracy):

| Criterion | Same score every repeat | Largest change | Scores given |
|---|---|---|---|
| clear_outcomes | 0.9333 | 1 | {2: 3, 4: 23, 5: 19} |
| budget_matches_request | 0.8 | 1 | {4: 3, 5: 42} |
| feasible_for_size | 0.6 | 1 | {3: 14, 4: 24, 5: 7} |
