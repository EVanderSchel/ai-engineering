# ai-engineering

Monorepo of hands-on projects for upskilling toward an AI Engineer role. Each top-level folder is an independent project with its own dependencies, README, and CLAUDE.md:

| Folder | What it is |
|---|---|
| `skills-training/` | Jupyter notebooks learning ML frameworks (TensorFlow, PyTorch) |
| `rag/` | From-scratch RAG pipeline: chunking, embeddings, Chroma, hybrid search, reranking, retrieval + generation evals |
| `ci-cd-workflow/` | Small FastAPI app used to learn a full CI/CD pipeline (GitHub Actions → GHCR → Render) |

GitHub: https://github.com/EVanderSchel/ai-engineering. Each project was originally its own repo (`Skills_Training`, `RAG`, `CI-CD-Workflow`) and was merged in with `git subtree add` on 2026-09-24, so full history is preserved. The old local copies under OneDrive still exist but are no longer the source of truth; always work in `C:\dev\ai-engineering`.

## Repo conventions

- **New projects** go in a new top-level folder (kebab-case) with their own README and dependency files, and get a row in the root README.md table. Don't share virtualenvs or requirements files across projects.
- **GitHub Actions** must live in the root `.github/workflows/`, since GitHub ignores workflows in subfolders. Name them `<project>-<name>.yml`, filter triggers with `paths: ["<project>/**"]`, and set `working-directory` only on jobs that check out code.
- **Release tags** are prefixed per project: `<project>-vX.Y.Z` (e.g. `ci-cd-workflow-v1.0.0`).
- **Secrets** never go in the repo or in chat. `.env` files are gitignored.

## Working with the user

- The user is learning, so the goal is understanding, not just working code. Build things step by step, one concept at a time, and check in before moving to the next stage rather than scaffolding everything at once.
- When explaining, the user often asks for both plain-language and technical explanations. Analogies help.
- Verify by actually running things (tests, notebooks via nbconvert, scripts) and say plainly what was and wasn't verified.
- The user often commits themselves. Ask before committing or pushing.

## Environment

- Windows 10, PowerShell. Git 2.55. The GitHub CLI (`gh`) may not be installed or on PATH.
- `ANTHROPIC_API_KEY` is set as a Windows **User** environment variable (workspace-scoped key). Processes started before it was set don't see it; a full restart of the host app is needed after changing it.
- Docker Desktop is installed with the WSL2 backend.
- The user's resume (`C:\Users\Administrator\OneDrive\Documents\Job Search\VanderSchel_Resume_2026.docx`) has a Projects section describing the RAG and CI/CD projects. Its links point to the original repos (`github.com/EVanderSchel/RAG`, `github.com/EVanderSchel/CI-CD-Workflow`).
