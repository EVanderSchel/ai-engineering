"""The classifier measurement's own logic: tokenizer settings, ranking, scoring and the memory reading. No model."""

import json

import numpy as np
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

import measure_classifier as mc

BROAD = {"1": {"name": "Arts", "groups": "A"}, "2": {"name": "Education", "groups": "B"}}


def tiny_tokenizer(tmp_path):
    vocab = {"<pad>": 0, "food": 1, "for": 2, "all": 3, "art": 4}
    tokenizer = Tokenizer(WordLevel(vocab, unk_token="<pad>"))
    tokenizer.pre_tokenizer = Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return path


def test_the_tokenizer_truncates_and_pads_as_training_did(tmp_path):
    info = {"max_tokens": 3, "pad_token_id": 0, "pad_token": "<pad>", "padding_side": "right"}
    tokenizer = mc.make_tokenizer(tiny_tokenizer(tmp_path), info)
    batch = mc.encode(tokenizer, ["food for all art", "art"])
    assert batch["input_ids"].tolist() == [[1, 2, 3], [4, 0, 0]]  # cut at 3 tokens; padded on the right
    assert batch["attention_mask"].tolist() == [[1, 1, 1], [1, 0, 0]]
    assert batch["input_ids"].dtype == np.int64  # what the exported model takes


def test_left_padding_is_honoured(tmp_path):
    info = {"max_tokens": 3, "pad_token_id": 0, "pad_token": "<pad>", "padding_side": "left"}
    batch = mc.encode(mc.make_tokenizer(tiny_tokenizer(tmp_path), info), ["food for", "art"])
    assert batch["input_ids"].tolist() == [[1, 2], [0, 4]]


def test_groups_are_ranked_by_logit():
    logits = np.array([[0.1, 2.0, -1.0, 0.5], [3.0, 0.0, 1.0, 2.0]])
    assert mc.ranked_groups(logits, list("ABCD")) == [["B", "D", "A"], ["A", "D", "C"]]


def test_scores_cover_top_group_top_three_broad_and_agreement():
    rows = [{"ein": e, "group": g} for e, g in zip("1234", "ABBA", strict=True)]
    ranked = {"1": ["A", "B", "C"], "2": ["A", "B", "C"], "3": ["B", "A", "C"], "4": ["B", "C", "A"]}
    s = mc.score(ranked, rows, BROAD, compare_to={"1": "A", "2": "A", "3": "A", "4": "A"})
    assert s == {"rows": 4, "accuracy": 0.5, "top3": 1.0, "broad": 0.5, "agreement": 0.5}
    assert "agreement" not in mc.score(ranked, rows, BROAD, None)


def test_peak_memory_is_read_and_grows_with_use():
    before = mc.peak_memory_mb()
    block = np.ones(64 * 2**20 // 8)  # 64 MB, touched
    assert before > 0 and mc.peak_memory_mb() >= before + 50
    del block


def test_the_dev_data_is_the_whole_split_with_colab_s_answers():
    data = json.loads(mc.DATA.read_text(encoding="utf-8"))
    assert len(data["rows"]) == 1813 and len({r["ein"] for r in data["rows"]}) == 1813
    assert all(r["lora_top1"] in "ABCDEFGHIJKLMNOPQRSTUVWXY" for r in data["rows"])
    assert data["reference"]["LoRA Qwen2.5-0.5B (fine-tuned, PyTorch on a T4)"]["accuracy"] == 0.5869


def test_a_variant_is_compared_with_fp32_by_top_group_and_logit_error():
    import quantize_classifier as qc

    reference = np.array([[2.0, 1.0, 0.0], [0.0, 1.0, 3.0]])
    variant = np.array([[1.5, 1.8, 0.0], [0.0, 1.0, 2.0]])
    assert qc.compare(variant, reference) == {"same_top1": 1, "rows": 2, "max_abs_diff": 1.0, "mean_abs_diff": 0.3833}
    assert set(qc.VARIANTS) >= {"matmul", "matmul-uint8-down-fp32"}
