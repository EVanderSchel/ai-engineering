| Approach | Accuracy | Top-3 | Macro-F1 | Broad | One prediction | Per 1,000 | Per 1,000 (batch API) | Training |
|---|---|---|---|---|---|---|---|---|
| TF-IDF baseline | 51.8% | 73.9% | 0.449 | 63.2% | 2 ms | ~$0 |  | ~$0 |
| LoRA Qwen2.5-0.5B | 57.3% | 80.5% | 0.490 | 66.8% | 62 ms | $0.0007 |  | $0.05 |
| LoRA Qwen2.5-1.5B | 60.8% | 82.3% | 0.531 | 69.2% | 74 ms | $0.0022 |  | $0.12 |
| Claude Haiku 4.5 few-shot | 56.5% | 78.8% | 0.513 | 68.2% | 1,012 ms | $0.69 | $0.35 | ~$0 |
| Claude Sonnet 5.5 guided | 61.4% | 83.0% | 0.547 | 70.7% | 1,214 ms | $0.87 | $0.44 | ~$0 |
| Cascade: LoRA 0.5B, then Sonnet | 61.2% | 81.9% | 0.544 | 70.3% | 799 ms | $0.49 |  | $0.05 |

- LoRA Qwen2.5-0.5B: training ($0.05) pays for itself against Claude Haiku 4.5 few-shot after 73 predictions.
- LoRA Qwen2.5-1.5B: training ($0.12) pays for itself against Claude Haiku 4.5 few-shot after 177 predictions.
