"""Versioned prompt templates, stored as files: prompts/<name>/<version>.txt (same scheme as the rag project).

Published versions are immutable. prompts/<name>/manifest.json records each version's SHA-256
fingerprint, and tests/test_prompts.py fails if a version file changes after it was registered.
To change a prompt: add a new version file, register it in the manifest, compare it against the
current one on the dev split, and only then switch "active". Every saved extraction records the
prompt id and fingerprint, so results always point at the exact text that produced them.

The active version can be overridden per process for experiments, e.g. IRS990_PROMPT_EXTRACT=v2.
"""

import hashlib
import json
import os
from dataclasses import dataclass

from paths import ROOT

PROMPTS_DIR = ROOT / "prompts"


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
    # Same fingerprint on Windows (git may check files out with CRLF) and on the Linux CI runner.
    return raw.replace("\r\n", "\n").removesuffix("\n")


def manifest(name: str) -> dict:
    return json.loads((PROMPTS_DIR / name / "manifest.json").read_text(encoding="utf-8"))


def load(name: str, version: str | None = None) -> Prompt:
    """The given version, else the IRS990_PROMPT_<NAME> override, else the manifest's active one."""
    version = version or os.environ.get(f"IRS990_PROMPT_{name.upper()}") or manifest(name)["active"]
    template = read_template(name, version)
    return Prompt(name=name, version=version, template=template, sha256=fingerprint(template))
