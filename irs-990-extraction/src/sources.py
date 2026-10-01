"""Download filings from public sources: the IRS index and e-file XML, and the IRS page-image PDFs
(located through the ProPublica Nonprofit Explorer API).

Be polite: these are free public services. Every request waits so there's at most about one per
second per host, failed requests are retried a few times with growing pauses, and every downloaded
file is kept in data/raw/ and reused, so running the build again doesn't download anything twice.
"""

import csv
import time
from urllib.parse import parse_qs, urlparse

import pymupdf
import requests
from remotezip import RemoteZip

from paths import RAW_DIR

IRS_XML_BASE = "https://apps.irs.gov/pub/epostcard/990/xml"
IRS_PDF_BASE = "https://apps.irs.gov/pub/epostcard/cor"
PROPUBLICA_ORG = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
HEADERS = {"User-Agent": "irs-990-extraction (learning project; github.com/EVanderSchel/ai-engineering)"}
MIN_SECONDS_BETWEEN_REQUESTS = 1.0

_last_request: dict[str, float] = {}


def _wait_turn(host: str) -> None:
    """Sleep until at least MIN_SECONDS_BETWEEN_REQUESTS has passed since the last request to host."""
    wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()


def _polite_get(url: str, *, stream: bool = False, attempts: int = 4) -> requests.Response:
    """GET with per-host spacing and retries on network errors and 5xx/429 responses."""
    host = urlparse(url).netloc
    for attempt in range(1, attempts + 1):
        _wait_turn(host)
        try:
            response = requests.get(url, headers=HEADERS, timeout=60, stream=stream)
        except requests.RequestException:
            if attempt == attempts:
                raise
        else:
            if response.status_code < 500 and response.status_code != 429:
                return response
            if attempt == attempts:
                response.raise_for_status()
        time.sleep(2**attempt)  # 2, 4, 8 s
    raise AssertionError("unreachable")


# --- IRS index -----------------------------------------------------------------------------------


def index_path(year: int):
    return RAW_DIR / f"index_{year}.csv"


def download_index(year: int):
    """The IRS's list of e-filed returns processed in a year (~90 MB). Downloaded once."""
    path = index_path(year)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    response = _polite_get(f"{IRS_XML_BASE}/{year}/index_{year}.csv", stream=True)
    response.raise_for_status()
    partial = path.with_suffix(".partial")
    with partial.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    partial.replace(path)  # only a complete download gets the real name
    return path


def form_990_rows(year: int) -> list[dict]:
    """Index rows for full Form 990 returns (not 990-EZ or 990-PF)."""
    with download_index(year).open(encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f) if row["RETURN_TYPE"] == "990"]


# --- IRS e-file XML --------------------------------------------------------------------------------


class XmlNotFound(LookupError):
    """A filing's XML couldn't be read: its batch ZIP is unavailable, or the file isn't in it."""


def batch_url(year: int, batch_id: str) -> str:
    """URL of a monthly XML ZIP. The index spells some batch IDs in lowercase ("2024_TEOS_XML_04a"),
    but the IRS serves them only under uppercase names ("2024_TEOS_XML_04A.zip")."""
    return f"{IRS_XML_BASE}/{year}/{batch_id.upper()}.zip"


def entries_by_object_id(names: list[str]) -> dict[str, str]:
    """Map each filing's object ID to its entry name in a batch ZIP. Layouts differ by year: the 2025
    ZIPs keep files at the top ("<id>_public.xml"), the 2024 ones inside a folder named after the
    batch ("2024_TEOS_XML_05A/<id>_public.xml"), so match on the file name alone."""
    entries = {}
    for name in names:
        filename = name.rsplit("/", 1)[-1]
        if filename.endswith("_public.xml"):
            entries[filename.removesuffix("_public.xml")] = name
    return entries


