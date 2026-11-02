"""int8 versions of the NTEE classifier, made on this PC from the fp32 export, and a quick check of each (step 6
stage 3). Free: everything is local.

    uv run python src/quantize_classifier.py                 # every variant below, then the check
    uv run python src/quantize_classifier.py --check-only    # check the variants already made

The Colab notebook's int8 (every MatMul and the token embeddings, one scale per weight matrix) agreed with fp32 on
25 of the 64 check rows. Dynamic quantization stores each weight as an 8-bit integer times a scale; one scale for a
whole matrix loses detail wherever a few large values set the scale for everything else. Each variant below keeps
more of the model's precision, to find what the damage came from:

- matmul:                 the transformer's matrices only; the token embeddings stay fp32
- matmul-perchannel:      the same, with one scale per output column instead of one per matrix
- matmul-perchannel-head: the same, and the 25-way classification head stays fp32 too
- matmul-reducerange, matmul-uint8: the remedies for int8 overflow on CPUs without VNNI
- matmul-uint8-down-fp32: unsigned weights, with the 24 MLP down projections (the outliers' inputs) left in fp32
- weightonly8, weightonly4-gather: weight-only quantization (activations stay fp32); 4-bit includes the embeddings
- weightonly8-level4, weightonly4-gather-level4: the same, multiplied in integers with activations quantized per block

Variants go in models/ntee/int8-<name>/ (gitignored). The check compares each one's logits with fp32's on the 64
check rows; src/measure_classifier.py then measures the chosen one in full.
"""

import argparse
import json
import sys

import numpy as np
import onnxruntime as ort
from onnxruntime.quantization import QuantType, quantize_dynamic

from measure_classifier import NTEE_DIR, encode, load_inputs, make_tokenizer

HEAD = "node_linear_168"  # the 896 -> 25 classification head's MatMul in the fp32 export
VARIANTS = {
    "matmul": {"per_channel": False, "nodes_to_exclude": []},
    "matmul-perchannel": {"per_channel": True, "nodes_to_exclude": []},
    "matmul-perchannel-head": {"per_channel": True, "nodes_to_exclude": [HEAD]},
    # On CPUs without VNNI (this i7-6700 has AVX2 only), ONNX Runtime's signed-weight int8 multiply can overflow its
    # intermediate sums; its documented remedies are 7-bit weights (reduce_range) or unsigned weights.
    "matmul-reducerange": {"per_channel": False, "nodes_to_exclude": [], "reduce_range": True},
    "matmul-uint8": {"per_channel": False, "nodes_to_exclude": [], "weight_type": QuantType.QUInt8},
    # Models like Qwen have a few huge activation channels, largest at each MLP's down projection (4864 -> 896).
    # Dynamic quantization turns activations into 8 bits with one scale per tensor too, so those outliers set the
    # scale and flatten everything else. Keeping the 24 down projections in fp32 tests that.
    "matmul-uint8-down-fp32": {"per_channel": False, "nodes_to_exclude": "down", "weight_type": QuantType.QUInt8},
}
# Weight-only: weights stored in 8 (or 4) bits with one scale per block of 128, and turned back into floats for the
# multiply, so activations are never quantized and their outliers can't do damage (ONNX Runtime's MatMulNBits and,
# for the embeddings, GatherBlockQuantized).
# Only the 4-bit version quantizes the embeddings: asked for 8-bit Gather, this ONNX Runtime left them in fp32.
WEIGHT_ONLY = {
    "weightonly8": {"bits": 8, "ops": ("MatMul",)},
    "weightonly4-gather": {"bits": 4, "ops": ("MatMul", "Gather")},
    # Unset, the multiply turns weights back into floats on a general path: accurate but, at 8 bits, 7x slower than
    # fp32 here. Level 4 multiplies in integers on the optimized kernels, quantizing activations per block of 128
    # values rather than per tensor, so an outlier only coarsens its own block.
    "weightonly8-level4": {"bits": 8, "ops": ("MatMul",), "accuracy_level": 4},
    "weightonly4-gather-level4": {"bits": 4, "ops": ("MatMul", "Gather"), "accuracy_level": 4},
}


