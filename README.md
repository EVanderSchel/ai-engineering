# AI Engineering

Hands-on projects built while upskilling toward an AI Engineer role. Each folder is a self-contained project with its own README and dependencies.

| Project | Description | Key tools |
|---|---|---|
| [rag](rag/) | A retrieval-augmented generation service built from scratch and run in production (released as `rag-v1.0.0`): hybrid search with reranking, a streaming FastAPI API with API-key auth and rate limiting, retrieval and LLM-as-judge evals that gate every pull request plus a weekly generation eval, versioned prompts, per-request cost/latency logging and tracing, health probes and alerts, and approval-gated deploys to Azure | Python 3.14, uv, ChromaDB, Claude API, FastAPI, Docker, GitHub Actions, Azure Container Apps, Key Vault, Langfuse |
| [irs-990-extraction](irs-990-extraction/) | *In progress.* Structured data extracted from charities' IRS Form 990 filings (page images only) with Claude's vision input, measured field by field against the IRS's own e-file XML, with confidence scores and a human-review queue | Python 3.14, uv, Claude API (vision, structured outputs), Pydantic |
| [ci-cd-workflow](ci-cd-workflow/) | A complete CI/CD pipeline built one stage at a time around a small FastAPI service: tests, lint gates, Docker, container registry, staged deployments with approval, and versioned releases | GitHub Actions, Docker, GHCR, Render, pytest, ruff |
| [skills-training](skills-training/) | Guided notebooks learning deep-learning frameworks side by side, from tensors and autograd to an MNIST classifier | TensorFlow, Keras, PyTorch, Jupyter |

## Highlights from `rag`

- **Quality is measured, not eyeballed.** Every pull request runs a retrieval eval (Hit@k, MRR) against a committed baseline and can't merge if a score drops. A separate LLM-as-judge eval, run weekly, scores faithfulness, relevance, citation rate, refusals on questions the documents can't answer, and "trap" questions whose context holds a misleading look-alike fact.
- **Prompts are versioned like code.** Each version is an immutable, fingerprinted file, and eval results record which version produced them. Revisions raised the source-citation rate from 0% to 100% and trap resistance from 83% to 100%, with no loss in faithfulness.
- **Performance was profiled, not guessed.** p50 retrieval latency went from 274 ms to 42 ms after tracing the cost to the vector database reloading its embedding model on every query.
- **Deployed safely.** Merges to `main` publish a smoke-tested image and, after manual approval, deploy it to Azure Container Apps. GitHub logs in to Azure with OIDC (no stored credentials), secrets live in Key Vault, and the deploy only succeeds once `/health` reports the new commit. Health probes restart a hung container, and alerts email on server errors or repeated restarts.

## Layout

- Each project lives in its own top-level folder, with its own dependencies.
- GitHub Actions workflows live in [`.github/workflows/`](.github/workflows/), named `<project>-<workflow>.yml`. Pull requests start every project's CI workflow, and each one's first job (`<project>-changes`) skips the rest when that project wasn't touched, so the checks can be required without blocking unrelated changes. Pushes to `main` run only the affected project's workflow.
- Release tags are prefixed with the project name: `<project>-vX.Y.Z`, e.g. `rag-v1.0.0`.

## How changes land

`main` is protected: every change goes through a pull request, and merging requires each project's checks to pass:

- **rag:** `rag-test`, `rag-eval-gate`, `rag-lint` (ruff), `rag-audit` (known vulnerabilities in locked dependencies), and `rag-image`, which builds the Docker image and smoke-tests it (offline search, startup, auth); `rag-publish` repeats that test on the exact image before pushing it.
- **irs-990-extraction:** `irs-990-extraction-test`, `-lint`, `-audit`.
- **ci-cd-workflow:** `ci-cd-workflow-lint`, `-test`, `-build`.
- Plus each project's `-changes` job, and **CodeQL**: a pull request that introduces a new high or critical security finding can't merge.

Deployments to production environments additionally wait for manual approval.

## Security and maintenance

- **Pinned dependencies:** Python packages are locked to exact versions with checksums (`uv.lock`), GitHub Actions are pinned to commit SHAs, and Docker base images to digests.
- **Dependabot** opens weekly update PRs for Python packages, Actions, and base images, plus immediate PRs for security fixes. Every update runs the full set of required checks.
- **Secret scanning with push protection** blocks pushes that contain credentials. Every workflow's token is read-only unless a job explicitly needs more.

## Roadmap

1. ~~**Productionize RAG**~~: done (`rag-v1.0.0`, see above).
2. **Document extraction** *(in progress)*: structured, validated data from IRS Form 990 filings, with confidence scores, a human-review queue, and field-level accuracy evals against the IRS's e-file data.
3. **Tool-using agent**: an agent loop built by hand and then with a framework, tools exposed over MCP, trajectory evals, and prompt-injection testing.
4. **Fine-tune vs. prompt**: LoRA fine-tuning of a small open model, compared with a prompted frontier model and a classic baseline on accuracy, latency, and cost.

## License

MIT. See [LICENSE](LICENSE).
