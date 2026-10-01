import pytest

from build_gold import assign_splits, size_band
from sources import batch_url, entries_by_object_id, pdf_filename_from_url


@pytest.mark.parametrize(
    ("revenue", "band"),
    [
        (0, "small"),
        (499_999, "small"),
        (500_000, "medium"),
        (4_999_999, "medium"),
        (5_000_000, "large"),
        (2_000_000_000, "large"),
        (None, None),
        (-1, None),
    ],
)
def test_size_band_boundaries(revenue, band):
    assert size_band(revenue) == band


def test_splits_give_each_band_the_same_dev_test_mix():
    filings = {band: [{"id": i} for i in range(20)] for band in ("small", "medium", "large")}
    assign_splits(filings)
    for band in filings.values():
        splits = [f["split"] for f in band]
        assert splits.count("dev") == 7 and splits.count("test") == 13
        assert splits[:7] == ["dev"] * 7  # dev comes first in selection order, so it's reproducible


def test_pdf_filename_comes_from_the_path_parameter():
    url = "https://projects.propublica.org/nonprofits/download-filing?path=IRS%2F581524250_202506_990O_2026040124061231.pdf"
    assert pdf_filename_from_url(url) == "581524250_202506_990O_2026040124061231.pdf"
    nested = "https://projects.propublica.org/nonprofits/download-filing?path=download990pdf_12_2023_prefixes_56-58%2F581524250_202306_990O_2023120722084341.pdf"
    assert pdf_filename_from_url(nested) == "581524250_202306_990O_2023120722084341.pdf"


def test_batch_url_uses_the_uppercase_name_the_irs_serves():
    # The index writes some batch IDs in lowercase; only the uppercase file exists.
    assert batch_url(2024, "2024_TEOS_XML_04a").endswith("/2024/2024_TEOS_XML_04A.zip")
    assert batch_url(2024, "2024_TEOS_XML_01A").endswith("/2024/2024_TEOS_XML_01A.zip")


def test_zip_entries_are_found_with_or_without_a_folder():
    names = ["2024_TEOS_XML_05A/202401289349300015_public.xml", "202522579349300162_public.xml", "2024_TEOS_XML_05A/"]
    assert entries_by_object_id(names) == {
        "202401289349300015": "2024_TEOS_XML_05A/202401289349300015_public.xml",  # 2024 layout
        "202522579349300162": "202522579349300162_public.xml",  # 2025 layout
    }
