"""Versioned prompt templates, stored as files: prompts/<name>/<version>.txt.

Published versions are immutable. prompts/<name>/manifest.json records each version's SHA-256
fingerprint, and tests/test_prompts.py fails if a version file changes after it was registered.
To change a prompt: add a new version file, register it in the manifest, compare it against the
current one with eval_generation.py, and only then switch "active". That way every logged answer,
trace, and eval result points at the exact text that produced it.

The active version can be overridden per process for experiments, e.g. RAG_PROMPT_ANSWER=v2.
"""

import hashlib
import json
import os
import pathlib
from dataclasses import dataclass

PROMPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    template: str
    sha256: str

    @property
    def id(self) -> str:
        return f"{self.name}/{self.version}"

    def format(self, **values) -> str:
        return self.template.format(**values)


def fingerprint(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def read_template(name: str, version: str) -> str:
    raw = (PROMPTS_DIR / name / f"{version}.txt").read_text(encoding="utf-8")
    # Normalize line endings so the fingerprint is the same on Windows (git may check files out
    # with CRLF) and on the Linux CI runner. The file's final newline isn't part of the prompt.
    return raw.replace("\r\n", "\n").removesuffix("\n")


def manifest(name: str) -> dict:
    return json.loads((PROMPTS_DIR / name / "manifest.json").read_text(encoding="utf-8"))


def load(name: str, version: str | None = None) -> Prompt:
    """Load a prompt: the given version, else the RAG_PROMPT_<NAME> override, else the manifest's active one."""
    version = version or os.environ.get(f"RAG_PROMPT_{name.upper()}") or manifest(name)["active"]
    template = read_template(name, version)
    return Prompt(name=name, version=version, template=template, sha256=fingerprint(template))
