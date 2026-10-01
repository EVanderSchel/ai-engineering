import paths


def test_data_directories_live_inside_the_project():
    assert paths.DATA_DIR.parent == paths.ROOT
    assert paths.RAW_DIR.parent == paths.DATA_DIR
    assert paths.GOLD_DIR.parent == paths.DATA_DIR


def test_downloaded_files_are_gitignored():
    """Raw IRS downloads are large and re-downloadable, so they must never be committed."""
    ignored = (paths.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/raw/" in ignored
