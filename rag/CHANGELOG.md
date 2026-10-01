# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `rag-vX.Y.Z`.

## [Unreleased]

Add entries here as you make notable changes. Move them under a new version heading when you cut a release.

## [1.0.0] - 2026-10-01

First release as a production service: the from-scratch RAG pipeline, served as an API on Azure with automated quality, security, and deployment checks.

### Added

- **Retrieval:** chunking, ChromaDB vector search, hybrid search (BM25 + Reciprocal Rank Fusion), cross-encoder reranking; several corpora can be ingested side by side
- **API:** FastAPI `/ask` and streaming `/ask/stream` (Server-Sent Events), `/health` reporting the deployed commit
- **Answers:** Claude with versioned prompts (`prompts/answer/v1`–`v3`, active `v3`): citations, refusal when the context lacks the answer, and no stale facts presented as current
- **Evals:** retrieval eval gate (Hit@k, MRR) against a committed baseline on every PR; LLM-as-judge generation eval (faithfulness, relevance, citation rate, refusals, trap questions) with a recorded history per prompt version and model
- **Observability:** one JSON log line per request (timings, tokens, cost), Langfuse tracing, Azure alerts on 5xx errors and repeated restarts
- **Security:** API-key auth, per-caller rate limiting (429), secrets in Azure Key Vault via managed identity, dependency vulnerability audit on every PR, no raw exception text sent to clients
- **Deployment:** Docker image with the search index and models built in, published to GHCR after checks pass, smoke-tested before pushing, and deployed to Azure Container Apps after manual approval using OIDC (no stored credentials); HTTP health probes
- **Engineering:** 75+ tests with fake Claude and retriever, ruff lint and formatting, Python 3.14 with a uv lock file (exact versions and checksums), Dependabot version updates, actions and base images pinned to commits and digests

### Performance

- p50 retrieval latency cut from 274 ms to 42 ms by loading the embedding model once instead of on every query
- Container ready in ~6.5 s (pre-compiled bytecode); CI checks under a minute with a cached environment
