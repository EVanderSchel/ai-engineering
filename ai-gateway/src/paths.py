"""Where the project keeps its files. models/ holds self-hosted model files (gitignored, re-downloadable); results
worth keeping (benchmarks, load tests, eval runs) go under results/ and are committed."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"  # GGUF and ONNX files (gitignored)
RESULTS_DIR = ROOT / "results"
