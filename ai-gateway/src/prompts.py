"""The prompt registry (step 9): named system prompts as versioned, immutable files, used by reference.

    prompts/<name>/<version>.txt     the template; once registered, never edited
    prompts/<name>/manifest.json     {"active": "v2", "versions": {"v1": {"sha256": ..., "created": ..., "notes": ...}}}

    uv run python src/prompts.py register ntee-classify v2 --notes "what changed and why"   # records the fingerprint
    uv run python src/prompts.py activate ntee-classify v2     # after the eval canary has scored it
    uv run python src/prompts.py list

Plain language: instead of every app pasting its own copy of a prompt into each request, the gateway keeps the
prompt, and apps name it: {"gateway": {"prompt": "ntee-classify", "variables": {"mission": "..."}}}. Changing a
prompt means adding a new version file, not editing one, so every logged answer, usage row and eval result points
at the exact text that produced it, and the eval canary (step 9) can score a new version before it becomes active.
Pinning a version ("ntee-classify@v1") keeps an app on it whatever is active. The pattern comes from the rag project.

Templates use $name placeholders (Python's string.Template), not {name}, so a prompt can contain JSON examples
without escaping their braces. `$$` is a literal dollar sign.

Variable values come from the caller, so they're as untrusted as a user message: the guardrails screen them like
one (guardrails.check_input), and they're never logged.
"""

import dataclasses
import hashlib
import json
import pathlib
import re
import string

PROMPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "prompts"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")  # a prompt's name: lowercase words joined by hyphens
VERSION = re.compile(r"v[1-9][0-9]{0,3}")  # v1, v2, ... in the order they were added
REFERENCE = re.compile(rf"(?P<name>{NAME.pattern})(@(?P<version>{VERSION.pattern}))?")


class PromptError(ValueError):
    """A request named a prompt it can't use. `code` and `param` become the 4xx error's."""

    def __init__(self, message: str, code: str, status: int = 400, param: str = "gateway.prompt"):
        super().__init__(message)
        self.message, self.code, self.status, self.param = message, code, status, param


@dataclasses.dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    template: str
    sha256: str

    @property
    def ref(self) -> str:
        """What the log line, trace and usage ledger record: name@version."""
        return f"{self.name}@{self.version}"

    @property
    def placeholders(self) -> set[str]:
        return set(string.Template(self.template).get_identifiers())

    def render(self, variables: dict[str, str]) -> str:
        """The system prompt with every placeholder filled. Raises PromptError naming what's missing or unknown,
        rather than sending a prompt with a gap or ignoring a value the caller meant to be used."""
        missing = sorted(self.placeholders - variables.keys())
        if missing:
            raise PromptError(
                f"prompt {self.ref} needs these variables: {', '.join(missing)}", "prompt_variables_missing",
                param="gateway.variables",
            )  # fmt: skip
        unknown = sorted(variables.keys() - self.placeholders)
        if unknown:
            raise PromptError(
                f"prompt {self.ref} has no variables named {', '.join(unknown)}", "prompt_variables_unknown",
                param="gateway.variables",
            )  # fmt: skip
        return string.Template(self.template).substitute(variables)