class XmlFetcher:
    """Reads single filings out of the IRS's monthly ZIPs (~250 MB each) over HTTP, without
    downloading whole archives. Keeps each opened ZIP's directory for reuse within a run."""

    def __init__(self, year: int):
        self.year = year
        self._zips: dict[str, RemoteZip] = {}
        self._entries: dict[str, dict[str, str]] = {}  # batch ID -> {object ID: entry name in the ZIP}
        self._unavailable: dict[str, str] = {}  # batch ID -> why it couldn't be opened

    def fetch(self, object_id: str, batch_id: str) -> bytes:
        path = RAW_DIR / "xml" / f"{object_id}.xml"
        if path.exists():
            return path.read_bytes()
        batch_id = batch_id.upper()
        if batch_id in self._unavailable:
            raise XmlNotFound(self._unavailable[batch_id])
        if batch_id not in self._zips:
            try:
                self._zips[batch_id] = RemoteZip(batch_url(self.year, batch_id), headers=HEADERS)
            except Exception as e:  # remotezip raises its own RemoteIOError, or zip-format errors
                self._unavailable[batch_id] = f"batch {batch_id} unavailable: {e}"
                raise XmlNotFound(self._unavailable[batch_id]) from e
            self._entries[batch_id] = entries_by_object_id(self._zips[batch_id].namelist())
        entry = self._entries[batch_id].get(object_id)
        if entry is None:
            raise XmlNotFound(f"{object_id}_public.xml not in batch {batch_id}")
        _wait_turn(urlparse(IRS_XML_BASE).netloc)  # remotezip's own range requests get the same spacing
        data = self._zips[batch_id].read(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def close(self) -> None:
        for z in self._zips.values():
            z.close()
        self._zips.clear()


# --- Page-image PDFs -------------------------------------------------------------------------------


class PdfNotFound(LookupError):
    """No single, matching page-image PDF could be found for a filing."""


def pdf_filename_from_url(pdf_url: str) -> str:
    """ProPublica's pdf_url carries the IRS file path in its `path` query parameter, e.g.
    ".../download-filing?path=IRS%2F581524250_202506_990O_2026040124061231.pdf"; the last part is the
    filename the IRS serves the same document under."""
    path = parse_qs(urlparse(pdf_url).query).get("path", [""])[0]
    return path.rsplit("/", 1)[-1]


def find_pdf_filename(ein: str, tax_period: str) -> str:
    """The IRS PDF filename for a Form 990 filing, via ProPublica's API. ProPublica's own download
    links are bot-protected, so only its JSON API is used; the PDF itself comes from the IRS."""
    response = _polite_get(PROPUBLICA_ORG.format(ein=ein))
    if response.status_code == 404:
        raise PdfNotFound(f"ProPublica has no organization {ein}")
    response.raise_for_status()
    data = response.json()
    filings = data.get("filings_with_data", []) + data.get("filings_without_data", [])
    matches = [
        f for f in filings if str(f.get("tax_prd")) == tax_period and f.get("formtype") == 0 and f.get("pdf_url")
    ]  # formtype 0 is Form 990
    filenames = {pdf_filename_from_url(f["pdf_url"]) for f in matches}
    if len(filenames) != 1:
        # None found, or several (an amended return): we can't tell which PDF matches this XML.
        raise PdfNotFound(f"{len(filenames)} Form 990 PDFs for {ein} period {tax_period}")
    return filenames.pop()


def download_pdf(filename: str, object_id: str):
    """Download the IRS page-image PDF and check it really is a readable PDF."""
    path = RAW_DIR / "pdf" / f"{object_id}.pdf"
    if path.exists():
        return path
    response = _polite_get(f"{IRS_PDF_BASE}/{filename}")
    if response.status_code == 404 or not response.content.startswith(b"%PDF"):
        raise PdfNotFound(f"IRS has no PDF {filename} (HTTP {response.status_code})")
    response.raise_for_status()
    try:
        with pymupdf.open(stream=response.content, filetype="pdf") as doc:
            if doc.page_count == 0:
                raise PdfNotFound(f"{filename} has no pages")
    except pymupdf.FileDataError as e:
        raise PdfNotFound(f"{filename} is not a readable PDF") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path
