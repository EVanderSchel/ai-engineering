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

Configuration for anything reachable from the internet:

- `RAG_API_KEY`: when set, `/ask` and `/ask/stream` require it in an `X-API-Key` header (401 otherwise). `/health` stays open and reports `auth_required`. Leave unset for local development.
- `RAG_WARM_UP` (default `true`): load the embedding model and indexes at startup, so the first request isn't slow. `RAG_WARM_UP_RERANKER=true` also preloads the reranker.

The Docker image builds the `data/current_events` search index into itself at build time, so a single container runs with no Chroma server. docker-compose sets `CHROMA_HOST` and uses its Chroma server instead.

Every request logs one JSON line (`src/telemetry.py`) with a request ID (also returned in the `X-Request-ID` header), per-stage timings (`retrieval_ms`, `generation_ms`, `ttft_ms` for streams, `total_ms`), token usage, dollar cost, and status (`ok`, `error`, or `cancelled` if a streaming client disconnects). The question text is deliberately not logged. Update `PRICES_PER_MTOK` when changing models.

### Tracing with Langfuse (optional)

With Langfuse keys configured, each request also becomes a trace in [Langfuse](https://langfuse.com) (`src/tracing.py`): a root span holding the question and answer, a `retrieval` step with the retrieved chunks, and an `answer` generation with the prompt, model, token usage, cost, and time to first token. The trace ID equals the log line's `request_id`, so any log line can be looked up in Langfuse.

Create `rag/.env` (gitignored) with keys from your Langfuse project's settings:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development
```

Use `https://us.cloud.langfuse.com` for a US-region project. Local runs load the file with python-dotenv; docker-compose passes it to the API container. Without it, tracing is simply off. Tests always disable tracing.

Two timings differ on purpose: the log's `ttft_ms` is measured from when the request arrived (what the user experiences, including retrieval), while Langfuse's time to first token is measured from when generation started (the model's own latency).

## Docker

`docker-compose.yml` runs two containers: the API, and a standalone Chroma server that replaces the local `chroma_db/` folder. Setting `CHROMA_HOST` is what switches the code from the folder to the server (`src/vector_store.py`). Embeddings are still computed in the API container; Chroma only stores vectors and searches them.

```
docker compose up -d --build
docker compose run --rm api python src/ingest.py --docs-dir data/current_events
```

Then call the API at http://localhost:8000 as above. Two named volumes persist data across restarts: `chroma-data` (the index) and `model-cache` (downloaded embedding/reranker models). `ANTHROPIC_API_KEY` is passed through from your shell environment. `docker compose down` stops everything; add `-v` to also delete the volumes.

## Deployment

The API runs on Azure Container Apps, with secrets in Key Vault and an API key required on `/ask`. See [deploy/README.md](deploy/README.md) for the architecture, setup scripts, and cold-start behavior.

## Prompt versions

The answer prompt lives in `prompts/answer/<version>.txt`, not in code (`src/prompts.py`). `prompts/answer/manifest.json` names the active version and records each version's SHA-256 fingerprint. Published versions are immutable: a test fails if a registered file changes, so every logged request (`"prompt": "answer/v1"`), Langfuse generation (`version`), and eval result always points at the exact text that produced it.

To try a prompt change:

1. Add `prompts/answer/v2.txt` and register it in the manifest (fingerprint: `python -c "import sys; sys.path.insert(0, 'src'); import prompts; print(prompts.fingerprint(prompts.read_template('answer', 'v2')))"`).
2. Compare it with the current version on the same cases: `python src/eval_generation.py --prompt-version v2 --eval-file data/eval_set_current_events.json data/eval_set_unanswerable.json`. Each full run is appended to `data/generation_eval_history.jsonl`; `python src/eval_generation.py --history` shows all runs side by side. Besides the judge's faithfulness and relevance, the eval reports citation rate (answers citing a retrieved `[file.txt]`, checked in code), false refusals (declining an answerable question), and correct refusals on `data/eval_set_unanswerable.json`, questions the corpus can't answer.
3. If it wins, set `"active": "v2"` in the manifest. `RAG_PROMPT_ANSWER=v2` overrides the active version for a single process.

## Eval regression gate

`src/eval_gate.py` runs the retrieval eval for every combination in `data/eval_baseline.json` (both corpora × vector / hybrid / rerank) and exits with an error if any Hit@1, Hit@k, or MRR score falls below its recorded baseline. Each corpus is ingested into a throwaway Chroma folder, so it never touches `chroma_db/`.

```
python src/eval_gate.py            # check against the baseline
python src/eval_gate.py --update   # accept current scores as the new baseline
```

GitHub Actions (`.github/workflows/rag-ci.yml`) runs the tests and this gate on every pull request that changes `rag/`, and posts the score table on the run's summary page. When a change genuinely improves scores, run `--update` and commit the new baseline in the same pull request, so the improvement becomes the new minimum.

## Performance

`python src/bench_retrieval.py` measures steady-state retrieval latency per strategy over the eval questions. Warm p50 on the development laptop (18 chunks):

| strategy | before (2026-09-28) | after |
|---|---|---|
| vector | 274 ms | 42 ms |
| hybrid | 277 ms | 47 ms |
| rerank | 457 ms | 233 ms |

The fix: Chroma's `DefaultEmbeddingFunction` reloads its ONNX model on every call, and Chroma bypasses any embedding function you pass for collections using that default. Retrieval now embeds the question itself with a model loaded once per process (`embeddings.CachedDefaultEmbeddingFunction`) and queries Chroma by vector; Chroma clients are also reused. Vectors are identical, so eval scores are unchanged. The first request after startup is still slow (~0.9 s) because models load lazily.

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
