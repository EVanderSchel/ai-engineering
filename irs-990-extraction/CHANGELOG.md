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

- `extract.py --input image-<pixels>|pdf` chooses how page 1 is sent; runs and the results history record the input and whether caching was on
- Prompt caching of the prompt and schema, on by default: $0.0175 per filing instead of $0.027 on the dev set
- A run that hits an API error it can't retry stops and is marked incomplete; `evaluate.py` doesn't score incomplete runs

- Part VII Section A in the answer keys: 703 rows across the 60 filings (name, title, hours, position checkboxes, three compensation columns) plus the line 1d totals and line 2 count

- Part VII page finder (`src/find_pages.py`): header sheets of every page, Claude names the Part VII pages; hand-checked page labels for the 21 dev returns (`data/gold/part_vii_pages.csv`). First run: 21 of 21 returns exactly right, $0.015 per return

- Part VII row extraction (`src/part_vii.py`): found pages in, every row and the totals out, checked against line 1d with one retry; list scoring that matches people by name and counts missed and invented people (`src/score_part_vii.py`). First run on dev: 320 of 320 people, 99.5% of their fields, $0.038 per return

- Simulated scans (`src/scans.py`, Pillow): light, medium, and heavy scanned copies of the gold-set returns, and `--scan` on every stage. Light matches clean pages, medium costs about two points on Part I, heavy breaks Part I and Part VII (and runs some requests out of output tokens); the page finder stayed 21/21 at every level

- Claude's own doubts (`extract.py --confidence`, prompt `extract/v3`, `unsure_fields` in a separate schema) and their scoring against real errors (`unsure_flagged`, `unsure_caught` in the history). They catch 9% of errors on clean pages and 43% on heavy scans: a weak signal compared with run-to-run disagreement

- Review rules (`src/review.py`): document signals (no answer, checks, thinking length) and field signals (doubts, a second run's disagreement), scored as errors caught vs. fields reviewed. Checks + thinking > 2,000 tokens + second run: 100% after reviewing 6% of fields on clean pages, 99.8% after 34% on medium scans

- Review queue (`src/review_queue.py`): a local HTML page of flagged fields beside page 1, with a contents list, progress, and a per-field "Looks right" so only checked fields count as reviewed; a corrections download that lists unreviewed fields, `--apply` to write a reviewed run, and `--simulate` for a perfect reviewer

- Batch extraction (`src/batch.py`): Part I for many filings through the Message Batches API, resumable and idempotent (`--no-wait`, `--resume`), scored like any run. 21 dev filings: 99.0%, $0.0107 per filing (39% less than one at a time; caching is weaker in a batch), 3 min 16 s
- Sturdier batches: failed or expired requests resent (up to 3 times, never invalid ones), one retry round for answers that break the arithmetic, batches split under the size limit, state saved at every step. `--warm` (filling the cache first) was tried and made caching worse
- Parallel extraction (`src/parallel.py`): a thread pool of workers running `extract()`, the first filing alone to fill the cache, more SDK retries for rate limits. 21 dev filings in 39 s at $0.0178 each; `extract.extract_filing()` shared by both

### Fixed

- Answer keys for organizations whose name is printed on two lines had only the first line (11 of 60); the name now joins `BusinessNameLine1Txt` and `BusinessNameLine2Txt`
