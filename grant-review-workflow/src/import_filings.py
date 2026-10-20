"""Copy the 60 real Form 990 filings' answer keys from Project 2 (irs-990-extraction) into this project,
with NTEE codes from Project 4's copy of the IRS Business Master File. Run once; the output is committed,
so this project never reads another project's folder at run time.

    uv run python src/import_filings.py

Writes data/filings/<object_id>.json (the Part I answer key, read from the IRS e-file XML) and
data/filings/index.csv (one row per filing).
"""

import csv
import json

import paths

REPO = paths.ROOT.parent
GOLD_990 = REPO / "irs-990-extraction" / "data" / "gold"
BMF_NTEE = REPO / "ntee-classification" / "data" / "raw" / "bmf_ntee.csv"

INDEX_COLUMNS = ["object_id", "ein", "organization_name", "tax_period_end", "size_band", "ntee_code", "pdf_filename"]


def main() -> None:
    with open(GOLD_990 / "manifest.csv", encoding="utf-8", newline="") as f:
        manifest = list(csv.DictReader(f))
    eins = {row["ein"] for row in manifest}
    with open(BMF_NTEE, encoding="utf-8", newline="") as f:
        ntee = {row["ein"]: row["ntee_code"] for row in csv.DictReader(f) if row["ein"] in eins}

    paths.FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for row in manifest:
        key = json.loads((GOLD_990 / f"{row['object_id']}.json").read_text(encoding="utf-8"))
        fields = key["fields"]
        out = {
            "object_id": row["object_id"],
            "form_type": "990",  # Project 2's gold set is full Form 990s only
            "source": f"{key['source']}; copied from irs-990-extraction/data/gold",
            "fields": fields,
        }
        (paths.FILINGS_DIR / f"{row['object_id']}.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        rows.append(
            {
                "object_id": row["object_id"],
                "ein": row["ein"],
                "organization_name": fields["organization_name"],
                "tax_period_end": fields["tax_period_end"],
                "size_band": row["band"],
                "ntee_code": ntee.get(row["ein"], ""),
                "pdf_filename": row["pdf_filename"],
            }
        )

    rows.sort(key=lambda r: r["object_id"])
    with open(paths.FILINGS_DIR / "index.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    with_ntee = sum(1 for r in rows if r["ntee_code"])
    print(f"Copied {len(rows)} filings ({with_ntee} with an NTEE code) to {paths.FILINGS_DIR}")


if __name__ == "__main__":
    main()
