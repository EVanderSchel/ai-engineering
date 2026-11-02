"""Writes notebooks/export_ntee_onnx.ipynb (run: uv run python notebooks/make_export_notebook.py).

Step 6 stage 3: the NTEE classifier ntee-classification fine-tuned in Colab (LoRA on Qwen2.5-0.5B with a 25-way
head, its adapter saved to Google Drive) becomes two ONNX files this project serves with ONNX Runtime on CPU:
fp32 (the merged model as trained) and int8 (weights quantized to 8-bit integers). The notebook checks both against
PyTorch before saving, so a broken export is caught in Colab rather than on this PC.

Generated from this file so its code lives in reviewable Python strings and can be syntax-checked by the tests
(tests/test_export_notebook.py). Edit here, then regenerate.
"""

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

CELLS = [
    (
        "markdown",
        """\
# Step 6, stage 3: the NTEE classifier to ONNX

ntee-classification's fine-tuned classifier (LoRA on Qwen2.5-0.5B, a 25-way head over the NTEE major groups) is
loaded from its adapter in Google Drive, merged into one ordinary model, exported to **ONNX** (a portable model
format that ONNX Runtime runs without PyTorch), and quantized to **int8**. Both files are checked against PyTorch
on 64 dev missions, then saved to Drive next to the adapter, in `ntee-classification/qwen2.5-0.5b/onnx/`.

**Run:** a CPU runtime is enough (Runtime > Change runtime type > CPU), then Runtime > Run all. About 10 minutes.
Then download the files listed by the last cell from Drive into `ai-gateway/models/ntee/` on the PC.""",
    ),
    (
        "code",
        """\
# 1. Settings.
RUN_NAME = "qwen2.5-0.5b"         # ntee-classification's run: its adapter is in MyDrive/ntee-classification/<RUN_NAME>/
BASE_MODEL = "Qwen/Qwen2.5-0.5B"
MAX_TOKENS = 128                  # as in training
GROUPS = list("ABCDEFGHIJKLMNOPQRSTUVWXY")  # the 25 NTEE major groups, in training's label order (sorted)
CHECK_ROWS = 64                   # dev missions the exports are checked on (the dev split only: test was used once)
DATASET_URL = "https://raw.githubusercontent.com/EVanderSchel/ai-engineering/main/ntee-classification/data/dataset.csv\"""",
    ),
    (
        "code",
        """\
# 2. Libraries: PEFT (to load the adapter), the ONNX exporter's dependencies, and ONNX Runtime.
!pip uninstall -y -q torchao   # Colab's torchao 0.10 is too old for current PEFT, and LoRA doesn't need it
!pip install -q peft onnx onnxscript onnxruntime
import json, pathlib, shutil
import numpy as np, pandas as pd, torch, transformers, peft, onnx, onnxruntime as ort
VERSIONS = {"torch": torch.__version__, "transformers": transformers.__version__, "peft": peft.__version__,
            "onnx": onnx.__version__, "onnxruntime": ort.__version__}
print(VERSIONS)""",
    ),
    (
        "code",
        """\
# 3. Google Drive, where ntee-classification's notebook saved the adapter.
from google.colab import drive
drive.mount("/content/drive")
RUN_DIR = pathlib.Path("/content/drive/MyDrive/ntee-classification") / RUN_NAME
ADAPTER = RUN_DIR / "adapter"
assert (ADAPTER / "adapter_config.json").exists(), f"no adapter in {ADAPTER}: is RUN_NAME right?"
WORK = pathlib.Path("/content/onnx")
WORK.mkdir(exist_ok=True)""",
    ),
    (
        "code",
        """\
# 4. Merge: the base model plus the adapter's small matrices, folded into one ordinary model (same answers,
#    no PEFT needed to run it). Eager attention exports more predictably than the fused kernels.
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from peft import PeftModel
tokenizer = AutoTokenizer.from_pretrained(ADAPTER)
base = AutoModelForSequenceClassification.from_pretrained(
    BASE_MODEL, num_labels=len(GROUPS), id2label=dict(enumerate(GROUPS)),
    label2id={g: i for i, g in enumerate(GROUPS)}, dtype=torch.float32, attn_implementation="eager")
base.config.pad_token_id = tokenizer.pad_token_id  # "score.weight MISSING" above is expected: the adapter has it
merged = PeftModel.from_pretrained(base, ADAPTER).merge_and_unload().eval()

class Classifier(torch.nn.Module):
    \"\"\"Token ids and attention mask in, the 25 groups' logits out (no cache, no extra outputs).\"\"\"
    def __init__(self, model):
        super().__init__()
        self.model = model
    def forward(self, input_ids, attention_mask):
        return self.model(input_ids=input_ids, attention_mask=attention_mask, use_cache=False).logits

classifier = Classifier(merged).eval()
parameters = sum(p.numel() for p in merged.parameters())
print(f"{parameters / 1e6:.0f}M parameters; padding side: {tokenizer.padding_side}")""",
    ),
    (
        "code",
        """\
# 5. The check rows: the first CHECK_ROWS dev missions, and PyTorch's answers for them.
df = pd.read_csv(DATASET_URL, dtype=str, keep_default_na=False)
check_df = df[df["split"] == "dev"].head(CHECK_ROWS)
batch = tokenizer(list(check_df["mission"]), truncation=True, max_length=MAX_TOKENS, padding=True,
                  return_tensors="pt")
with torch.no_grad():
    torch_logits = classifier(batch["input_ids"], batch["attention_mask"]).numpy()
print("PyTorch accuracy on the check rows:",
      np.mean(np.array(GROUPS)[torch_logits.argmax(-1)] == check_df["group"].to_numpy()))""",
    ),
    (
        "code",
        """\
# 6. Export fp32. The batch size and sequence length stay variable, so one file serves any request. The model is
#    about 2 GB, over protobuf's 2 GB limit, so the weights go in a separate model.onnx.data file.
FP32 = WORK / "fp32" / "model.onnx"
FP32.parent.mkdir(exist_ok=True)
example = tokenizer(list(check_df["mission"][:2]), truncation=True, max_length=MAX_TOKENS, padding=True,
                    return_tensors="pt")
inputs = (example["input_ids"], example["attention_mask"])
try:
    batch_dim, seq_dim = torch.export.Dim("batch", min=1), torch.export.Dim("sequence", min=1, max=MAX_TOKENS)
    torch.onnx.export(
        classifier, inputs, FP32, dynamo=True, external_data=True,
        input_names=["input_ids", "attention_mask"], output_names=["logits"],
        dynamic_shapes={"input_ids": {0: batch_dim, 1: seq_dim}, "attention_mask": {0: batch_dim, 1: seq_dim}})
    EXPORTER = "dynamo"
except Exception as error:  # the older TorchScript exporter, if the newer one can't trace this model
    print("dynamo export failed, using the TorchScript exporter:", repr(error)[:500])
    shutil.rmtree(FP32.parent)
    FP32.parent.mkdir()
    torch.onnx.export(
        classifier, inputs, FP32, dynamo=False, opset_version=17,
        input_names=["input_ids", "attention_mask"], output_names=["logits"],
        dynamic_axes={"input_ids": {0: "batch", 1: "sequence"}, "attention_mask": {0: "batch", 1: "sequence"},
                      "logits": {0: "batch"}})
    EXPORTER = "torchscript"
print("exported with", EXPORTER, sorted(p.name for p in FP32.parent.iterdir()))""",
    ),
    (
        "code",
        """\
# 7. int8: each weight matrix stored as 8-bit integers plus a scale (dynamic quantization: activations are
#    quantized on the fly, so no calibration data is needed). MatMul covers the transformer layers; Gather covers
#    the token embeddings, about a quarter of the weights.
from onnxruntime.quantization import quantize_dynamic, QuantType
INT8 = WORK / "int8" / "model.onnx"
INT8.parent.mkdir(exist_ok=True)
try:
    quantize_dynamic(FP32, INT8, weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul", "Gather"])
    QUANTIZED_OPS = ["MatMul", "Gather"]
except Exception as error:
    print("quantizing Gather failed, MatMul only:", repr(error)[:300])
    quantize_dynamic(FP32, INT8, weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul"])
    QUANTIZED_OPS = ["MatMul"]
print("quantized", QUANTIZED_OPS)""",
    ),
    (
        "code",
        """\
# 8. Check both files against PyTorch on the check rows: the fp32 export must give the same answers; int8 shows
#    how far quantization moves the logits.
feeds = {"input_ids": batch["input_ids"].numpy(), "attention_mask": batch["attention_mask"].numpy()}

def onnx_logits(path):
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return session.run(None, feeds)[0]

def compare(logits):
    return {"max_abs_diff": round(float(np.abs(logits - torch_logits).max()), 5),
            "same_top1": int((logits.argmax(-1) == torch_logits.argmax(-1)).sum()), "rows": CHECK_ROWS}

CHECK = {"fp32_vs_pytorch": compare(onnx_logits(FP32)), "int8_vs_pytorch": compare(onnx_logits(INT8))}
print(CHECK)
assert CHECK["fp32_vs_pytorch"]["same_top1"] == CHECK_ROWS, "the fp32 export doesn't give PyTorch's answers"
assert CHECK["fp32_vs_pytorch"]["max_abs_diff"] < 1e-2, "the fp32 export's logits drift from PyTorch's\"""",
    ),
    (
        "code",
        """\
# 9. Save to Drive: both models, the tokenizer, the labels, and export_info.json (settings, sizes, the check, and
#    PyTorch's top group for each check row, so the PC can confirm its tokenizer and runtime match).
OUT = RUN_DIR / "onnx"
tokenizer.backend_tokenizer.save(str(WORK / "tokenizer.json"))
(WORK / "labels.json").write_text(json.dumps(GROUPS))
sizes = {str(p.relative_to(WORK)).replace("\\\\", "/"): round(p.stat().st_size / 1e6, 1)
         for p in sorted(WORK.rglob("*")) if p.is_file()}
info = {
    "run": RUN_NAME, "base_model": BASE_MODEL, "max_tokens": MAX_TOKENS, "pad_token_id": tokenizer.pad_token_id,
    "pad_token": tokenizer.pad_token, "padding_side": tokenizer.padding_side, "exporter": EXPORTER,
    "quantized_ops": QUANTIZED_OPS, "sizes_mb": sizes, "check": CHECK, "versions": VERSIONS,
    "check_rows": [{"ein": e, "top1": GROUPS[i]}
                   for e, i in zip(check_df["ein"], torch_logits.argmax(-1), strict=True)],
}
(WORK / "export_info.json").write_text(json.dumps(info, indent=1))
shutil.copytree(WORK, OUT, dirs_exist_ok=True)
print(f"saved to {OUT}. Download these into ai-gateway/models/ntee/ on the PC, keeping the folders:")
for name, mb in info["sizes_mb"].items():
    print(f"  {name}  ({mb:,} MB)")""",
    ),
]


def notebook() -> dict:
    cells = []
    for kind, source in CELLS:
        cell = {"cell_type": kind, "metadata": {}, "source": source.splitlines(keepends=True)}
        if kind == "code":
            cell |= {"execution_count": None, "outputs": []}
        cells.append(cell)
    return {
        "cells": cells,
        "metadata": {
            "colab": {"provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }


if __name__ == "__main__":
    path = HERE / "export_ntee_onnx.ipynb"
    path.write_text(json.dumps(notebook(), indent=1) + "\n", encoding="utf-8")
    print("wrote", path)
