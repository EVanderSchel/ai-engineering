"""The data-size curve: stratified subsampling, and the points it reads and trains."""

from collections import Counter

import data
import data_size


def test_a_stratified_sample_keeps_every_group_in_proportion():
    train = data.load("train")
    sample = data_size.stratified_sample(train, 0.1)
    full, sampled = Counter(r["group"] for r in train), Counter(r["group"] for r in sample)
    assert set(sampled) == set(full)
    assert all(sampled[g] == max(1, round(full[g] * 0.1)) for g in full)
    assert data_size.stratified_sample(train, 0.1) == sample  # same seed, same sample


def test_the_full_fraction_is_the_whole_training_set():
    train = data.load("train")
    assert data_size.stratified_sample(train, 1.0) is train


def test_lora_points_read_the_notebook_runs():
    points = [data_size.lora_point(f) for f in data_size.FRACTIONS]
    assert [p["train_examples"] for p in points] == sorted(p["train_examples"] for p in points)
    assert points[-1]["train_examples"] == len(data.load("train"))
