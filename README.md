# CI/CD Workflow Demo

A small FastAPI service used as the vehicle for building up a full CI/CD pipeline, one stage at a time, on GitHub Actions.

## Roadmap

- [x] **Stage 1 — App scaffold**: minimal FastAPI app, runnable locally
- [x] **Stage 2 — Automated tests**: pytest unit tests for the API
- [x] **Stage 3 — Continuous Integration**: GitHub Actions workflow running tests on push/PR
- [x] **Stage 4 — Code quality gates**: linting (ruff) and formatting checks in CI
- [x] **Stage 5 — Containerization**: Dockerfile + build step in CI
- [x] **Stage 6 — Continuous Delivery**: push built image to a registry
- [x] **Stage 7 — Continuous Deployment**: deploy to an environment automatically
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

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every push and pull request to `main`, as five jobs:

- **lint** — `ruff check` (style/bug rules) and `ruff format --check` (formatting) against [pyproject.toml](pyproject.toml)'s config
- **test** — the pytest suite
- **build** — builds the [Dockerfile](Dockerfile) into an image on every push/PR, to catch build breakage early
- **publish** — only on a push to `main` (not on PRs), rebuilds the image and pushes it to GitHub Container Registry (see below)
- **deploy** — only after `publish` succeeds, triggers a live deployment on Render (see below)

Check the **Actions** tab on GitHub after pushing to see them run.

## Continuous Delivery

The **publish** job in [ci.yml](.github/workflows/ci.yml) runs after `lint`, `test`, and `build` all succeed, and only on pushes to `main` — PRs never publish an image. It authenticates to `ghcr.io` (GitHub Container Registry) using the automatically-provided `GITHUB_TOKEN` (no extra secrets or accounts needed), then pushes the image tagged two ways:

- `latest` — always points at the most recent successful build on `main`
- `<commit-sha>` — an immutable tag for that exact commit, so you can always trace a running container back to the exact code that produced it

Find the published image under the repo's **Packages** tab on GitHub, or pull it directly:

```bash
docker pull ghcr.io/evanderschel/ci-cd-workflow:latest
```

Note: GHCR packages published via `GITHUB_TOKEN` default to **private**. To pull anonymously (e.g. from another machine), open the package settings on GitHub and change its visibility to public.

## Continuous Deployment

The **deploy** job runs after `publish` succeeds and sends a POST request to a Render "Deploy Hook" URL, which tells Render to pull the latest image and restart the live service. Render itself is never given build/test logic — it just reacts to a signal from our pipeline, after our own gates have already passed.

**One-time setup on Render (manual — only you can do this):**

1. Make the GHCR package public (see the note above) — Render needs to pull it without registry credentials.
2. Create a free account at [render.com](https://render.com).
3. **New → Web Service → Deploy an existing image**, and enter: `ghcr.io/evanderschel/ci-cd-workflow:latest`
4. Set **Port** to `8000` (matches the Dockerfile's `EXPOSE 8000`).
5. Create the service, then go to its **Settings** tab and copy the **Deploy Hook** URL.
6. In this GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**, name it `RENDER_DEPLOY_HOOK_URL`, and paste the URL.

Once that's set up, every push to `main` that passes lint/test/build/publish will automatically redeploy the live service.

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
