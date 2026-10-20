"""The return PDFs: which are needed, which are missing, and what a download accepts. No network."""

import io

import pymupdf
import pytest

import returns


def test_every_gold_packet_needs_one_return_with_its_irs_file_name():
    entries = returns.needed()
    assert len(entries) == 40 and len({object_id for _, object_id, _ in entries}) == 40
    assert all(filename.endswith(".pdf") for _, _, filename in entries)


def test_urls_point_at_the_irs_public_copies():
    assert returns.url("123_990.pdf") == "https://apps.irs.gov/pub/epostcard/cor/123_990.pdf"


def test_missing_lists_returns_not_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(returns, "return_pdf_path", lambda object_id: tmp_path / f"{object_id}.pdf")
    assert len(returns.missing()) == 40
    first = returns.needed()[0][1]
    (tmp_path / f"{first}.pdf").write_bytes(b"%PDF-")
    assert len(returns.missing()) == 39


def a_pdf() -> bytes:
    with pymupdf.open() as doc:
        doc.new_page()
        return doc.tobytes()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_download_is_checked_and_saved_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(returns, "return_pdf_path", lambda object_id: tmp_path / f"{object_id}.pdf")
    monkeypatch.setattr(returns.urllib.request, "urlopen", lambda request, timeout: FakeResponse(a_pdf()))
    path = returns.download("123", "123_990.pdf")
    assert path.read_bytes().startswith(b"%PDF-") and not list(tmp_path.glob("*.part"))


def test_a_download_that_isnt_a_pdf_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(returns, "return_pdf_path", lambda object_id: tmp_path / f"{object_id}.pdf")
    monkeypatch.setattr(returns.urllib.request, "urlopen", lambda request, timeout: FakeResponse(b"<html>Not found"))
    with pytest.raises(ValueError, match="other than a PDF"):
        returns.download("123", "123_990.pdf")
    assert not list(tmp_path.iterdir())
