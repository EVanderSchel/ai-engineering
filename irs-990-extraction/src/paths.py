"""Where the project keeps its data. Everything under data/raw/ is downloaded from public IRS sources
and gitignored; the gold set's answer keys (small JSON files) are committed."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"  # downloaded PDFs and XML (gitignored)
GOLD_DIR = DATA_DIR / "gold"  # answer keys built from the e-file XML (committed)
