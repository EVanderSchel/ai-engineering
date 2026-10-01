"""Build the gold set: real Form 990 filings, each with its IRS page-image PDF (the extractor's input)
and an answer key built from its e-file XML (the correct values).

    python src/build_gold.py              # 60 filings: 20 small, 20 medium, 20 large organizations
    python src/build_gold.py --per-band 5 # a quick trial run

Filings are picked at random (fixed seed, so the same set every time) from returns the IRS processed
in --year, and spread across three size bands by current-year total revenue, since larger
organizations file longer, more varied returns. Within each band, about a third go to the "dev"
split (for developing prompts) and the rest to "test" (held out, only for final scoring).

Writes, all committed:
    data/gold/<object_id>.json   answer key for one filing
    data/gold/manifest.csv       one row per filing: identity, size band, split, PDF filename
    data/gold/skipped.csv        filings tried but left out, and why
PDFs and XML go to data/raw/ (gitignored) and can be re-downloaded by running this again.
"""

import argparse
import csv
import json
import random

import sources
from fields import FIELD_NAMES
from irs_xml import NotForm990, answer_key
from paths import GOLD_DIR

# Current-year total revenue (Part I line 12), in dollars.
BANDS = [("small", 0, 500_000), ("medium", 500_000, 5_000_000), ("large", 5_000_000, None)]
DEV_SHARE = 1 / 3
# If this many filings in a row fail before any succeeds, the problem is the pipeline (a changed URL
# or file layout), not individual filings: stop instead of skipping the whole index.
GIVE_UP_AFTER_FAILURES = 25


class PipelineBroken(RuntimeError):
    """The first GIVE_UP_AFTER_FAILURES filings all failed."""


def size_band(total_revenue: int | None) -> str | None:
    """The band for a filing's current-year total revenue; None if it's missing or negative."""
    if total_revenue is None or total_revenue < 0:
        return None
    for name, low, high in BANDS:
        if total_revenue >= low and (high is None or total_revenue < high):
            return name
    return None


def assign_splits(filings_by_band: dict[str, list[dict]]) -> None:
    """Mark each filing "dev" or "test": the first third of each band (in selection order) is dev,
    so both splits get the same mix of sizes."""
    for filings in filings_by_band.values():
        n_dev = round(len(filings) * DEV_SHARE)
        for i, filing in enumerate(filings):
            filing["split"] = "dev" if i < n_dev else "test"


def build(year: int, per_band: int, seed: int) -> None:
    rows = sources.form_990_rows(year)
    random.Random(seed).shuffle(rows)
    print(f"{len(rows)} Form 990 returns in the {year} index; picking {per_band} per size band")

    accepted: dict[str, list[dict]] = {name: [] for name, _, _ in BANDS}
    skipped: list[dict] = []
    xml = sources.XmlFetcher(year)
    fetched_any = False  # has any filing's XML been read successfully yet?
    try:
        for row in rows:
            if all(len(v) >= per_band for v in accepted.values()):
                break
            ident = {"object_id": row["OBJECT_ID"], "ein": row["EIN"], "tax_period": row["TAX_PERIOD"]}
            try:
                key = answer_key(xml.fetch(row["OBJECT_ID"], row["XML_BATCH_ID"]))
            except (NotForm990, sources.XmlNotFound) as e:
                skipped.append({**ident, "reason": str(e)})
                if not fetched_any and len(skipped) >= GIVE_UP_AFTER_FAILURES:
                    raise PipelineBroken(f"the first {len(skipped)} filings all failed; last error: {e}") from e
                continue
            fetched_any = True
            band = size_band(key["total_revenue_current_year"])
            if band is None or len(accepted[band]) >= per_band:
                continue  # band already full (or no usable revenue): not a skip, just not needed
            try:
                filename = sources.find_pdf_filename(row["EIN"], row["TAX_PERIOD"])
                sources.download_pdf(filename, row["OBJECT_ID"])
            except sources.PdfNotFound as e:
                skipped.append({**ident, "reason": str(e)})
                print(f"  skip {ident['ein']} ({band}): {e}")
                continue
            accepted[band].append({**ident, "band": band, "pdf_filename": filename, "dln": row["DLN"], "key": key})
            print(f"  {band:<6} {sum(map(len, accepted.values())):>3} filings  {key['organization_name']}")
    finally:
        xml.close()

    short = {band: per_band - len(v) for band, v in accepted.items() if len(v) < per_band}
    if short:
        print(f"WARNING: ran out of filings before filling every band; short by {short}")

    assign_splits(accepted)
    write(accepted, skipped, year)


def write(accepted: dict[str, list[dict]], skipped: list[dict], year: int) -> None:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    manifest_fields = ["object_id", "ein", "organization_name", "tax_period", "band", "split", "dln", "pdf_filename"]
    with (GOLD_DIR / "manifest.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=manifest_fields)
        writer.writeheader()
        for filings in accepted.values():
            for filing in filings:
                key = filing["key"]
                writer.writerow(
                    {
                        **{k: filing[k] for k in manifest_fields if k in filing},
                        "organization_name": key["organization_name"],
                    }
                )
                answer = {
                    "object_id": filing["object_id"],
                    "source": f"IRS e-file XML, index {year}",
                    "fields": {name: key[name] for name in FIELD_NAMES},
                }
                path = GOLD_DIR / f"{filing['object_id']}.json"
                path.write_text(json.dumps(answer, indent=2) + "\n", encoding="utf-8", newline="\n")
    with (GOLD_DIR / "skipped.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["object_id", "ein", "tax_period", "reason"])
        writer.writeheader()
        writer.writerows(skipped)

    counts = {band: {"dev": 0, "test": 0} for band in accepted}
    for band, filings in accepted.items():
        for filing in filings:
            counts[band][filing["split"]] += 1
    print(f"\nWrote {sum(map(len, accepted.values()))} answer keys to {GOLD_DIR}; {len(skipped)} skipped")
    for band, c in counts.items():
        print(f"  {band:<6} dev {c['dev']:>2}  test {c['test']:>2}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, default=2024, help="IRS index year (returns processed that year)")
    parser.add_argument("--per-band", type=int, default=20, help="Filings per size band")
    parser.add_argument("--seed", type=int, default=990, help="Random seed: the same seed picks the same filings")
    args = parser.parse_args()
    build(args.year, args.per_band, args.seed)
