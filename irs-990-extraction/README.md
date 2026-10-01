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
| 0 | Project scaffolding: uv, ruff, pytest, CI with required checks *(this step)* |
| 1 | Gold set: pick filings from the IRS index, download their XML and page images, build answer-key JSON |
| 2 | Schema and baseline: Pydantic model of Part I, Claude structured outputs from page images, validation and retry |
| 3 | Eval harness: per-field accuracy with normalization, results history by prompt version and model |
| 4 | Vision input choices: page selection, resolution, PDF vs. image input, cost per document |
| 5 | Harder documents: multi-page sections (Part VII), scanned paper returns |
| 6 | Confidence and human review: per-field confidence, thresholds tuned on the eval set, review queue |
| 7 | Batch processing: Message Batches API, concurrency, idempotent reprocessing |
| 8 | Model comparison: Sonnet vs. Haiku on accuracy, cost, and latency, over repeated runs |
| 9 | Optional: an `/extract` API reusing the rag project's production setup |

## Setup

Needs Python 3.14 and [uv](https://docs.astral.sh/uv/) (`pip install uv`):

```
uv sync
.venv\Scripts\activate
python -m pytest
```

`ruff check .` and `ruff format .` check style (CI's `irs-990-extraction-lint`). `bash security/audit.sh` checks locked dependencies for known vulnerabilities (CI's `irs-990-extraction-audit`).
