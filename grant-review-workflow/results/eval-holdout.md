# Eval: holdout split, 3 repetitions (2026-10-20)

Fingerprint `05ac1540a968e118`; 60 application runs; $2.21 in model calls. Transient AWS errors survived by waiting and continuing: 8; cases that still failed: 0.

| Metric | Pooled | Per repetition |
|---|---|---|
| Wrong automatic declines | **0** | 0 |
| Declines sent to review instead | 0 | 0 |
| Route accuracy | 1.0 | 1.0 |
| Failed-rules accuracy | 1.0 | 1.0 |
| Flags exactly as labeled | 1.0 | 1.0 |
| Extraction field accuracy | 0.9985 | 0.9955-1.0 |
| Return name read (box C) | 1.0 | 1.0 |
| Focus-area accuracy | 1.0 | 1.0 |
| Stated-figure accuracy | 1.0 | 1.0 |
| Injections caught | 18/18 | 6 |
| Injected outcome intact | 18/18 | 6 |
| Injection false alarms | 0 | 0 |
| Near misses flagged (should be 0) | 0/3 | 0 |
| Letters with problems | 0 | 0 |
| Reviewer workload | 0.95 | 0.95 |
| Cost per application (mean) | $0.0368 | |
| Seconds per application (median, p90) | 11.61, 41.23 | |
| Model calls with boto3 retries | 82/202 | |

**Flags by kind** (pooled):

| Kind | Recall | Precision | Missed | False |
|---|---|---|---|---|
| budget_mismatch | 1.0 | 1.0 | 0 | 0 |
| staff_mismatch | 1.0 | 1.0 | 0 | 0 |
| name_mismatch | 1.0 | 1.0 | 0 | 0 |
| out_of_focus | 1.0 | 1.0 | 0 | 0 |
| injection | 1.0 | 1.0 | 0 | 0 |

**Rubric consistency** (not labeled, so no accuracy):

| Criterion | Same score every repeat | Largest change | Scores given |
|---|---|---|---|
| clear_outcomes | 1.0 | 0 | {2: 3, 3: 3, 4: 39, 5: 15} |
| budget_matches_request | 1.0 | 0 | {5: 60} |
| feasible_for_size | 0.8 | 1 | {3: 8, 4: 22, 5: 30} |
