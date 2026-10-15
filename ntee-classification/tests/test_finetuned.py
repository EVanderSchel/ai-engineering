"""Scoring the fine-tuned model's predictions file, and the notebook generator's output (no GPU needed)."""

import ast
import json

import pytest

import data
import evaluate
import finetuned
from paths import ROOT


def predictions_file(tmp_path, rows, **overrides):
    run = {
        "approach": "lora_toy", "split": "dev", "model": "Qwen/Qwen2.5-0.5B", "latency_ms": 12.0,
        "batch_ms_per_mission": 0.5, "gpu_usd_per_hour": 0.36, "train_minutes": 10.0, "gpu": "Tesla T4",
        "settings": {"lora_rank": 16, "train_fraction": 1.0},
        "predictions": [{"ein": r["ein"], "ranked": [r["group"], "B", "P"]} for r in rows],
    } | overrides  # fmt: skip
    path = tmp_path / "lora_toy_dev.json"
    path.write_text(json.dumps(run), encoding="utf-8")
    return path


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(evaluate, "HISTORY_PATH", tmp_path / "results" / "history.csv")


def test_scores_a_complete_predictions_file(tmp_path, isolated_results):
    rows = data.load("dev")
    metrics = finetuned.score_file(predictions_file(tmp_path, rows))
    assert metrics["accuracy"] == 1.0 and metrics["n"] == len(rows)
    saved = json.loads((tmp_path / "results" / "lora_toy_dev.json").read_text(encoding="utf-8"))
    assert saved["details"]["train_cost_usd"] == pytest.approx(10 / 60 * 0.36)


def test_a_file_missing_missions_is_rejected(tmp_path, isolated_results):
    with pytest.raises(ValueError, match="doesn't cover exactly"):
        finetuned.score_file(predictions_file(tmp_path, data.load("dev")[:-1]))


def test_a_file_from_elsewhere_is_rejected(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"approach": "x", "split": "dev", "predictions": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        finetuned.load_predictions(path)


def test_gpu_cost_per_1000_predictions():
    # 0.5 ms each -> 0.5 s per 1,000 -> 0.5/3600 h x $0.36/h = $0.00005
    assert finetuned.cost_per_1k(0.5, 0.36) == pytest.approx(0.00005)


def test_the_notebook_is_current_and_its_code_parses():
    import make_finetune_notebook as generator

    committed = json.loads((ROOT / "notebooks" / "finetune_lora.ipynb").read_text(encoding="utf-8"))
    assert committed == generator.notebook(), "regenerate: python notebooks/make_finetune_notebook.py"
    for cell in committed["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(line for line in cell["source"] if not line.lstrip().startswith(("!", "%"))))
