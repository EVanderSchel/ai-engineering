# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions. This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`). Releases are tagged `irs-990-extraction-vX.Y.Z`.

## [Unreleased]

### Added

- Project scaffolding: uv project on Python 3.14, ruff, pytest, CI workflow with change detection, lint, test, and vulnerability-audit checks
- Data source research: IRS e-file XML (answer keys) paired with IRS page images (input), verified to match on Part I
- Gold set: 60 Form 990 filings (20 per size band, 7 dev / 13 test each) with answer keys for 42 fields; `src/build_gold.py` rebuilds it reproducibly and downloads politely
- Baseline extraction (`src/extract.py`): page 1 as an image, Claude structured outputs into a schema generated from `src/fields.py`, Part I arithmetic checks with one retry, versioned prompts, cost per filing from token usage
- Evaluation (`src/evaluate.py`): per-field accuracy with documented normalization (case and whitespace in names and missions, the dash in EINs; blank and 0 stay different), errors classified by type, and a committed results history (`results/history.csv`); saved runs can be rescored without API calls
- `build_gold.py --refresh-keys` rebuilds answer keys from the downloaded XML
- `extract.py --split test` requires `--final`, keeping the test split held out

### Changed

- Prompt `extract/v2` is active: the EIN is copied as printed and its dash removed in code, each cell is read on its own (blank next to 0 stays blank), and two-line names are read in full. On 2 runs each over the 21 dev filings: 99.7% of fields correct vs. 98.6% for v1, no EIN or name errors (v1: 8)

### Fixed

- Answer keys for organizations whose name is printed on two lines had only the first line (11 of 60); the name now joins `BusinessNameLine1Txt` and `BusinessNameLine2Txt`
