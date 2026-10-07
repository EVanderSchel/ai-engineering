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
| 3 | Eval harness: per-field accuracy with normalization, results history by prompt version and model; prompt v2 *(done)* |
| 4 | Vision input choices: page selection, resolution, PDF vs. image input, cost per document; prompt caching *(done)* |
| 5 | Harder documents: multi-page sections (Part VII), scanned paper returns |
| 6 | Confidence and human review: per-field confidence, thresholds tuned on the eval set, review queue |
| 7 | Batch processing: Message Batches API, concurrency, idempotent reprocessing |
| 8 | Model comparison: Sonnet vs. Haiku on accuracy, cost, and latency, over repeated runs |
| 9 | Optional: an `/extract` API reusing the rag project's production setup |

## Gold set

60 real Form 990 filings from the IRS's 2024 index, 20 per size band by current-year total revenue (small < $500k, medium $500k-$5M, large >= $5M), split 7 **dev** / 13 **test** per band. Dev is for developing prompts; **test is held out** and only used for final scores, so improvements can't be tuned to the questions that grade them.

| File | What |
|---|---|
| `data/gold/<object_id>.json` | Answer key, read from the e-file XML: the 42 fields in `src/fields.py` (filer identity + Part I), and Part VII Section A (`part_vii`: one row per officer, director, key employee, or highly paid employee, with the line 1d totals and the line 2 count) |
| `data/gold/manifest.csv` | One row per filing: EIN, name, tax period, band, split, DLN, IRS PDF filename |
| `data/gold/skipped.csv` | Filings tried but left out, and why (15, all with no published PDF yet) |

Rebuild (or fetch the PDFs on a new machine) with `python src/build_gold.py`, or only re-read the answer keys from the downloaded XML with `--refresh-keys` (after changing `src/fields.py` or `src/irs_xml.py`); the fixed seed picks the same filings, and files already in `data/raw/` aren't downloaded again. Requests are spaced about a second apart per host.

**What's in it:** tax years ending 2021-2024 (mostly 2023), 16-79 pages per return, Part I always on page 1. A blank line on the form is `null` in the answer key, not `0`: blanks are common (volunteers in 19 of 60, some prior-year lines in up to 36), and so are negative values (current-year revenue less expenses in 19 of 60). Missions run up to 760 characters. Spot checks of three filings (tax years 2021, 2023, 2025) against their page images matched on every field.

## Baseline extraction (step 2)

`python src/extract.py` (all 21 dev filings; `--limit 3` for a quick trial) renders page 1 of each filing's PDF to a PNG (longer side 1568 px) and asks Claude (`claude-sonnet-5`) to transcribe it:

- **Schema** (`src/schema.py`): a Pydantic model generated from `src/fields.py`. Structured outputs force the reply to match it: every field present with the right type. Blank lines are listed in `blank_lines` and become `null`; the API rejects schemas with more than 16 nullable ("int or null") fields, so nullable fields aren't used.
- **Checks** (`src/checks.py`): Part I's own arithmetic (line 12 = lines 8-11, line 18 = lines 13-17, line 19 = 12 - 18, line 22 = 20 - 21), a 9-digit EIN, and a tax year that begins before it ends. Every rule holds on all 60 answer keys, so a failure means a misreading.
- **Retry**: if a check fails, Claude is shown the broken rules in the same conversation and asked once more.
- **Prompts** (`prompts/<name>/<version>.txt`): versioned and immutable, as in the rag project; each result records the prompt version and fingerprint.
- **Cost**: priced from the API's token usage. Each run is saved to `data/runs/<run>/` (committed: one JSON per filing with the answer, every attempt, and token usage) and scored automatically (step 3).

First run, all 21 dev filings: **$0.025 per filing** (about 7,000 input and 1,050 output tokens), 5-14 s each, no check failures or retries. 866 of 882 fields (98.2%) matched the answer keys exactly; 14 filings were perfect. Every mismatch was checked against the page image:

| Mismatches | Cause | Whose error |
|---|---|---|
| 11 in 4 filings | Blank vs. 0: a 0 written into a blank cell next to a 0 (10), or the reverse (1). The arithmetic checks can't catch this, since blank counts as 0 | Claude |
| 3 | EIN digits scrambled right after the dash (41-1657792 read as 416657792): likely from removing the dash while transcribing | Claude |
| 1 | "SCHOOLINC" printed, "SCHOOL INC" returned: a correction, not a transcription | Claude (harmless) |
| 1 | Name printed on two lines; the answer key has only the first (`BusinessNameLine1Txt`). 11 of the 60 answer keys are truncated this way | Answer key |

