Test split (n=1,813), threshold 0.7 chosen on dev

| Approach | Accuracy | Sent to Sonnet | Cost per 1,000 | Mean time |
|---|---|---|---|---|
| small model alone | 57.3% | 0% | $0.001 | 62 ms |
| Sonnet alone | 61.4% | 100% | $0.874 | 1,319 ms |
| cascade (t=0.7) | 61.2% | 56% | $0.490 | 799 ms |

- cascade minus Sonnet, 95% paired bootstrap interval -0.9% to +0.5%
