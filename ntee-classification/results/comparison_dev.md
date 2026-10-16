| Approach | Accuracy | Top-3 | Macro-F1 | Broad | One prediction | Per 1,000 | Per 1,000 (batch API) | Training |
|---|---|---|---|---|---|---|---|---|
| TF-IDF baseline | 53.7% | 75.4% | 0.461 | 63.9% | 2 ms | ~$0 |  | ~$0 |
| LoRA Qwen2.5-0.5B | 58.7% | 80.3% | 0.510 | 67.1% | 61 ms | $0.0007 |  | $0.05 |
| LoRA Qwen2.5-1.5B | 60.2% | 80.8% | 0.516 | 68.0% | 75 ms | $0.0024 |  | $0.13 |
| Claude Haiku 4.5 few-shot | 58.7% | 80.3% | 0.529 | 69.8% | 984 ms | $0.72 | $0.36 | ~$0 |
| Claude Sonnet 5.5 guided | 62.5% | 83.1% | 0.558 | 70.8% | 1,149 ms | $0.90 | $0.45 | ~$0 |

- LoRA Qwen2.5-0.5B: training ($0.05) pays for itself against Claude Haiku 4.5 few-shot after 70 predictions.
- LoRA Qwen2.5-1.5B: training ($0.13) pays for itself against Claude Haiku 4.5 few-shot after 178 predictions.
