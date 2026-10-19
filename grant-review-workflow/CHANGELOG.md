# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `grant-review-workflow-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- Design doc (`docs/design.md`): the fictional fund's rules, the workflow's steps and state, principles, metrics, and the gold set plan
- The fund's eligibility rules as code (`src/fund_rules.py`), tested at every boundary
- The 60 real Form 990 answer keys, copied from Project 2 (`data/filings/`)
- Gold set of 40 application packets (`data/gold/`): forms, specs and computed labels for all 40; narratives for the 15 dev packets, checked for their stated figures and approved
