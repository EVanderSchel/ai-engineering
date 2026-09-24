# AI Engineering

Hands-on projects built while upskilling toward an AI Engineer role. Each folder is a self-contained project with its own README and dependencies.

| Project | Description | Key tools |
|---|---|---|
| [rag](rag/) | Retrieval-Augmented Generation pipeline built from scratch: chunking, embeddings, vector search, hybrid BM25 search, cross-encoder reranking, and retrieval + LLM-as-judge generation evals | Python, ChromaDB, sentence-transformers, rank_bm25, Claude API |
| [ci-cd-workflow](ci-cd-workflow/) | A complete CI/CD pipeline built one stage at a time around a small FastAPI service: tests, lint gates, Docker, container registry, staged deployments with approval, and versioned releases | GitHub Actions, Docker, GHCR, Render, pytest, ruff |
| [skills-training](skills-training/) | Guided notebooks learning deep-learning frameworks side by side, from tensors and autograd to an MNIST classifier | TensorFlow, Keras, PyTorch, Jupyter |

## Layout

- Each project lives in its own top-level folder.
- GitHub Actions workflows live in [`.github/workflows/`](.github/workflows/), named `<project>-<workflow>.yml`, and only run when their project's files change.
- Release tags are prefixed with the project name, e.g. `ci-cd-workflow-v1.0.0`.
