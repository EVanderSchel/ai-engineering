# CI/CD Workflow Demo

A small FastAPI service used as the vehicle for building up a full CI/CD pipeline, one stage at a time, on GitHub Actions.

## Roadmap

- [x] **Stage 1 — App scaffold**: minimal FastAPI app, runnable locally
- [x] **Stage 2 — Automated tests**: pytest unit tests for the API
- [x] **Stage 3 — Continuous Integration**: GitHub Actions workflow running tests on push/PR
- [x] **Stage 4 — Code quality gates**: linting (ruff) and formatting checks in CI
- [x] **Stage 5 — Containerization**: Dockerfile + build step in CI
- [ ] **Stage 6 — Continuous Delivery**: push built image to a registry
- [ ] **Stage 7 — Continuous Deployment**: deploy to an environment automatically
- [ ] **Stage 8 — Environments & secrets**: staging vs. production, GitHub Environments, secret management
- [ ] **Stage 9 — Release management**: versioning/tagging, changelogs

## Local development

```bash
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then visit http://127.0.0.1:8000/health

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Continuous Integration

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every push and pull request to `main`, as two parallel jobs:

- **lint** — `ruff check` (style/bug rules) and `ruff format --check` (formatting) against [pyproject.toml](pyproject.toml)'s config
- **test** — the pytest suite
- **build** — builds the [Dockerfile](Dockerfile) into an image (not pushed anywhere yet — that's Stage 6). This proves every commit produces a deployable artifact, not just "passing tests."

Check the **Actions** tab on GitHub after pushing to see them run.

## Linting & formatting locally

```bash
ruff check .          # lint
ruff format .          # auto-format
ruff format --check .  # verify formatting without changing files (what CI runs)
```

## Building the container locally

```bash
docker build -t ci-cd-workflow-demo .
docker run -p 8000:8000 ci-cd-workflow-demo
```

Then visit http://127.0.0.1:8000/health
