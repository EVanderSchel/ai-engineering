"""Where the project keeps its data. data/raw/ holds downloaded IRS files (gitignored, re-downloadable); the
gold set of application packets and results worth keeping go elsewhere under data/ and results/ and are
committed."""

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"  # IRS downloads (gitignored)
FILINGS_DIR = DATA_DIR / "filings"  # answer keys of the 60 real returns (from Project 2)
GOLD_DIR = DATA_DIR / "gold"  # one folder per application packet
# ProPublica API responses (gitignored). Lambda sets GRANT_REVIEW_CACHE_DIR to /tmp: its code folder is read-only.
CACHE_DIR = pathlib.Path(os.environ.get("GRANT_REVIEW_CACHE_DIR", DATA_DIR / "cache"))
