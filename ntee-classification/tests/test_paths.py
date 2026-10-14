import paths


def test_data_directories_live_inside_the_project():
    assert paths.DATA_DIR.parent == paths.ROOT
    assert paths.RAW_DIR.parent == paths.DATA_DIR


def test_downloads_and_model_weights_are_gitignored():
    """Raw IRS downloads are hundreds of MB and model weights are GBs: neither belongs in the repo."""
    ignored = (paths.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/raw/" in ignored
    assert "models/" in ignored