def down_projections() -> list[str]:
    """The 24 MLP down projections' MatMul nodes: the ones whose weight is 4864 x 896."""
    import onnx

    model = onnx.load(NTEE_DIR / "fp32" / "model.onnx", load_external_data=False)
    shapes = {i.name: list(i.dims) for i in model.graph.initializer}
    return [n.name for n in model.graph.node if n.op_type == "MatMul" and shapes.get(n.input[1]) == [4864, 896]]


def quantize_weight_only(name: str) -> None:
    from onnxruntime.quantization.matmul_nbits_quantizer import MatMulNBitsQuantizer

    out = NTEE_DIR / f"int8-{name}" / "model.onnx"
    out.parent.mkdir(exist_ok=True)
    settings = WEIGHT_ONLY[name]
    quantizer = MatMulNBitsQuantizer(
        str(NTEE_DIR / "fp32" / "model.onnx"), bits=settings["bits"], block_size=128, is_symmetric=True,
        op_types_to_quantize=settings["ops"], accuracy_level=settings.get("accuracy_level"),
    )  # fmt: skip
    quantizer.process()
    quantizer.model.save_model_to_file(str(out), use_external_data_format=False)
    print(f"{name}: {out.stat().st_size / 1e6:,.1f} MB")


def quantize(name: str) -> None:
    if name in WEIGHT_ONLY:
        return quantize_weight_only(name)
    out = NTEE_DIR / f"int8-{name}" / "model.onnx"
    out.parent.mkdir(exist_ok=True)
    settings = VARIANTS[name]
    quantize_dynamic(
        NTEE_DIR / "fp32" / "model.onnx", out, weight_type=settings.get("weight_type", QuantType.QInt8),
        op_types_to_quantize=["MatMul"], per_channel=settings["per_channel"],
        nodes_to_exclude=down_projections() if settings["nodes_to_exclude"] == "down" else settings["nodes_to_exclude"],
        reduce_range=settings.get("reduce_range", False),
    )  # fmt: skip
    print(f"{name}: {out.stat().st_size / 1e6:,.1f} MB")


def compare(logits: np.ndarray, reference: np.ndarray) -> dict:
    return {
        "same_top1": int((logits.argmax(-1) == reference.argmax(-1)).sum()),
        "rows": len(reference),
        "max_abs_diff": round(float(np.abs(logits - reference).max()), 4),
        "mean_abs_diff": round(float(np.abs(logits - reference).mean()), 4),
    }


def check(names: list[str]) -> dict:
    info, data, _ = load_inputs()
    tokenizer = make_tokenizer(NTEE_DIR / "tokenizer.json", info)
    feeds = encode(tokenizer, [r["mission"] for r in data["rows"][: len(info["check_rows"])]])
    logits = {}
    for name in ["fp32", "int8", *(f"int8-{n}" for n in names)]:
        path = NTEE_DIR / name / "model.onnx"
        if path.exists():
            logits[name] = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]).run(None, feeds)[0]
    result = {
        name: {"file_mb": round(sum(p.stat().st_size for p in (NTEE_DIR / name).iterdir()) / 1e6, 1),
               **compare(values, logits["fp32"])}
        for name, values in logits.items() if name != "fp32"
    }  # fmt: skip
    for name, row in result.items():
        print(f"{name:28} {row}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--variants", default=",".join([*VARIANTS, *WEIGHT_ONLY]))
    args = parser.parse_args()
    names = args.variants.split(",")
    if not args.check_only:
        for name in names:
            quantize(name)
    result = check(names)
    from paths import RESULTS_DIR

    (RESULTS_DIR / "classifier-int8-check.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
