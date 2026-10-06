"""find_pages.py: header sheets, scoring, the labels, and one call with a fake client (no API, no downloads)."""

import csv
from types import SimpleNamespace

import pymupdf
import pytest

import find_pages


@pytest.fixture
def return_pdf(tmp_path):
    """A 120-page US Letter "return" whose pages say which page they are, at the top."""
    path = tmp_path / "return.pdf"
    with pymupdf.open() as doc:
        for number in range(1, 121):
            doc.new_page(width=612, height=792).insert_text((72, 30), f"Form 990 Page {number}", fontsize=14)
        doc.save(path)
    return path


def test_header_sheets_fit_the_image_limits_and_cover_every_page(return_pdf):
    sheets = [pymupdf.Pixmap(png) for png in find_pages.header_sheets(return_pdf)]
    assert len(sheets) > 1  # 120 pages don't fit on one sheet
    for sheet in sheets:
        assert max(sheet.width, sheet.height) <= 2576 and sheet.width * sheet.height <= 3_750_000
    # every page is on a sheet: a full sheet holds COLUMNS columns of strips, the last one the rest
    strip_height = 792 * find_pages.STRIP * find_pages.STRIP_WIDTH / 612 + find_pages.GAP
    per_sheet = find_pages.COLUMNS * int(find_pages.MAX_SHEET_HEIGHT // strip_height)
    assert len(sheets) == -(-120 // per_sheet)  # ceiling division


def test_score_separates_missed_from_extra_pages():
    assert find_pages.score([7, 8, 14], [7, 8, 14]) == {"missed": [], "extra": []}
    assert find_pages.score([7, 8, 35], [7, 8, 14]) == {"missed": [14], "extra": [35]}
    assert find_pages.score(None, [7, 8]) == {"missed": [7, 8], "extra": []}


def test_labels_cover_the_dev_split_and_always_include_pages_7_and_8():
    with (find_pages.GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
        dev = {row["object_id"] for row in csv.DictReader(f) if row["split"] == "dev"}
    labels = find_pages.labels()
    assert set(labels) == dev  # test returns aren't labeled: the test split stays held out
    for pages in labels.values():
        assert pages[:2] == [7, 8] and pages == sorted(pages)


def test_find_pages_sends_the_sheets_and_returns_sorted_unique_pages(return_pdf):
    requests = []

    def parse(**request):
        requests.append(request)
        usage = SimpleNamespace(
            input_tokens=7000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0
        )
        parsed = find_pages.PartVIIPages(pages=[8, 7, 8, 16])
        return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=usage)

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    result = find_pages.find_pages(client, return_pdf)

    assert result.pages == [7, 8, 16]
    content = requests[0]["messages"][0]["content"]
    assert [block["type"] for block in content[:-1]] == ["image"] * len(find_pages.header_sheets(return_pdf))
    assert "120 pages" in content[-1]["text"]
    assert requests[0]["output_format"] is find_pages.PartVIIPages
    assert result.cost_usd == pytest.approx((7000 * 2.00 + 500 * 10.00) / 1_000_000)
