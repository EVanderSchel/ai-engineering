"""Stage 1 of Part VII extraction: find the pages that hold Part VII, Section A.

Part VII is printed on form pages 7 and 8, but a long list continues on "Additional Data" pages
("Form 990, Part VII - Compensation of Officers, ...") that can be anywhere in a 16-79 page return.
The PDFs are page images with no text to search, and sending every page costs 5-10 times as much as
the extraction itself. So Claude gets a "header sheet" instead: the top strip of every page (where the
form prints "Page 7" or a continuation title), stacked and numbered, which is enough to tell the pages
apart and costs a few thousand tokens per return.

    python src/find_pages.py --count-tokens   # what a run would cost, without running it (free)
    python src/find_pages.py                  # find the pages for every dev return, scored against
                                              # the hand-checked labels in data/gold/part_vii_pages.csv
"""

import argparse
import csv
import datetime
import json
import time
from dataclasses import asdict, dataclass, field

import anthropic
import pymupdf
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

import prompts
from extract import MODEL, PRICES, Usage, _base64
from paths import DATA_DIR, GOLD_DIR, RAW_DIR

LABELS = GOLD_DIR / "part_vii_pages.csv"
RUNS_DIR = DATA_DIR / "page_runs"
MAX_TOKENS = 16000

# Header sheet layout, in pixels: two columns of page strips, each strip the top 7.5% of a page scaled to
# STRIP_WIDTH, with the page number in a gutter to its left. Claude Sonnet 5 shrinks images larger than
# 2576 px on a side or 3.75 megapixels, so a sheet stays within both (shrunk pixels are paid for and
# lost), and a long return gets more than one sheet.
STRIP = 0.075
STRIP_WIDTH = 800
GUTTER = 64
COLUMNS = 2
GAP = 8
SHEET_WIDTH = COLUMNS * (GUTTER + STRIP_WIDTH + GAP)
MAX_SHEET_HEIGHT = min(2576, int(3_750_000 / SHEET_WIDTH))


def header_sheets(pdf_path) -> list[bytes]:
    """PNG images of every page's header strip, numbered from 1, in page order."""
    with pymupdf.open(pdf_path) as doc:
        strips = []
        for page in doc:
            r = page.rect
            clip = pymupdf.Rect(0, 0, r.width, r.height * STRIP)
            strips.append((page, clip, clip.height * STRIP_WIDTH / r.width))
        row_height = max(h for _, _, h in strips) + GAP
        per_column = int(MAX_SHEET_HEIGHT // row_height)
        per_sheet = per_column * COLUMNS

        sheets = []
        for start in range(0, len(strips), per_sheet):
            chunk = strips[start : start + per_sheet]
            rows = min(per_column, len(chunk))
            with pymupdf.open() as out:
                sheet = out.new_page(width=SHEET_WIDTH, height=rows * row_height)
                for i, (page, clip, height) in enumerate(chunk):
                    x = (i // per_column) * (GUTTER + STRIP_WIDTH + GAP)
                    y = (i % per_column) * row_height
                    sheet.insert_text((x + 4, y + 22), str(start + i + 1), fontsize=20, color=(0.8, 0, 0))
                    target = pymupdf.Rect(x + GUTTER, y, x + GUTTER + STRIP_WIDTH, y + height)
                    sheet.show_pdf_page(target, doc, page.number, clip=clip)
                    sheet.draw_line((x, y + height + GAP / 2), (x + GUTTER + STRIP_WIDTH, y + height + GAP / 2))
                sheets.append(sheet.get_pixmap(dpi=72).tobytes("png"))  # 72 dpi: one pixel per point
        return sheets


class PartVIIPages(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages: list[int] = PydanticField(
        description="Page numbers (as labeled on the sheet) that hold Form 990 Part VII Section A, in order"
    )


def _request(pdf_path) -> dict:
    """The request for one return, without the model (shared by the real call and token counting)."""
    sheets = header_sheets(pdf_path)
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _base64(png)}}
        for png in sheets
    ]
    content.append({"type": "text", "text": f"This return has {_page_count(pdf_path)} pages. Which hold Part VII?"})
    return {"system": prompts.load("find_pages").template, "messages": [{"role": "user", "content": content}]}