Twenty-one filings is still a small sample. Step 3 fixed the two-line names and rescored this run (below).

## Evaluation (step 3)

`python src/evaluate.py data/runs/<run>` scores a run against the answer keys and adds a row to `results/history.csv` (run, split, model, prompt version and fingerprint, accuracy, errors by type, retries, cost and time per filing). `extract.py` does this at the end of every run. `python src/evaluate.py --all` rescores every saved run, with no API calls: use it after fixing an answer key or a scoring rule, so old and new runs are always scored the same way.

**Scoring rules.** A field is correct when it equals the answer key after normalization, and normalization only forgives differences that don't change the value's meaning:

| Field | Ignored | Still counts |
|---|---|---|
| `organization_name`, `mission` | case, whitespace (line wraps, "SCHOOLINC" vs. "SCHOOL INC") | spelling, punctuation, missing words |
| `ein` | the dash | every digit |
| amounts, counts, dates | nothing | blank (`null`) and 0 are different answers |

**Error types.** Accuracy alone hides what to fix, so every wrong field is classified: `blank_as_zero` (blank line read as 0), `zero_as_blank`, `missed` (a value read as blank), `invented` (a blank read as a non-zero value), and `wrong_value`.

**Answer-key fix.** Long organization names are split over two XML elements (`BusinessNameLine1Txt`, `BusinessNameLine2Txt`) and printed on two lines; the answer keys had only the first, in 11 of 60 filings. `irs_xml.py` now reads the whole name, and `python src/build_gold.py --refresh-keys` rebuilt the keys from the downloaded XML (only those 11 names changed).

**Baseline, rescored** (`extract/v1`, `claude-sonnet-5`, 21 dev filings): **98.3%** of fields correct (867 of 882), all 42 right on 14 filings. Errors: 9 `blank_as_zero`, 2 `zero_as_blank`, 4 `wrong_value` (3 EINs, and one two-line name returned as its first line only).

**Prompt v2 vs. v1.** v2 changes three instructions, one per error found in the baseline: copy the EIN exactly as printed, dash included (the code removes the dash); read each cell on its own, since a blank cell next to a 0 is still blank; and include the second line of a long name (but not a care-of line). Single runs vary: two runs of the identical v1 prompt scored 98.3% and 99.0%. So each prompt ran twice on all 21 dev filings:

| Prompt | Runs | Fields correct | Blank vs. 0 errors | Wrong values | Cost per filing |
|---|---|---|---|---|---|
| v1 | 2 | 98.6% (1,740 of 1,764) | 16 | 8 (6 EINs, 2 names) | $0.025 |
| **v2** | 2 | **99.7%** (1,759 of 1,764) | 5 | **0** | $0.027 |

