# IRS Form 990 extraction

Structured, validated data from charities' **IRS Form 990** filings, extracted with Claude from the IRS's page images and measured field by field against the IRS's own e-file data.

**Why it matters:** the Form 990 is the public record of how a tax-exempt organization raises and spends money: revenue, expenses, program spending, executive pay. Donors, journalists, and watchdogs rely on it, but much of it is published only as page images. Turning those images into reliable, structured data is the problem this project works on.

## Data sources

| What | Where | Role |
|---|---|---|
| Filing index (EIN, tax period, form type, object ID) | [IRS Form 990 Series Downloads](https://www.irs.gov/charities-non-profits/form-990-series-downloads), `index_<year>.csv` | Choosing filings |
| E-filed returns as XML | Same page: monthly ZIPs (`<year>_TEOS_XML_<month><letter>.zip`); single files are read from inside the ZIP over HTTP, without downloading the whole archive | **Answer keys** (ground truth) |
| Page images of the same returns | IRS public copies of returns, `https://apps.irs.gov/pub/epostcard/cor/<file>.pdf` | **Input** to the extractor |
| Each filing's PDF filename | [ProPublica Nonprofit Explorer API](https://projects.propublica.org/nonprofits/api) | Locating the PDF for a filing |

IRS data is public domain. Thanks to ProPublica for the Nonprofit Explorer API; requests are kept slow and few.

**What the data looks like (checked 2026-10-01):**

- The IRS PDFs have **no text layer**: every page is an image, even for e-filed returns. Extraction therefore reads the images (Claude's vision input), not extracted text.
- For e-filed returns, the page images are clean IRS renderings of the filed data, and the XML holds exactly the same values. A spot check of Part I (revenue, expenses, assets, employees, mission) matched on every field.
- Older paper-filed returns are genuine scans with no XML, so they can only be labeled by hand. A few will form a "hard" test set.
- Scope starts with **Form 990** (not 990-EZ or 990-PF, which are different forms), **Part I (Summary)** on page 1, then officer compensation (Part VII).

## Plan

| Step | What |
|---|---|
| 0 | Project scaffolding: uv, ruff, pytest, CI with required checks *(done)* |
| 1 | Gold set: pick filings from the IRS index, download their XML and page images, build answer-key JSON *(done)* |
| 2 | Schema and baseline: Pydantic model of Part I, Claude structured outputs from page images, validation and retry *(done)* |
| 3 | Eval harness: per-field accuracy with normalization, results history by prompt version and model |
| 4 | Vision input choices: page selection, resolution, PDF vs. image input, cost per document |
| 5 | Harder documents: multi-page sections (Part VII), scanned paper returns |
| 6 | Confidence and human review: per-field confidence, thresholds tuned on the eval set, review queue |
| 7 | Batch processing: Message Batches API, concurrency, idempotent reprocessing |
| 8 | Model comparison: Sonnet vs. Haiku on accuracy, cost, and latency, over repeated runs |
| 9 | Optional: an `/extract` API reusing the rag project's production setup |

## Gold set

60 real Form 990 filings from the IRS's 2024 index, 20 per size band by current-year total revenue (small < $500k, medium $500k-$5M, large >= $5M), split 7 **dev** / 13 **test** per band. Dev is for developing prompts; **test is held out** and only used for final scores, so improvements can't be tuned to the questions that grade them.

| File | What |
|---|---|
| `data/gold/<object_id>.json` | Answer key: the 42 fields in `src/fields.py` (filer identity + Part I), read from the e-file XML |
| `data/gold/manifest.csv` | One row per filing: EIN, name, tax period, band, split, DLN, IRS PDF filename |
| `data/gold/skipped.csv` | Filings tried but left out, and why (15, all with no published PDF yet) |

Rebuild (or fetch the PDFs on a new machine) with `python src/build_gold.py`; the fixed seed picks the same filings, and files already in `data/raw/` aren't downloaded again. Requests are spaced about a second apart per host.

**What's in it:** tax years ending 2021-2024 (mostly 2023), 16-79 pages per return, Part I always on page 1. A blank line on the form is `null` in the answer key, not `0`: blanks are common (volunteers in 19 of 60, some prior-year lines in up to 36), and so are negative values (current-year revenue less expenses in 19 of 60). Missions run up to 760 characters. Spot checks of three filings (tax years 2021, 2023, 2025) against their page images matched on every field.

## Baseline extraction (step 2)

`python src/extract.py --split dev --limit 3` renders page 1 of each filing's PDF to a PNG (longer side 1568 px) and asks Claude (`claude-sonnet-5`) to transcribe it:

- **Schema** (`src/schema.py`): a Pydantic model generated from `src/fields.py`. Structured outputs force the reply to match it: every field present with the right type. Blank lines are listed in `blank_lines` and become `null`; the API rejects schemas with more than 16 nullable ("int or null") fields, so nullable fields aren't used.
- **Checks** (`src/checks.py`): Part I's own arithmetic (line 12 = lines 8-11, line 18 = lines 13-17, line 19 = 12 - 18, line 22 = 20 - 21), a 9-digit EIN, and a tax year that begins before it ends. Every rule holds on all 60 answer keys, so a failure means a misreading.
- **Retry**: if a check fails, Claude is shown the broken rules in the same conversation and asked once more.
- **Prompts** (`prompts/<name>/<version>.txt`): versioned and immutable, as in the rag project; each result records the prompt version and fingerprint.
- **Cost**: priced from the API's token usage. Results go to `data/runs/<run>/` (gitignored for now).

First run, all 21 dev filings: **$0.025 per filing** (about 7,000 input and 1,050 output tokens), 5-14 s each, no check failures or retries. 866 of 882 fields (98.2%) matched the answer keys exactly; 14 filings were perfect. Every mismatch was checked against the page image:

| Mismatches | Cause | Whose error |
|---|---|---|
| 11 in 4 filings | Blank vs. 0: a 0 written into a blank cell next to a 0 (10), or the reverse (1). The arithmetic checks can't catch this, since blank counts as 0 | Claude |
| 3 | EIN digits scrambled right after the dash (41-1657792 read as 416657792): likely from removing the dash while transcribing | Claude |
| 1 | "SCHOOLINC" printed, "SCHOOL INC" returned: a correction, not a transcription | Claude (harmless) |
| 1 | Name printed on two lines; the answer key has only the first (`BusinessNameLine1Txt`). 11 of the 60 answer keys are truncated this way | Answer key |

Twenty-one filings is still a small sample: step 3 adds the eval harness and fixes the two-line names.

## Setup

Needs Python 3.14 and [uv](https://docs.astral.sh/uv/) (`pip install uv`):

```
uv sync
.venv\Scripts\activate
python -m pytest
```

`ruff check .` and `ruff format .` check style (CI's `irs-990-extraction-lint`). `bash security/audit.sh` checks locked dependencies for known vulnerabilities (CI's `irs-990-extraction-audit`).