def fingerprint(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def read_template(path: pathlib.Path) -> str:
    """The template as registered: line endings normalized (git may check files out with CRLF on Windows and LF on
    Linux) and the file's final newline dropped, so the fingerprint is the same everywhere."""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").removesuffix("\n")


class Registry:
    """Every registered prompt, read and checked once at startup. A version whose text doesn't match its recorded
    fingerprint, or a manifest naming a missing file, stops the gateway from starting: serving a prompt that isn't
    what was evaluated is worse than not starting."""

    def __init__(self, directory: pathlib.Path = PROMPTS_DIR):
        self.prompts: dict[tuple[str, str], Prompt] = {}
        self.active: dict[str, str] = {}
        if not directory.is_dir():
            return  # no prompts registered: requests that name one get a 404
        for folder in sorted(p for p in directory.iterdir() if p.is_dir()):
            name = folder.name
            if not NAME.fullmatch(name):
                raise RuntimeError(f"prompt folder {name!r}: names are lowercase letters, digits and hyphens")
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            for version, entry in manifest["versions"].items():
                if not VERSION.fullmatch(version):
                    raise RuntimeError(f"prompt {name}: version {version!r} isn't v1, v2, ...")
                template = read_template(folder / f"{version}.txt")
                if fingerprint(template) != entry["sha256"]:
                    raise RuntimeError(
                        f"prompt {name}@{version} doesn't match its registered fingerprint: published versions are "
                        "never edited (add a new version instead)"
                    )
                self.prompts[(name, version)] = Prompt(name, version, template, entry["sha256"])
            unregistered = {p.stem for p in folder.glob("*.txt")} - manifest["versions"].keys()
            if unregistered:
                raise RuntimeError(f"prompt {name}: files not in its manifest: {sorted(unregistered)}")
            if manifest["active"] not in manifest["versions"]:
                raise RuntimeError(f"prompt {name}: active version {manifest['active']!r} isn't registered")
            self.active[name] = manifest["active"]

    def names(self) -> list[str]:
        return sorted(self.active)

    def resolve(self, reference: str) -> Prompt:
        """ "name" (the active version) or "name@v2" (that version). Raises PromptError (404) if there's none."""
        match = REFERENCE.fullmatch(reference)
        if match is None:
            raise PromptError(
                f"{reference!r} isn't a prompt reference: use a name, or name@version (e.g. ntee-classify@v1)",
                "invalid_prompt_reference",
            )  # fmt: skip
        name, version = match["name"], match["version"]
        if name not in self.active:
            raise PromptError(f"no prompt named {name!r} is registered", "prompt_not_found", status=404)
        version = version or self.active[name]
        if (name, version) not in self.prompts:
            raise PromptError(f"prompt {name!r} has no version {version}", "prompt_not_found", status=404)
        return self.prompts[(name, version)]


def apply(request, prompt: Prompt):
    """The request with the rendered prompt as its system message (a schemas.ChatRequest in, a copy out).

    A request that names a prompt may not bring its own system or developer message: the system prompt is exactly
    the registered text, which is what the eval canary scored. App-specific instructions belong in a new version or
    in variables."""
    from schemas import Message  # here, not at the top: schemas doesn't need the registry

    if any(m.role in ("system", "developer") for m in request.messages):
        raise PromptError(
            "with gateway.prompt, the system prompt comes from the registry: send no system or developer message",
            "prompt_conflicts_with_system_message", param="messages",
        )  # fmt: skip
    system = Message(role="system", content=prompt.render(request.gateway.variables))
    return request.model_copy(update={"messages": [system, *request.messages]})


def register(name: str, version: str, notes: str, directory: pathlib.Path = PROMPTS_DIR, today=None) -> str:
    """Records a new version file's fingerprint in its manifest (creating the manifest for a new prompt, with this
    version active). Refuses to re-register a version: published versions don't change."""
    import datetime as dt

    if not NAME.fullmatch(name) or not VERSION.fullmatch(version):
        raise SystemExit("names are lowercase letters, digits and hyphens; versions are v1, v2, ...")
    folder = directory / name
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"active": version, "versions": {}}
    if version in manifest["versions"]:
        raise SystemExit(f"{name}@{version} is already registered; add a new version instead of changing it")
    sha = fingerprint(read_template(folder / f"{version}.txt"))
    created = (today or dt.date.today()).isoformat()
    manifest["versions"][version] = {"sha256": sha, "created": created, "notes": notes}
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return sha


def activate(name: str, version: str, directory: pathlib.Path = PROMPTS_DIR) -> None:
    path = directory / name / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if version not in manifest["versions"]:
        raise SystemExit(f"{name}@{version} isn't registered")
    manifest["active"] = version
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register", help="record a new version file's fingerprint")
    add.add_argument("name")
    add.add_argument("version")
    add.add_argument("--notes", required=True, help="what this version changes, and why")
    use = commands.add_parser("activate", help="make a registered version the one requests get by default")
    use.add_argument("name")
    use.add_argument("version")
    commands.add_parser("list")
    args = parser.parse_args()
    if args.command == "register":
        print(f"registered {args.name}@{args.version} sha256 {register(args.name, args.version, args.notes)}")
    elif args.command == "activate":
        activate(args.name, args.version)
        print(f"{args.name}: active version is now {args.version}")
    else:
        registry = Registry()
        for name in registry.names():
            versions = sorted(v for n, v in registry.prompts if n == name)
            print(f"{name}: active {registry.active[name]}; versions {', '.join(versions)}")


if __name__ == "__main__":
    main()
