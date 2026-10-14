"""Loading the dataset's splits. The test split is held out: each approach is scored on it once, at the end,
so loading it takes an explicit final=True (and the scripts a --final flag)."""

import csv

from paths import DATA_DIR

DATASET_PATH = DATA_DIR / "dataset.csv"
SPLITS = ("train", "dev", "test")


class HeldOutError(RuntimeError):
    """The test split was requested without final=True."""


def load(split: str, final: bool = False) -> list[dict]:
    """The rows of one split, in file order (ein, form, tax_period, mission, ntee_code, group, broad, split)."""
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}; expected one of {SPLITS}")
    if split == "test" and not final:
        raise HeldOutError("the test split is held out: pass final=True (--final) only for an approach's final score")
    with DATASET_PATH.open(encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f) if row["split"] == split]
