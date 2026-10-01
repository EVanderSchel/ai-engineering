import pytest

import prompts

PROMPT_NAMES = [d.name for d in prompts.PROMPTS_DIR.iterdir() if (d / "manifest.json").exists()]


def test_both_prompts_are_registered():
    assert {"extract", "extract_retry"} <= set(PROMPT_NAMES)


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_published_versions_are_unchanged(name):
    """A registered version must never be edited: saved results would stop describing the text in
    the file. Add a new version instead."""
    for version, info in prompts.manifest(name)["versions"].items():
        actual = prompts.fingerprint(prompts.read_template(name, version))
        assert actual == info["sha256"], f"prompts/{name}/{version}.txt changed after it was registered"


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_every_version_file_is_registered_and_active_exists(name):
    manifest = prompts.manifest(name)
    assert {p.stem for p in (prompts.PROMPTS_DIR / name).glob("*.txt")} == set(manifest["versions"])
    assert manifest["active"] in manifest["versions"]


def test_retry_templates_have_their_placeholder():
    for version in prompts.manifest("extract_retry")["versions"]:
        assert "PROBLEMS" in prompts.load("extract_retry", version).format(problems="PROBLEMS")


def test_active_version_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("IRS990_PROMPT_EXTRACT", "v1")
    assert prompts.load("extract").version == "v1"


def test_fingerprint_ignores_windows_line_endings(tmp_path, monkeypatch):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "unix.txt").write_bytes(b"line one\nline two\n")
    (tmp_path / "demo" / "windows.txt").write_bytes(b"line one\r\nline two\r\n")
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    assert prompts.read_template("demo", "unix") == prompts.read_template("demo", "windows") == "line one\nline two"
