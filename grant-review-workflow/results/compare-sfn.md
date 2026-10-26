# LangGraph vs Step Functions: dev packets (2026-10-26)

Run `20261026192744`, 15 packets, the same decisions for both. Both versions reached the same outcome on 15 of the 15 packets both finished.

| | LangGraph on Lambda | Step Functions |
|---|---|---|
| Finished | 15 | 15 |
| Wrong automatic declines | 0 | 0 |
| Route accuracy | 1.0 | 1.0 |
| Flags exactly as labeled | 1.0 | 1.0 |
| Extraction field accuracy | 1.0 | 1.0 |
| Letters with problems | 0 | 0 |
| Upload to first stop, median s | 10.28 | 6.67 |
| Decision to finish, median s | 2.09 | 4.37 |
| System time per application, median s | 10.28 | 9.6 |
| Model calls, $ | 0.4894 | 0.4909 |
| Lambda invocations | 25 | 159 |
| Model calls with boto3 retries | 11 | 1 |

Step Functions also made 198 state transitions ($0.005); failed tasks, all retried: {'Extract': 2}.

## Median seconds per stage

| Stage | LangGraph | Step Functions |
|---|---|---|
| extract+read_proposal+research | 6.29 | 3.81 |
| cross_check | 0.0 | 0.1 |
| eligibility | 0.0 | 0.09 |
| route | 0.0 | 0.09 |
| draft_letter | 1.76 | 2.03 |
| intake |  | 0.27 |
| join |  | 0.1 |
| apply_review |  | 0.13 |
