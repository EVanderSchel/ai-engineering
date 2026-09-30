# AI Engineering

Hands-on projects built while upskilling toward an AI Engineer role. Each folder is a self-contained project with its own README and dependencies.

| Project | Description | Key tools |
|---|---|---|
| [rag](rag/) | A retrieval-augmented generation service built from scratch and run in production: hybrid search with reranking, a streaming FastAPI API, retrieval and LLM-as-judge evals that gate every pull request, versioned prompts, per-request cost/latency logging and tracing, and approval-gated deploys to Azure | Python, ChromaDB, Claude API, FastAPI, Docker, GitHub Actions, Azure Container Apps, Key Vault, Langfuse |
| [ci-cd-workflow](ci-cd-workflow/) | A complete CI/CD pipeline built one stage at a time around a small FastAPI service: tests, lint gates, Docker, container registry, staged deployments with approval, and versioned releases | GitHub Actions, Docker, GHCR, Render, pytest, ruff |
| [skills-training](skills-training/) | Guided notebooks learning deep-learning frameworks side by side, from tensors and autograd to an MNIST classifier | TensorFlow, Keras, PyTorch, Jupyter |

## Highlights from `rag`

- **Quality is measured, not eyeballed.** Every pull request runs a retrieval eval (Hit@k, MRR) against a committed baseline and can't merge if a score drops. A separate LLM-as-judge eval scores faithfulness, relevance, citation rate, and refusals on questions the documents can't answer.
- **Prompts are versioned like code.** Each version is an immutable, fingerprinted file, and eval results record which version produced them. A revised prompt raised the source-citation rate from 0% to 100% with no loss in faithfulness.
- **Performance was profiled, not guessed.** p50 retrieval latency went from 274 ms to 42 ms after tracing the cost to the vector database reloading its embedding model on every query.
- **Deployed safely.** Merges to `main` publish an image and, after manual approval, deploy it to Azure Container Apps. GitHub logs in to Azure with OIDC (no stored credentials), secrets live in Key Vault, and the deploy only succeeds once `/health` reports the new commit.

## Layout

- Each project lives in its own top-level folder, with its own dependencies.
- GitHub Actions workflows live in [`.github/workflows/`](.github/workflows/), named `<project>-<workflow>.yml`. Pull requests start every workflow, and each one's first job (`<project>-changes`) skips the rest when that project wasn't touched, so the checks can be required without blocking unrelated changes. Pushes to `main` run only the affected project's workflow.
- Release tags are prefixed with the project name, e.g. `ci-cd-workflow-v1.0.0`.

## How changes land

`main` is protected: every change goes through a pull request, and merging requires each project's checks to pass (`rag-test`, `rag-eval-gate`, `rag-image`, `ci-cd-workflow-lint`, `ci-cd-workflow-test`, `ci-cd-workflow-build`, plus the `-changes` jobs). `rag-image` builds the Docker image and smoke-tests it (offline search, startup, auth), and `rag-publish` repeats that test on the exact image before pushing it. Deployments to production environments additionally wait for manual approval.

## Roadmap

1. ~~**Productionize RAG**~~: done (see above).
2. **Document extraction**: structured, validated JSON from insurance-style PDFs, with confidence scores, a human-review queue, and field-level accuracy evals.
3. **Tool-using agent**: an agent loop built by hand and then with a framework, tools exposed over MCP, trajectory evals, and prompt-injection testing.
4. **Fine-tune vs. prompt**: LoRA fine-tuning of a small open model, compared with a prompted frontier model and a classic baseline on accuracy, latency, and cost.
