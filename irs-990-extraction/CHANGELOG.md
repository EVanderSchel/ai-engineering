# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `irs-990-extraction-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- Data source research: IRS e-file XML (answer keys) paired with IRS page images (input), verified to match on Part I
- Gold set: 60 Form 990 filings (20 per size band, 7 dev / 13 test each) with answer keys for 43 fields; `src/build_gold.py` rebuilds it reproducibly and downloads politely
