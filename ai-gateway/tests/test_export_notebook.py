"""The Colab export notebook (step 6 stage 3) is current with its generator, and its code parses."""

import ast
import json
import sys

from paths import ROOT

sys.path.insert(0, str(ROOT / "notebooks"))
import make_export_notebook as generator  # noqa: E402


def test_the_notebook_is_current_and_its_code_parses():
    committed = json.loads((ROOT / "notebooks" / "export_ntee_onnx.ipynb").read_text(encoding="utf-8"))
    assert committed == generator.notebook(), "regenerate: uv run python notebooks/make_export_notebook.py"
    for cell in committed["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(line for line in cell["source"] if not line.lstrip().startswith(("!", "%"))))


def test_the_notebook_keeps_training_s_label_order_and_length():
    source = "".join(source for _, source in generator.CELLS)
    assert 'GROUPS = list("ABCDEFGHIJKLMNOPQRSTUVWXY")' in source  # sorted, as ntee-classification trained them
    assert "MAX_TOKENS = 128" in source
