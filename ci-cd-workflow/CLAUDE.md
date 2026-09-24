# ci-cd-workflow

Learning project: a deliberately tiny FastAPI app (`/health`, `/greet/{name}`) used as the vehicle for building a full CI/CD pipeline one stage at a time. The pipeline is the thing being learned, not the app. README.md documents every stage and the manual setup steps.

## Roadmap status

All 9 stages are complete: 1 app, 2 pytest tests, 3 GitHub Actions CI, 4 ruff lint/format gates, 5 Docker, 6 publish to GHCR, 7 deploy to Render via deploy hook, 8 staging/production GitHub Environments with a required reviewer on production, 9 release workflow with semver-tagged images + GitHub Releases.

Possible next steps that were suggested: test coverage, rollback strategy, monitoring.

## Pipeline layout after the monorepo move (important)

The workflows now live at the repo root, not in this folder:
- `.github/workflows/ci-cd-workflow-ci.yml`: lint → test → build → publish → deploy-staging → deploy-production. It triggers only on changes under `ci-cd-workflow/**`. `working-directory: ci-cd-workflow` is set on the `lint` and `test` jobs only, because the deploy jobs don't check out code. Docker `context` is `ci-cd-workflow`.
- `.github/workflows/ci-cd-workflow-release.yml`: triggered by tags matching `ci-cd-workflow-v*.*.*` (not `v*.*.*` anymore). Example: `git tag ci-cd-workflow-v0.2.0; git push origin ci-cd-workflow-v0.2.0`.
- Image: `ghcr.io/evanderschel/ci-cd-workflow` (`:latest`, `:<sha>`, `:<version>`). This is the same name as before the move, so the Render services didn't need changing. The package was created by the old `CI-CD-Workflow` repo, so the `ai-engineering` repo must have **Write** access under the package's "Manage Actions access" settings, or pushes fail with `permission_denied: write_package`.
- `RENDER_DEPLOY_HOOK_URL` is an **environment** secret in both `staging` and `production` (different values: `ci-cd-workflow-staging` and the production Render service).

## Environment

- Python venv at `ci-cd-workflow/.venv/` (gitignored, not carried over by the move). Recreate with:
  `py -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`
- CI uses Python 3.11 on ubuntu-latest; the Dockerfile uses `python:3.11-slim` and runs as a non-root user on port 8000.
- Docker Desktop (WSL2 backend) is available locally for `docker build` / `docker run` checks.

## Gotchas already hit

- Verify with plain `pytest`, not `python -m pytest`. `python -m` adds the cwd to `sys.path` and hid a CI-only import failure. The fix was `pythonpath = ["."]` in `pyproject.toml`.
- GitHub Actions can't be run locally. Validate YAML by parsing it, and be explicit that the real test is the Actions run after a push.
