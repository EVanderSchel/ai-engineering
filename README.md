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
- [x] **Stage 8 — Environments & secrets**: staging vs. production, GitHub Environments, secret management
- [x] **Stage 9 — Release management**: versioning/tagging, changelogs

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

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every push and pull request to `main`, as six jobs:

- **lint** — `ruff check` (style/bug rules) and `ruff format --check` (formatting) against [pyproject.toml](pyproject.toml)'s config
- **test** — the pytest suite
- **build** — builds the [Dockerfile](Dockerfile) into an image on every push/PR, to catch build breakage early
- **publish** — only on a push to `main` (not on PRs), rebuilds the image and pushes it to GitHub Container Registry (see below)
- **deploy-staging** — only after `publish` succeeds, triggers a deploy to the staging Render service
- **deploy-production** — only after `deploy-staging` succeeds, and only after manual approval, triggers a deploy to the production Render service (see below)

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

## Environments & secrets

`deploy-staging` and `deploy-production` each declare a GitHub **Environment** (`environment: staging` / `environment: production`). Environments let you:

- scope secrets to a specific environment (so `staging` and `production` can hold *different values* for a secret with the *same name*, `RENDER_DEPLOY_HOOK_URL`, without the workflow needing to know which one it's using)
- attach **protection rules** — e.g. require a human to click "approve" before a job targeting that environment is allowed to run

This pipeline auto-deploys to staging on every push, but **pauses `deploy-production` for manual approval** — the same promotion pattern real teams use: ship to staging automatically, promote to production deliberately.

**One-time setup (manual — only you can do this):**

1. **Second Render service.** Repeat the Stage 7 Render steps to create a second Web Service — e.g. `ci-cd-workflow-staging` — also deploying `ghcr.io/evanderschel/ci-cd-workflow:latest` on port `8000`. Copy its Deploy Hook URL too. You now have two: one for staging, one for the original (production) service.

2. **Create the GitHub Environments.** In this repo: **Settings → Environments → New environment**. Create one named exactly `staging` and one named exactly `production` (names must match the `environment:` values in [ci.yml](.github/workflows/ci.yml)).

3. **Add a required reviewer to `production`.** Open the `production` environment → under **Deployment protection rules**, check **Required reviewers** and add yourself. Leave `staging` with no protection rules.

4. **Move the deploy hook secrets into their environments.** Inside each environment's page there's its own **Environment secrets** section:
   - In `staging` → add secret `RENDER_DEPLOY_HOOK_URL` = the **staging** service's deploy hook
   - In `production` → add secret `RENDER_DEPLOY_HOOK_URL` = the **original** service's deploy hook (the one from Stage 7)
   - Delete the old repository-level `RENDER_DEPLOY_HOOK_URL` secret (Settings → Secrets and variables → Actions) so there's only one source of truth per environment.

After this, pushing to `main` will deploy to staging immediately, then the Actions run will show `deploy-production` sitting in a **"Waiting for review"** state until you approve it from the run's page.

## Release management

Everything so far tracks `main` — `:latest` always means "whatever's on `main` right now." Releases are different: a **release** is a deliberately named, permanent snapshot you can always come back to, versioned with [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH` — bump `MAJOR` for breaking changes, `MINOR` for new backward-compatible features, `PATCH` for fixes).

[CHANGELOG.md](CHANGELOG.md) tracks notable changes under an `[Unreleased]` heading as you make them, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. When you're ready to cut a release, move those entries under a new version heading.

**To cut a release:**

```bash
git tag v0.2.0
git push origin v0.2.0
```

Pushing a tag matching `v*.*.*` triggers [.github/workflows/release.yml](.github/workflows/release.yml), which:

1. Builds and pushes the image tagged with the version only — `ghcr.io/evanderschel/ci-cd-workflow:0.2.0` — separate from `:latest`, which stays under the continuous `main` pipeline's control
2. Creates a **GitHub Release** for that tag, with release notes auto-generated from the commits/PRs merged since the last tag (GitHub does this natively — no changelog tooling required)

The tagged image is permanent: `ci-cd-workflow:0.2.0` will always mean the exact same code, even after `:latest` has moved on. This is what lets you roll a deployment back to a known-good version by pointing Render at a specific tag instead of `:latest`.

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
