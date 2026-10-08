"""Where the project keeps its data. data/cache/ holds responses from public APIs (gitignored, re-downloadable);
eval task sets and results that are worth keeping go elsewhere under data/ and are committed."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"  # cached API responses (gitignored)
