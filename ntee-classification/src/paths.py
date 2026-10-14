"""Where the project keeps its data. data/raw/ holds downloaded IRS files (gitignored, re-downloadable); the
built dataset and results that are worth keeping go elsewhere under data/ and results/ and are committed."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"  # IRS downloads (gitignored)
