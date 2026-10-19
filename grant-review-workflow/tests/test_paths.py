import paths


def test_data_directories_live_inside_the_project():
    assert paths.DATA_DIR.parent == paths.ROOT
    assert paths.RAW_DIR.parent == paths.DATA_DIR


def test_downloads_and_build_output_are_gitignored():
    """Raw IRS downloads are hundreds of MB, and CDK output is rebuilt on every synth: neither belongs in
    the repo."""
    ignored = (paths.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/raw/" in ignored
    assert "cdk.out/" in ignored
    assert "node_modules/" in ignored