def _page_count(pdf_path) -> int:
    with pymupdf.open(pdf_path) as doc:
        return doc.page_count


@dataclass
class Found:
    pages: list[int] | None  # None if Claude stopped without an answer
    model: str
    prompt: str
    prompt_sha256: str
    stop_reason: str = ""
    seconds: float = 0.0
    usage: Usage = field(default_factory=Usage)

    @property
    def cost_usd(self) -> float:
        return self.usage.cost(self.model)


def find_pages(client, pdf_path, *, model: str = MODEL) -> Found:
    prompt = prompts.load("find_pages")
    result = Found(pages=None, model=model, prompt=prompt.id, prompt_sha256=prompt.sha256)
    started = time.monotonic()
    response = client.messages.parse(
        model=model, max_tokens=MAX_TOKENS, output_format=PartVIIPages, **_request(pdf_path)
    )
    result.seconds = round(time.monotonic() - started, 1)
    result.usage.add(response.usage)
    result.stop_reason = response.stop_reason
    if response.stop_reason not in ("refusal", "max_tokens"):
        result.pages = sorted(set(response.parsed_output.pages))
    return result


def labels() -> dict[str, list[int]]:
    with LABELS.open(encoding="utf-8", newline="") as f:
        return {row["object_id"]: [int(p) for p in row["pages"].split()] for row in csv.DictReader(f)}


def score(found: list[int] | None, expected: list[int]) -> dict:
    """Missed pages lose rows; extra pages only cost tokens (and risk rows from a decoy table)."""
    found_set = set(found or [])
    return {"missed": sorted(set(expected) - found_set), "extra": sorted(found_set - set(expected))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only the first N labeled returns")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    parser.add_argument("--count-tokens", action="store_true", help="estimate the cost without running (free)")
    args = parser.parse_args()

    expected = labels()  # dev returns only: the test split stays held out
    object_ids = list(expected)[: args.limit]
    client = anthropic.Anthropic()

    if args.count_tokens:
        counts = []
        for object_id in object_ids:
            request = _request(RAW_DIR / "pdf" / f"{object_id}.pdf")
            tokens = client.messages.count_tokens(
                model=args.model,
                # the same schema parse() sends (the SDK has no public helper for it)
                output_config={"format": {"type": "json_schema", "schema": transform_schema(PartVIIPages)}},
                **request,
            ).input_tokens
            counts.append(tokens)
            sheets = sum(1 for block in request["messages"][0]["content"] if block["type"] == "image")
            print(f"{object_id}: {sheets} sheet(s), {tokens} input tokens")
        input_price, _ = PRICES[args.model]
        mean = sum(counts) / len(counts)
        print(f"\nMean {mean:.0f} input tokens: ${mean * input_price / 1e6:.4f} input per return, plus output")
        return

    run_dir = RUNS_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{args.model}_{prompts.load('find_pages').version}"
    run_dir.mkdir(parents=True)
    total, exact, missed, extra = Usage(), 0, 0, 0
    for object_id in object_ids:
        result = find_pages(client, RAW_DIR / "pdf" / f"{object_id}.pdf", model=args.model)
        total.add(result.usage)
        s = score(result.pages, expected[object_id])
        exact += not s["missed"] and not s["extra"]
        missed += len(s["missed"])
        extra += len(s["extra"])
        record = {"object_id": object_id, "expected": expected[object_id], **s, "cost_usd": result.cost_usd}
        record |= asdict(result)
        (run_dir / f"{object_id}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"{object_id}: found {result.pages}, missed {s['missed']}, extra {s['extra']}, ${result.cost_usd:.4f}")

    n = len(object_ids)
    summary = {"run": run_dir.name, "returns": n, "exact": exact, "missed_pages": missed, "extra_pages": extra}
    summary |= {"cost_usd": total.cost(args.model), "usage": asdict(total)}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        f"\n{exact} of {n} returns exactly right; {missed} pages missed, {extra} extra; ${total.cost(args.model):.4f}"
    )


if __name__ == "__main__":
    main()
