"""The gold set's return PDFs: the IRS's public page images of each filed Form 990, kept in data/raw/returns/
(gitignored: about 50 MB, and re-downloadable).

    uv run python src/returns.py              # list any that are missing (downloads nothing)
    uv run python src/returns.py --download   # fetch the missing ones from the IRS

The files were first downloaded by Project 2 (irs-990-extraction) from the same IRS URLs. Downloads are
polite: one request per second, a descriptive User-Agent, and each file checked to be a readable PDF before
it's saved.
"""

import argparse
import csv
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import pymupdf

import paths
from intake import return_pdf_path

IRS_PDF_BASE = "https://apps.irs.gov/pub/epostcard/cor"
HEADERS = {"User-Agent": "grant-review-workflow (learning project; github.com/EVanderSchel/ai-engineering)"}
SECONDS_BETWEEN_REQUESTS = 1.0


def needed() -> list[tuple[str, str, str]]:
    """(packet_id, object_id, IRS file name) for every gold packet."""
    with open(paths.FILINGS_DIR / "index.csv", encoding="utf-8", newline="") as f:
        filenames = {row["object_id"]: row["pdf_filename"] for row in csv.DictReader(f)}
    out = []
    for folder in sorted(paths.GOLD_DIR.glob("gp-*")):
        object_id = json.loads((folder / "spec.json").read_text(encoding="utf-8"))["object_id"]
        out.append((folder.name, object_id, filenames[object_id]))
    return out


def missing() -> list[tuple[str, str, str]]:
    return [entry for entry in needed() if not return_pdf_path(entry[1]).is_file()]


def url(filename: str) -> str:
    return f"{IRS_PDF_BASE}/{filename}"


def download(object_id: str, filename: str) -> pathlib.Path:
    """Fetch one return and save it, after checking it's a readable PDF with at least one page."""
    request = urllib.request.Request(url(filename), headers=HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if not data.startswith(b"%PDF-"):
        raise ValueError(f"{filename}: the IRS returned something other than a PDF")
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.page_count == 0:
            raise ValueError(f"{filename}: the PDF has no pages")
    path = return_pdf_path(object_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)  # only a complete, checked file ever appears under its real name
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download", action="store_true", help="fetch missing returns from the IRS")
    args = parser.parse_args()

    todo = missing()
    print(f"{len(needed()) - len(todo)} of {len(needed())} returns present in {return_pdf_path('x').parent}")
    for packet_id, object_id, filename in todo:
        print(f"  missing: {packet_id} {object_id} <- {url(filename)}")
    if not todo or not args.download:
        return 0
    for i, (packet_id, object_id, filename) in enumerate(todo):
        if i:
            time.sleep(SECONDS_BETWEEN_REQUESTS)
        try:
            print(f"  saved {download(object_id, filename)}")
        except (urllib.error.URLError, ValueError) as e:
            print(f"  failed {packet_id}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
