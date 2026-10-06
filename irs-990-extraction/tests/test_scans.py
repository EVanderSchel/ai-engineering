import pymupdf
import pytest

import scans


@pytest.fixture
def clean_pdf(tmp_path):
    path = tmp_path / "clean.pdf"
    with pymupdf.open() as doc:
        for number in range(1, 4):
            doc.new_page(width=612, height=792).insert_text((72, 72), f"Total revenue {number * 1000}", fontsize=12)
        doc.save(path)
    return path


def _first_page_pixels(pdf, dpi=50) -> bytes:
    with pymupdf.open(pdf) as doc:
        return doc[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY).samples


def test_a_scanned_copy_keeps_the_pages_and_their_size(clean_pdf, tmp_path):
    out = tmp_path / "scan.pdf"
    scans.make_scan(clean_pdf, out, "medium", "111")
    with pymupdf.open(clean_pdf) as clean, pymupdf.open(out) as scanned:
        assert scanned.page_count == clean.page_count
        assert [p.rect for p in scanned] == [p.rect for p in clean]
        assert scanned[0].get_text() == ""  # an image of the page, like the IRS's PDFs: no text to search


def test_the_same_return_and_level_always_get_the_same_damage(clean_pdf, tmp_path):
    a, b, c = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"
    scans.make_scan(clean_pdf, a, "heavy", "111")
    scans.make_scan(clean_pdf, b, "heavy", "111")
    scans.make_scan(clean_pdf, c, "heavy", "222")
    assert _first_page_pixels(a) == _first_page_pixels(b)
    assert _first_page_pixels(a) != _first_page_pixels(c)  # a different return gets a different tilt and dust


def test_levels_get_steadily_worse():
    light, medium, heavy = (scans.LEVELS[level] for level in ("light", "medium", "heavy"))
    assert light.dpi > medium.dpi > heavy.dpi
    assert light.paper > medium.paper > heavy.paper and light.ink > medium.ink > heavy.ink
    assert light.tilt < medium.tilt < heavy.tilt and light.blur < medium.blur < heavy.blur
    assert light.speckle < medium.speckle < heavy.speckle and light.jpeg > medium.jpeg > heavy.jpeg


def test_ink_fades_toward_the_paper(clean_pdf, tmp_path):
    out = tmp_path / "scan.pdf"
    scans.make_scan(clean_pdf, out, "heavy", "111")
    pixels = _first_page_pixels(out, dpi=100)
    assert min(pixels) > 0  # no pure black left: faded ink
    assert max(pixels) <= 255


def test_a_missing_scan_says_how_to_make_it(tmp_path, monkeypatch):
    monkeypatch.setattr(scans, "RAW_DIR", tmp_path)
    with pytest.raises(SystemExit, match="python src/scans.py --level medium"):
        scans.source_pdf("111", "medium")
    with pytest.raises(SystemExit, match="build_gold.py"):
        scans.source_pdf("111")
