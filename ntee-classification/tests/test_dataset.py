"""Integrity of the committed dataset (data/dataset.csv): the rules every later step relies on."""

import csv
from collections import Counter

import pytest

import build_dataset as bd
import ntee

ROWS = list(csv.DictReader(bd.DATASET_PATH.open(encoding="utf-8", newline=""))) if bd.DATASET_PATH.exists() else []
pytestmark = pytest.mark.skipif(not ROWS, reason="data/dataset.csv not built yet")


def test_columns_and_values():
    assert list(ROWS[0]) == bd.FIELDS
    for r in ROWS:
        assert r["group"] in ntee.GROUPS and r["group"] == r["ntee_code"][0]
        assert r["broad"] == ntee.broad_category(r["group"])
        assert r["split"] in bd.SPLITS and r["form"] in ("990", "990EZ")
        assert len(r["mission"]) >= bd.MIN_CHARS


def test_no_organization_or_mission_text_appears_twice():
    """So nothing in test was also seen in training, even as a near-copy."""
    assert len({r["ein"] for r in ROWS}) == len(ROWS)
    assert len({bd.text_key(r["mission"]) for r in ROWS}) == len(ROWS)


def test_splits_have_the_designed_proportions_and_the_same_group_mix():
    counts = Counter(r["split"] for r in ROWS)
    for name, share in bd.SPLITS.items():
        assert abs(counts[name] / len(ROWS) - share) < 0.01
    for group in {r["group"] for r in ROWS}:
        in_group = [r for r in ROWS if r["group"] == group]
        if len(in_group) >= 100:
            test_share = sum(r["split"] == "test" for r in in_group) / len(in_group)
            assert abs(test_share - bd.SPLITS["test"]) < 0.02, group
