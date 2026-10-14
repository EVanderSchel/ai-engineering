# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `ntee-classification-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- Dataset builder (`src/build_dataset.py`) and NTEE taxonomy (`src/ntee.py`): IRS master-file labels joined with Form 990/990-EZ missions from one e-file batch; cleaning, deduplication (conflicting duplicates dropped), natural-mix sampling topped up to 100 per group, stratified 70/15/15 split
- `data/dataset.csv` (12,078 missions, 25 groups) with `data/stats.json`, integrity tests, and a 50-example label-quality check graded by the user (74% agree, 20% ambiguous, 6% disagree)
- Eval harness (`src/evaluate.py`): accuracy, top-3, macro-F1, broad-category accuracy, per-group results, confusions, latency and cost per 1,000 predictions; every run saved with its predictions; `src/data.py` holds out the test split
- Classic baseline (`src/baseline.py`): TF-IDF (word + character n-grams) and logistic regression, tuned on dev: 53.7% accuracy, 75.4% top-3, 0.461 macro-F1, 1.8 ms per mission
