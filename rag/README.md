# RAG

A minimal, from-scratch Retrieval-Augmented Generation pipeline for learning how RAG and vector databases work under the hood.

## Pipeline

1. **Chunk** — `src/chunking.py` splits documents into overlapping text chunks.
2. **Embed + store** — `src/ingest.py` embeds each chunk with a chosen embedding model (`src/embeddings.py`) and stores it in a Chroma collection at `chroma_db/`. Each embedding model gets its own collection (`rag_docs__<model>`), since vectors from different models aren't comparable.
3. **Retrieve** (`src/retrieval.py`) — three interchangeable strategies:
   - **Vector search** — embed the question, do a similarity search against the collection.
   - **Hybrid search** (`--hybrid`) — also ranks chunks with BM25 keyword search, then fuses the two rankings with Reciprocal Rank Fusion. Catches exact terms (names, numbers, acronyms) that don't always embed distinctively.
   - **Reranking** (`--rerank`) — pulls a larger candidate set (vector or hybrid), then re-scores each candidate against the question with a cross-encoder model, which reads the pair jointly instead of comparing precomputed vectors. More accurate, too slow to run over a whole corpus, so it only re-orders a short candidate list.
4. **Generate** (optional) — if `ANTHROPIC_API_KEY` is set, the retrieved chunks are passed to Claude to synthesize a final answer.
5. **Evaluate retrieval** — `src/eval.py` runs a labeled set of (question, expected source) pairs through retrieval and reports Hit@1, Hit@k, and MRR, so you can measure the effect of any of the above choices directly instead of eyeballing results.
6. **Evaluate generation** — `src/eval_generation.py` runs the same questions all the way through to a generated answer, then has a second Claude call (an LLM judge) score each answer for faithfulness (is every claim actually supported by the retrieved context?) and relevance (does it address the question?). Retrieval eval alone can't catch a system that retrieves the right chunk but then ignores it, or one that pads a correct answer with hallucinated detail.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

Ingest the sample corpus (short docs about chunking, embeddings, vector databases, and RAG itself):

```
python src\ingest.py
```

Query it:

```
python src\query.py "What is the difference between fixed-size and semantic chunking?"
```

To point at your own documents instead of the sample corpus, drop `.txt` files into a folder and run:

```
python src\ingest.py --docs-dir path\to\your\docs
```

Run the retrieval eval:

```
python src\eval.py
```

Add your own cases to `data/eval_set.json` (a list of `{"question": ..., "expected_source": ...}` objects) as you add documents.

## Generation eval

Requires `ANTHROPIC_API_KEY` (one call generates each answer, a second, cheaper call on `claude-haiku-4-5` judges it):

```
python src\eval_generation.py --eval-file data\eval_set_current_events.json
```

Add `--limit N` while experimenting, since each case costs two API calls. Accepts the same `--embedding-model`, `--hybrid`, `--rerank` flags as `eval.py`, so you can see whether a retrieval change actually improved the final answers, not just which chunks got retrieved.

## API

`src/api.py` serves the pipeline over HTTP with FastAPI:

```
python -m uvicorn api:app --app-dir src --reload
```

- `GET /health`: liveness check; also reports whether an API key is configured.
- `POST /ask`: returns the full answer and its sources as one JSON response.
- `POST /ask/stream`: streams the answer as Server-Sent Events: a `sources` event, then `token` events as Claude writes, then `done` (stop reason and token usage), or `error` if generation fails partway.

Interactive docs are at http://localhost:8000/docs.

## Docker

`docker-compose.yml` runs two containers: the API, and a standalone Chroma server that replaces the local `chroma_db/` folder. Setting `CHROMA_HOST` is what switches the code from the folder to the server (`src/vector_store.py`). Embeddings are still computed in the API container; Chroma only stores vectors and searches them.

```
docker compose up -d --build
docker compose run --rm api python src/ingest.py --docs-dir data/current_events
```

Then call the API at http://localhost:8000 as above. Two named volumes persist data across restarts: `chroma-data` (the index) and `model-cache` (downloaded embedding/reranker models). `ANTHROPIC_API_KEY` is passed through from your shell environment. `docker compose down` stops everything; add `-v` to also delete the volumes.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

The API tests replace Claude and retrieval with fakes (`tests/conftest.py`), so they need no API key or Chroma index, cost nothing, and run in well under a second.

## Retrieval strategies

All three of `ingest.py`, `query.py`, and `eval.py` accept `--embedding-model` (`default`, `mpnet`, or `bge-small` — see `src/embeddings.py`). `query.py` and `eval.py` also accept `--hybrid` and `--rerank`. Compare strategies against the eval set:

```
python src\eval.py --eval-file data\eval_set_current_events.json
python src\eval.py --eval-file data\eval_set_current_events.json --hybrid
python src\eval.py --eval-file data\eval_set_current_events.json --rerank
python src\ingest.py --docs-dir data\current_events --embedding-model mpnet
python src\eval.py --eval-file data\eval_set_current_events.json --embedding-model mpnet
```

Note that ingesting under the same `--embedding-model` always replaces that model's collection — collections are keyed by embedding model, not by corpus. Re-ingest before switching between `data/sample_docs` and `data/current_events` if you've been using a different corpus.

## Corpora

- `data/sample_docs/` — short docs explaining RAG concepts themselves (chunking, embeddings, vector databases, RAG). Paired with `data/eval_set.json`.
- `data/current_events/` — a paraphrased digest of Wikipedia's Current Events Portal (September 2026), grouped by theme (armed conflicts, politics, business, science/disasters). Content published after the model's training cutoff, so any generated answer has to come from retrieval rather than memory. Paired with `data/eval_set_current_events.json`.

Switch corpora with `--docs-dir` and `--eval-file`:

```
python src\ingest.py --docs-dir data\current_events
python src\eval.py --eval-file data\eval_set_current_events.json
```

## Next steps

- Try a different vector DB (Qdrant, pgvector) behind the same interface.
- Point `--docs-dir` at your own, messier documents and see how retrieval quality holds up, and whether hybrid search or reranking helps more than they did on the clean sample corpora.
- Add a hosted embedding model (OpenAI, Cohere) to `src/embeddings.py` and compare cost/latency against the local options.
