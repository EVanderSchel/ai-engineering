"""The Colab notebooks (step 6 stages 3 and 4) are current with their generators, and their code parses."""

import ast
import json
import sys

import pytest

from paths import ROOT

sys.path.insert(0, str(ROOT / "notebooks"))
import make_export_notebook  # noqa: E402
import make_vllm_notebook  # noqa: E402

NOTEBOOKS = [(make_export_notebook, "export_ntee_onnx.ipynb"), (make_vllm_notebook, "vllm_t4.ipynb")]


@pytest.mark.parametrize("generator, name", NOTEBOOKS)
def test_the_notebook_is_current_and_its_code_parses(generator, name):
    committed = json.loads((ROOT / "notebooks" / name).read_text(encoding="utf-8"))
    assert committed == generator.notebook(), f"regenerate: uv run python notebooks/{generator.__name__}.py"
    for cell in committed["cells"]:
        if cell["cell_type"] == "code":
            code = "".join(line for line in cell["source"] if not line.lstrip().startswith(("!", "%")))
            # Notebooks allow `await` at the top level of a cell.
            compile(code, name, "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_the_export_notebook_keeps_training_s_label_order_and_length():
    source = "".join(source for _, source in make_export_notebook.CELLS)
    assert 'GROUPS = list("ABCDEFGHIJKLMNOPQRSTUVWXY")' in source  # sorted, as ntee-classification trained them
    assert "MAX_TOKENS = 128" in source


def test_the_vllm_notebook_runs_stage_2_s_measurement_from_main():
    source = "".join(source for _, source in make_vllm_notebook.CELLS)
    assert "/main/ai-gateway" in source and "src/measure_quantization.py" in source
    assert '"--max-model-len", "2048"' in source  # the same context per request as llama-server's slots
    assert '"--generation-config", "vllm"' in source  # no repetition penalty from the model's config, as llama-server
