"""Download the models the image needs, so containers never fetch them at runtime.

The Dockerfile copies only this file and model_names.py before running it. Because neither
changes when application code changes, Docker keeps this step cached across builds instead of
re-downloading ~170 MB of models on every commit.
"""

from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
from sentence_transformers import CrossEncoder

from model_names import RERANKER_MODEL

# Chroma's default embedding model (all-MiniLM-L6-v2, ONNX) downloads on first use.
ONNXMiniLM_L6_V2()(["download"])
CrossEncoder(RERANKER_MODEL)
print("Models downloaded")
