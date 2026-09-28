import pytest

import prompts

PROMPT_NAMES = [d.name for d in prompts.PROMPTS_DIR.iterdir() if (d / "manifest.json").exists()]


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_published_versions_are_unchanged(name):
    """A registered version must never be edited: its results in the eval history would stop
    describing the text in the file. Add a new version instead."""
    for version, info in prompts.manifest(name)["versions"].items():
        actual = prompts.fingerprint(prompts.read_template(name, version))
        assert actual == info["sha256"], (
            f"prompts/{name}/{version}.txt changed after it was registered. "
            f"Revert it and create a new version file instead."
        )


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_every_version_file_is_registered_and_active_exists(name):
    manifest = prompts.manifest(name)
    files = {p.stem for p in (prompts.PROMPTS_DIR / name).glob("*.txt")}
    assert files == set(manifest["versions"]), "every version file needs a manifest entry, and vice versa"
    assert manifest["active"] in manifest["versions"]


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_answer_templates_have_their_placeholders(name):
    for version in prompts.manifest(name)["versions"]:
        text = prompts.load(name, version).format(context="CTX", question="QST")
        assert "CTX" in text and "QST" in text


def test_active_version_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("RAG_PROMPT_ANSWER", "v1")
    assert prompts.load("answer").version == "v1"


def test_fingerprint_ignores_windows_line_endings(tmp_path, monkeypatch):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "unix.txt").write_bytes(b"line one\nline two\n")
    (tmp_path / "demo" / "windows.txt").write_bytes(b"line one\r\nline two\r\n")
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)

    unix, windows = prompts.read_template("demo", "unix"), prompts.read_template("demo", "windows")
    assert unix == windows == "line one\nline two"