The EIN fix is clear-cut: v1 scrambled digits next to the dash in both runs, mostly on the same filings, and v2 read all 42 EINs correctly. Blank-vs-0 errors fell from 16 to 5, but they come in clusters (one filing had 3 of v2's 5), so that gain is likely but not yet proven. v2 is now the active prompt. It costs about 5% more per filing.

**The test split is held out:** `extract.py --split test` refuses to run without `--final`, so test filings are only scored once prompt and model choices are made on dev.

## Vision inputs and cost (step 4)

`extract.py --input` sends page 1 as `image-<pixels>` (a PNG whose longer side is that many pixels, up to 2576, Claude Sonnet 5's maximum) or `pdf` (the page as a one-page PDF, rendered by the API). Prompt caching is on by default (`--no-cache` turns it off). Both are recorded with every run and in `results/history.csv`.

**What each input costs** (input tokens, counted for free with the token-counting endpoint, mean of the 21 dev filings):

| Input | Page tokens | Request input tokens | Input cost |
|---|---|---|---|
| image-1000 | ~820 | 5,870 | $0.0117 |
| image-1568 | ~1,990 | 7,040 | $0.0141 |
| image-2000 | ~3,250 | 8,300 | $0.0166 |
| image-2576 | ~4,730 | 9,770 | $0.0195 |
| pdf | ~1,600 | 6,650 | $0.0133 |
| whole return as a PDF | 30,000-71,000 | | $0.06-0.14 |

The IRS scans are 300 dpi (2253 x ~3600 px), so every option here is a downscale. Image tokens grow with pixel area. Most of each request is fixed: the prompt and schema are 5,047 tokens. Sending only page 1 matters most: the whole return costs 5-10x as much, and Part I is always on page 1.

**Accuracy and cost per filing** (prompt v2). The first pass of each option ran out of API credit before the end, and the second passes never started, so each is compared with the two complete v2 runs at image-1568 *on the same filings*:

| Input | Filings | Errors | Same filings in the 2 earlier image-1568 runs | Cost per filing | Same filings, earlier runs |
|---|---|---|---|---|---|
| image-1000 | 10 | 32 | 1, 0 | $0.040 | $0.026 |
| pdf | 18 | 20 | 5, 0 | $0.028 | $0.027 |
| image-2576 | 20 | 5 | 5, 0 | $0.031 | $0.027 |
| image-1568, cached | 20 | 12 | 5, 0 | **$0.0175** | $0.027 |

- **Lower resolution is a false economy.** At 1000 px Claude misread digits (127,203 as 127,283; 99,360 as 99,760), and on one filing it shifted three rows (line 17's numbers read as line 18's). That misreading still added up, so the arithmetic checks didn't catch it. It also thought longer and retried more, so it cost **more** per filing, not less. The PDF input (rendered by the API at about 1,600 tokens) had the same row shift.
- **More resolution didn't help.** image-2576 matched image-1568's errors on the same filings, and cost 15% more.
- **Prompt caching cut the cost by 35%.** The prompt and schema (5,030 tokens) are written to the cache once, then read at a tenth of the price on every later filing (the cache lasts 5 minutes from the last use). The request's content is unchanged, so caching can't change the answers.
- **Noise is lumpy.** The cached run sends the same content as the earlier image-1568 runs, yet it had 12 errors to their 5 and 0. One filing had its whole current-year column read as blank (6 errors at once). Errors come in clusters, so error counts from a few runs swing widely, and the blank-vs-0 improvement claimed for prompt v2 is weaker than it looked.

**Decision:** keep image-1568, and turn caching on. That's $0.0175 per filing, down from $0.026. A run that stops early (no credit, an outage) is now marked incomplete in its `summary.json`, and `evaluate.py` leaves it out of the history.

## Part VII: officers, directors, and pay (step 5a)

Part VII Section A lists everyone the organization must report, with their title, hours, position (director, officer, key employee, ...), and three columns of pay. It's a table of unknown length: in the gold set a median of 4 rows for small organizations and 14.5 for large ones, up to 88 (703 rows in all, in the answer keys' `part_vii` section). The form has room for 26 rows on pages 7-8; longer lists, and some shorter ones, move to "Additional Data" continuation pages elsewhere in the return, and pages 7-8 then say "See Additional Data Table". The PDFs are images, so those pages can't be found by searching text.

Extraction runs in two stages: find the pages, then read the rows from just those pages.

**Stage 1, finding the pages** (`src/find_pages.py`, prompt `find_pages/v1`). Claude gets one or more *header sheets* per return: the top strip of every page, stacked and numbered, sized to Claude Sonnet 5's image limits (2576 px per side, 3.75 megapixels) so nothing is paid for and then scaled away. Full-page thumbnails were tried while labeling and are unreadable at that size; page headers are clear. Other schedules have look-alike headers (Schedule D "Part VII Investments", Schedule R continuation pages, Schedule J "Compensation Information"), and the prompt names them.

The correct pages for the 21 dev returns are in `data/gold/part_vii_pages.csv`: proposed by comparing page headers with known Part VII headers, then every proposal and every return's page 7 checked by eye (the header comparison itself was fooled twice by Schedule R pages). All 21 have Part VII on pages 7-8; 3 add continuation pages (4, 9, and 3 of them).

First run (`data/page_runs/`): **21 of 21 returns exactly right**, no pages missed and none extra, both Schedule R decoys left out. $0.015 per return (7,442 input tokens, 15 output: Claude didn't need to think), 2.2 s each. One run on 21 returns, so a small sample; the scanned returns of step 5b will test it harder.

**Stage 2, reading the rows** (`src/part_vii.py`, prompt `extract_part_vii/v1`). The Part VII pages go to Claude at 1568 px, in order and labeled with their page numbers, with the prompt and schema cached. The answer is every row (13 columns each) plus the line 1d totals and the line 2 count. The request is streamed, because an 88-row list is a long answer. Hours are nullable (a blank line for related organizations is not 0.00); every row shares one definition, so the schema has 6 nullable fields, under the API's limit of 16.

*Checks:* each pay column must add up to its line 1d total within $1 per row (one answer key is $1 off by rounding), and no more rows can show over $100,000 in column (D) than line 2 counts. Both hold on all 60 answer keys. A list that doesn't add up gets one retry with the gap.

*Scoring* (`src/score_part_vii.py`, history in `results/part_vii_history.csv`): rows are first matched to the answer key by name (ignoring case, spaces, and punctuation; a name at least 80% alike still counts as that person). Unmatched answer-key rows are *missed people*, unmatched extracted rows *invented people*. Matched rows and the totals are then scored field by field with the Part I rules and error types (punctuation in a name still counts).

First run on the 21 dev returns (pages from the labels, so stage 2 is measured on its own):

| | |
|---|---|
| People | **320 of 320 found**, none invented |
| Fields of matched rows | 99.5% correct: every name, title, hour, and amount right |
| Totals | 81 of 84 correct |
| Returns entirely right | 18 of 21 |
| Cost and time | $0.038 per return, 16 s (Prep for Prep, 88 rows on 11 pages: $0.20, 91 s); no retries |

Every error was checked against the page image and every one is Claude's. They have one thing in common: Claude inferred instead of transcribing.
- **Checkboxes moved one column to the left**, on every row of two returns (20 errors). On one, every X is under "Individual trustee or director" and Claude marked "Officer" for a CFO and a President; on the other, X's under "Key employee" and "Highest compensated employee" became "Officer" and "Key employee" for a General Manager and Senior Directors. The columns are narrow with sideways labels, and Claude's answers match what the titles suggest.
- **Blank totals read as 0** (3 errors): line 1d is blank on one return, and Claude wrote the 0 that the rows add up to.

The checks can't catch either: a checkbox doesn't change any total, and 0 adds up the same as blank.

A second, identical run separated habits from bad luck: again 320 of 320 people and none invented, 99.8% of fields correct. The Valley Crest checkboxes (8 errors) and the blank totals (3) came back exactly the same, so those are habits; Gencure dropped from 12 errors to 2 (only Geoffrey Kindt's), so that one is partly luck.

**Prompt v2 didn't help.** `extract_part_vii/v2` named column (C)'s six boxes in order, told Claude to place each X by the column it's printed in and never from the title, and said a blank total stays null even when the rows show 0. Two runs of each prompt:

| Errors | v1 run 1 | v1 run 2 | v2 run 1 | v2 run 2 |
|---|---|---|---|---|
| Valley Crest checkboxes | 8 | 8 | 8 | 0 |
| Gencure checkboxes | 12 | 2 | 12 | 12 |
| Blank totals read as 0 | 3 | 3 | 4 (on a different return) | 0 |
| **Total** | 23 | 13 | 24 | 12 |

The same 36 errors over two runs either way; they moved between returns instead of going away. Where an X sits in a narrow column is a question of *seeing*, and instructions don't improve eyesight. v1 stays active (v2 is kept in the registry, unchanged, as the record of the attempt). Levers that act on perception, such as a zoomed crop of column (C), or treating checkbox fields as low-confidence and routing them to human review (step 6), are what's left to try.

## Simulated scans (step 5b)

Paper Form 990s predate mandatory e-filing, so they have no e-file XML and no answer keys. `src/scans.py` makes scanned copies of the gold-set returns instead: every page re-rendered at a lower resolution, with faded ink on gray paper, a tilted sheet, blur, dust specks, and JPEG compression, and saved as an image PDF like the IRS's own. The words and numbers are unchanged, so the answer keys still apply. The damage is seeded per return and level, so reruns get exactly the same pages. Every stage takes `--scan light|medium|heavy`, and runs and history rows record it.

| Level | Like | dpi | Tilt | Legibility at the size Claude sees |
|---|---|---|---|---|
| light | a decent office scan | 200 | 0.5° | fully legible |
| medium | a poor scan | 150 | 1.5° | readable; small print soft |
| heavy | a bad fax | 100 | 3° | names barely legible |

This tests image quality only: real paper returns also have handwriting, typewriter fonts, stamps, and other layouts. It's a test fixture, not a production step.

**Results** (one run per stage and level, 21 dev returns; image tokens depend only on size, so scans cost the same to send, but not to answer):

| Stage | Clean | Light | Medium | Heavy |
|---|---|---|---|---|
| Part I, fields correct | 98.6-99.7% | 98.5% | 96.6% | 73.2% (90.1% on the 17 filings that got an answer) |
| Part I, cost per filing | $0.0175 | $0.017 | $0.028 | $0.099 |
| Page finder, returns exactly right | 21/21 | 21/21 | 21/21 | 21/21 |
| Part VII, people missed / invented | 0 / 0 | 0 / 0 | 0 / 0 | 25 / 25 (of 320) |
| Part VII, fields of matched rows | 99.5-99.8% | 100% | 100% | 94.9% |
| Part VII, cost per return | $0.038 | $0.038 | $0.038 | $0.121 |

- **Light scans are as good as clean pages**, and medium costs about two points on Part I (new misread digits) and nothing on Part VII. Heavy is where it breaks.
- **The page finder never noticed.** Page headers are large print, and 21 of 21 returns were exactly right at every level.
- **Unreadable pages fail expensively, not gracefully.** On 4 of the 21 heavy Part I filings Claude used its whole 16,000-token output budget thinking and returned nothing ($0.16 each); the rest cost 5 times as much as clean pages. A production system needs a cap on that, and a way to flag unreadable input instead of retrying it.
- **Wrong answers on heavy scans look confident.** Part VII's 25 "missed" and 25 "invented" people are mostly the same people with misread names (KIM BOCKENSTEDT as KIM ROCKENSTEIN, too different to match), and pay amounts were misread 63 times. Nothing in the answer says the page was hard to read, which is what step 6 (confidence and human review) is for.
- Curiously, the Part VII checkbox errors of the clean runs didn't appear on light or medium scans. One run each, so this may be chance; not investigated.

## Confidence and human review (step 6)

The pipeline returns every field with the same apparent certainty, right or wrong. Step 6 adds signals that say which fields or documents to send to a person, measured as a trade-off: how many errors a signal catches against how much review work it creates.

**Free signals, from runs already made** (Part I and Part VII, dev):

| Signal | Errors caught | Fields flagged | Notes |
|---|---|---|---|
| Two identical runs disagree (Part I, clean) | 5 of 5, and 12 of 12 in a second pair | 0.6-2% | doubles the API cost |
| Two identical runs disagree (Part VII, clean) | 10 of 23 (43%) | 0.2% | misses *systematic* errors, which repeat identically |
| Claude thought a lot (output tokens) | per document: the quarter of filings with the least output had no errors, the top quarter most of them | | free; median 1,100 tokens on clean pages, 7,400 on heavy scans |
| Checks failed or retried | filings averaging 19 wrong fields (others 0.8) | | free, but rare (11 of 105 filings) |

**Claude's own doubts** (prompt `extract/v3` = v2 plus an `unsure_fields` list in the schema, `extract.py --confidence`; one run each, prompt v2's accuracy unchanged):

| Pages | Accuracy | Fields Claude flagged | Errors among them (answered filings) | Flags that were errors |
|---|---|---|---|---|
| clean | 98.8% | 14 (1.6%) | 1 of 11 (9%) | 7% |
| medium scan | 97.0% | 49 (5.6%) | 5 of 26 (19%) | 10% |
| heavy scan | 77.0% | 106, plus 3 filings with no answer | 36 of 84 (43%) | 34% |

Claude's doubts are a weak signal. Blank-vs-0 mistakes, the most common error on clean pages, were flagged 2 times out of 29: Claude is confidently wrong about them. It does better on genuinely illegible text (every value it couldn't read at all on heavy scans was flagged), but most errors still went unflagged. A model's self-reported confidence is not the same as its error rate; measured signals (disagreement, effort, checks) did much better here.

**Review rules** (`src/review.py`). A rule combines signals: send the *whole document* to a person when Claude gave no answer, the checks failed or needed a retry, or Claude wrote more than a set number of output tokens (it thinks longer on hard pages); send *single fields* when Claude doubted them or a second run disagrees. A rule is scored by the errors it sends to review (assuming the reviewer fixes them), the share of fields a person must look at, and the accuracy afterwards. Scored on the three prompt-v3 runs, with the prompt-v2 run of the same pages as the second run (the prompts differ only by the doubts paragraph, so it's a second, slightly different reading):

| Rule | Clean: errors caught / fields reviewed / accuracy after | Medium scan | Heavy scan |
|---|---|---|---|
| No review | 0 of 11 / 0% / 98.8% | 0 of 26 / 0% / 97.1% | 119 of 203 / 14% / 90.5% (filings with no answer) |
| Checks failed or retried | 0 of 11 / 0% / 98.8% | 0 of 26 / 0% / 97.1% | 187 of 203 / 57% / 98.2% |
| Claude's doubts | 1 of 11 / 1.6% / 98.9% | 5 of 26 / 5.6% / 97.6% | 155 of 203 / 26% / 94.6% |
| Second run disagrees | **11 of 11 / 1.2% / 100%** | 9 of 26 / 2.5% / 98.1% | 171 of 203 / 31% / 96.4% |
| Checks + thinking > 2,000 tokens | 0 of 11 / 4.8% / 98.8% | 24 of 26 / 33% / 99.8% | 195 of 203 / 86% / 99.1% |
| Checks + thinking > 2,000 + second run | **11 of 11 / 6.0% / 100%** | **24 of 26 / 34% / 99.8%** | **195 of 203 / 86% / 99.1%** |
| Everything (adds doubts) | 11 of 11 / 6.7% / 100% | 24 of 26 / 36% / 99.8% | 196 of 203 / 87% / 99.2% |

- **Different signals catch different errors.** On clean pages the errors are random slips on easy documents: a second run catches all of them, reviewing 1.2% of fields, while thinking length catches none. On scans the errors repeat (both runs misread the same blurred digit), so the second run catches only a third, and the effort signal (how long Claude thought) is what finds them.
- **Combined, the rule `checks + thinking > 2,000 tokens + second run`** brings clean pages to 100% by reviewing 6% of fields, medium scans to 99.8% by reviewing a third, and heavy scans to 99.1% by reviewing most of each document, which by then is simply "a person transcribes this one". It doubles the API cost (about 3.5 cents per clean filing).
- **Claude's doubts add almost nothing** once the other signals are in (one more error caught, on heavy scans).
- These rules and the 2,000-token threshold were chosen on the same 21 dev filings they're scored on, so the numbers are optimistic; the held-out test split is where they'd be confirmed.

**Review queue** (`src/review_queue.py`). The flagged fields of a run go on a local HTML page (`data/review/<run>.html`, gitignored, regenerable): one section per filing that needs a person, with page 1 on the left (click to zoom) and the fields to check on the right. Each field shows its form line, the extracted value, the second run's value when they disagree, and whether Claude doubted it. The reviewer corrects a value or clicks "Looks right"; "Blank" means the line is empty on the form, which is different from 0. A contents list at the top shows every filing with its progress, and filings with a few flagged fields come before whole-document reviews. A field counts as reviewed only once it has been edited or confirmed: "Download corrections" saves the reviewed fields and lists the ones nobody checked, so an untouched field is never passed off as confirmed. Entries stay in the browser while they work. `--apply corrections.json` writes the run again with the reviewed values (`data/runs/<run>_reviewed/`; unreviewed fields keep the extracted value and are listed as unreviewed), scored like any other run, so the history shows accuracy after review.

The first real review found two problems with the first version of the page, now fixed: two filings sat below a 42-field whole-document review with nothing saying more followed, and the download sent every field, so fields nobody had looked at counted as confirmed (5 errors stayed in that way). `--simulate` writes a perfect reviewer's corrections from the answer keys, to test that loop: on the clean run, the 53 flagged fields (6.0%) take accuracy from 98.8% to 100%, as the rule's score predicted.

Crops of each line would be quicker to review than the whole page, but page 1's lines move: a long mission pushes Part I down by several lines. Fixed positions and lining up the pages' text rows both put crops on the wrong line, and a wrong crop is worse than the whole page. Exact crops (for example from positions Claude reports) belong with the planned upgrade of this page to a shared web page that saves each reviewer's decisions.

## Setup

Needs Python 3.14 and [uv](https://docs.astral.sh/uv/) (`pip install uv`):

```
uv sync
.venv\Scripts\activate
python -m pytest
```

`ruff check .` and `ruff format .` check style (CI's `irs-990-extraction-lint`). `bash security/audit.sh` checks locked dependencies for known vulnerabilities (CI's `irs-990-extraction-audit`).
