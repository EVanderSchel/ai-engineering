# rag

From-scratch Retrieval-Augmented Generation pipeline, built without LangChain or similar frameworks on purpose, so every step (chunk → embed → store → retrieve → generate → evaluate) is visible and swappable. See README.md for the pipeline description and CLI usage.

## Current state (as of 2026-09-24)

Built and working:
- Paragraph-based chunking, 500 chars with 50-char overlap (`src/chunking.py`). These settings haven't been tuned since the first commit.
- Embedding model registry (`src/embeddings.py`): `default` (Chroma's built-in MiniLM, ONNX), `mpnet`, `bge-small`. Each model gets its own Chroma collection (`rag_docs__<model>`) because vectors from different models aren't comparable.
- Retrieval strategies (`src/retrieval.py`): vector, BM25, hybrid (Reciprocal Rank Fusion), and cross-encoder reranking. Toggle with `--hybrid` / `--rerank`; choose the embedding model with `--embedding-model`.
- Generation in `src/query.py` (`synthesize_answer` + `ANSWER_PROMPT`): retrieved chunks are pasted into the prompt, and Claude answers from them only.
- Retrieval eval (`src/eval.py`): Hit@1, Hit@k, MRR.
- Generation eval (`src/eval_generation.py`): LLM-as-judge on faithfulness + relevance, with `claude-haiku-4-5-20251001` as the judge.

Corpora:
- `data/sample_docs/` + `data/eval_set.json`: 4 clean docs about RAG topics, 13 questions. Scores 100% (too easy to be informative).
- `data/current_events/` + `data/eval_set_current_events.json`: paraphrased from Wikipedia's Current Events Portal for Sept 15–19, 2026, which is after model training cutoffs, so answers must come from retrieval. 13 questions. Results: vector/hybrid/rerank all 100%; `mpnet` got 92% Hit@1, a useful example that bigger embedding models aren't automatically better. The generation eval scored 13/13 faithful and relevant.

Ideas discussed but not built yet:
- Chunk-size/overlap sweep script, re-running `eval.py` at each setting
- Query transformation (query rewriting, multi-query retrieval)
- Multi-turn conversation (folding prior turns into the retrieval query)
- Metadata filtering
- Messier real-world documents (PDFs) where topics overlap
- Production hardening: Qdrant/pgvector, FastAPI wrapper, caching, streaming

## Environment

- Python 3.10 venv at `rag/venv/` (gitignored). It didn't come over in the monorepo move, so recreate it first:
  `py -3.10 -m venv venv; .\venv\Scripts\python.exe -m pip install -r requirements.txt`
- `chroma_db/` is gitignored and also needs rebuilding: run `src/ingest.py` (and again with `--docs-dir data/current_events`, plus `--embedding-model mpnet` etc. for any model you want to compare).
- Run scripts with the venv's Python. The system `python` doesn't have `chromadb` (a common error).
- Generation and `eval_generation.py` need `ANTHROPIC_API_KEY`. Without it, `query.py` still prints the retrieved chunks.
- sentence-transformers and cross-encoder models download to the Hugging Face cache on first use.

## Conventions

- Every retrieval change should be measured with `eval.py` (and `eval_generation.py` if it affects answers). Report before/after numbers rather than eyeballing query output.
- When adding documents, add matching `{question, expected_source}` cases to the relevant eval set.
- Keep new corpora outside the model's training data where possible, and paraphrase rather than copy source text.
