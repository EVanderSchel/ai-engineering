"""Names of the models downloaded from the internet. Kept in a module with no dependencies so the
Dockerfile can download the models before copying in the rest of the code (see download_models.py)."""

RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
