# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).

## [Unreleased]

Add entries here as you make notable changes. Move them under a new version heading when you cut a release.

## [0.1.0] - 2026-09-24

Initial release: the CI/CD pipeline itself, built stage by stage.

### Added

- FastAPI app with `/health` and `/greet/{name}` endpoints
- pytest test suite
- GitHub Actions CI: lint (ruff) and test jobs
- Docker containerization with a build-validation job in CI
- Continuous Delivery: image published to GitHub Container Registry
- Continuous Deployment: automatic staging deploy, manually-approved production deploy via Render
- GitHub Environments for staging/production secret scoping and approval gating
