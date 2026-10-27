import paths


def test_directories_live_inside_the_project():
    assert paths.MODELS_DIR.parent == paths.ROOT
    assert paths.RESULTS_DIR.parent == paths.ROOT


def test_model_files_are_gitignored():
    """Quantized models are hundreds of MB: they're downloaded or exported again, never committed."""
    ignored = (paths.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "models/" in ignored
    assert "*.gguf" in ignored
    assert "*.onnx" in ignored
