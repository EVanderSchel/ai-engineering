# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `ntee-classification-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- Dataset builder (`src/build_dataset.py`) and NTEE taxonomy (`src/ntee.py`): IRS master-file labels joined with Form 990/990-EZ missions from one e-file batch; cleaning, deduplication (conflicting duplicates dropped), natural-mix sampling topped up to 100 per group, stratified 70/15/15 split
- `data/dataset.csv` (12,078 missions, 25 groups) with `data/stats.json`, integrity tests, and a 50-example label-quality check graded by the user (74% agree, 20% ambiguous, 6% disagree)
