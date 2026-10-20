# Eval: test split, 3 repetitions (2026-10-20)

Fingerprint `de066b25f2165d4e`; 67 application runs; $2.26 in model calls. Transient AWS errors survived by waiting and continuing: 15; cases that still failed: 8.

| Metric | Pooled | Per repetition |
|---|---|---|
| Wrong automatic declines | **0** | 0 |
| Declines sent to review instead | 0 | 0 |
| Route accuracy | 1.0 | 1.0 |
| Failed-rules accuracy | 1.0 | 1.0 |
| Flags exactly as labeled | 0.8955 | 0.8696-0.92 |
| Extraction field accuracy | 1.0 | 1.0 |
| Focus-area accuracy | 0.9701 | 0.9565-1.0 |
| Stated-figure accuracy | 1.0 | 1.0 |
| Injections caught | 14/14 | 4-5 |
| Injected outcome intact | 14/14 | 4-5 |
| Injection false alarms | 0 | 0 |
| Near misses flagged (should be 0) | 0/5 | 0 |
| Letters with problems | 0 | 0 |
| Reviewer workload | 0.6418 | 0.6316-0.6522 |
| Cost per application (mean) | $0.0338 | |
| Seconds per application (median, p90) | 7.58, 43.14 | |
| Model calls with boto3 retries | 35/214 | |

**Flags by kind** (pooled):

| Kind | Recall | Precision | Missed | False |
|---|---|---|---|---|
| budget_mismatch | 1.0 | 1.0 | 0 | 0 |
| staff_mismatch | 1.0 | 1.0 | 0 | 0 |
| name_mismatch | 1.0 | 0.6 | 0 | 4 |
| out_of_focus | 1.0 | 1.0 | 0 | 0 |
| injection | 1.0 | 1.0 | 0 | 0 |
| figures_disagree | None | 0.0 | 0 | 3 |

**Rubric consistency** (not labeled, so no accuracy):

| Criterion | Same score every repeat | Largest change | Scores given |
|---|---|---|---|
| clear_outcomes | 0.96 | 1 | {2: 4, 3: 2, 4: 39, 5: 22} |
| budget_matches_request | 0.96 | 1 | {4: 4, 5: 63} |
| feasible_for_size | 0.84 | 1 | {2: 1, 3: 18, 4: 22, 5: 26} |
