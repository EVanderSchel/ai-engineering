# RAG

A minimal, from-scratch Retrieval-Augmented Generation pipeline for learning how RAG and vector databases work under the hood.

## Pipeline

1. **Chunk** — `src/chunking.py` splits documents into overlapping text chunks.
2. **Embed + store** — `src/ingest.py` embeds each chunk (via Chroma's local default embedding model) and stores it in a persistent Chroma collection at `chroma_db/`.
3. **Retrieve** — `src/query.py` embeds a question, does a similarity search against the collection, and prints the top-k matching chunks.
4. **Generate** (optional) — if `ANTHROPIC_API_KEY` is set, the retrieved chunks are passed to Claude to synthesize a final answer.
5. **Evaluate** — `src/eval.py` runs a labeled set of (question, expected source) pairs through retrieval and reports Hit@1, Hit@k, and MRR, so you can measure retrieval quality directly instead of eyeballing results.

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

## Next steps

- Swap the embedding model (e.g. a sentence-transformers or hosted model) and compare Hit@1/MRR before and after.
- Try a different vector DB (Qdrant, pgvector) behind the same interface.
- Point `--docs-dir` at your own, messier documents and see how retrieval quality holds up against the eval set.
