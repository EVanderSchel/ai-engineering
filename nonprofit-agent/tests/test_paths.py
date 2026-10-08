import paths


def test_data_directories_live_inside_the_project():
    assert paths.DATA_DIR.parent == paths.ROOT
    assert paths.CACHE_DIR.parent == paths.DATA_DIR


def test_cached_api_responses_are_gitignored():
    """Cached responses are re-downloadable and can be large, so they must never be committed."""
    ignored = (paths.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/cache/" in ignored
