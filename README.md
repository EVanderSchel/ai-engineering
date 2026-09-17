# RAG

A minimal, from-scratch Retrieval-Augmented Generation pipeline for learning how RAG and vector databases work under the hood.

## Pipeline

1. **Chunk** — `src/chunking.py` splits documents into overlapping text chunks.
2. **Embed + store** — `src/ingest.py` embeds each chunk (via Chroma's local default embedding model) and stores it in a persistent Chroma collection at `chroma_db/`.
3. **Retrieve** — `src/query.py` embeds a question, does a similarity search against the collection, and prints the top-k matching chunks.
4. **Generate** (optional) — if `ANTHROPIC_API_KEY` is set, the retrieved chunks are passed to Claude to synthesize a final answer.

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

## Next steps

- Swap the embedding model (e.g. a sentence-transformers or hosted model) and compare retrieval quality.
- Try a different vector DB (Qdrant, pgvector) behind the same interface.
- Build a small eval set of (question, expected source) pairs to measure retrieval accuracy directly, rather than eyeballing results.
