Calibration (all dev): the small model's accuracy by its confidence, and Sonnet's on the same missions

| Confidence | Missions | Share | Mean confidence | Small model right | Sonnet right |
|---|---|---|---|---|---|
| 0.00-0.50 | 555 | 31% | 0.36 | 33.3% | 43.8% |
| 0.50-0.80 | 613 | 34% | 0.64 | 53.8% | 57.4% |
| 0.80-0.95 | 395 | 22% | 0.88 | 82.0% | 80.5% |
| 0.95-0.99 | 203 | 11% | 0.97 | 87.7% | 85.7% |
| 0.99-1.00 | 47 | 3% | 0.99 | 100.0% | 100.0% |

Cascade, thresholds chosen on the tune half, scored on the report half (n=907)

| Approach | Accuracy | Sent to Sonnet | Cost per 1,000 | Mean time |
|---|---|---|---|---|
| small model alone | 59.4% | 0% | $0.001 | 61 ms |
| Sonnet alone | 63.8% | 100% | $0.901 | 1,348 ms |
| cascade, match_sonnet (t=0.7) | 64.9% | 53% | $0.478 | 737 ms |
| cascade, most_accurate (t=0.8) | 64.6% | 64% | $0.574 | 885 ms |
| ceiling: either model right | 70.6% | | | |

- match_sonnet: accuracy minus Sonnet's, 95% paired bootstrap interval +0.1% to +2.2%
- most_accurate: accuracy minus Sonnet's, 95% paired bootstrap interval +0.1% to +1.5%
