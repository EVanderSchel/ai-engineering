# CI/CD Workflow Demo

A small FastAPI service used as the vehicle for building up a full CI/CD pipeline, one stage at a time, on GitHub Actions.

## Roadmap

- [x] **Stage 1 — App scaffold**: minimal FastAPI app, runnable locally
- [x] **Stage 2 — Automated tests**: pytest unit tests for the API
- [ ] **Stage 3 — Continuous Integration**: GitHub Actions workflow running lint + tests on push/PR
- [ ] **Stage 4 — Code quality gates**: linting (ruff) and formatting checks in CI
- [ ] **Stage 5 — Containerization**: Dockerfile + build step in CI
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
