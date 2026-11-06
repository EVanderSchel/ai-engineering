"""The eval canary (step 9): scores a route, and the prompt it uses, on questions with known answers, through the
gateway itself, and compares the score with that check's baseline before a change ships.

    uv run python src/canary.py --stub                       # every check on a scripted model: free, no network
    uv run python src/canary.py ntee-fast                    # real models (paid), compared with its baseline
    uv run python src/canary.py ntee-fast --save-baseline    # record this run as the baseline
    uv run python src/canary.py --changed                    # only the checks whose route or prompt has changed
    uv run python src/canary.py --changed --baseline-ref origin/main ntee-fast ntee-smart    # what CI runs

Plain language: before a new prompt version or a different model goes live, it sits the same exam as the current
one did, and has to score about as well. The exam questions have exact answers (an NTEE letter, an ARC choice), so
scoring is a string comparison in code, with no judge model to pay for or to trust.

How it works:
- The checks are in config/canary.yaml: a suite of questions, a route, optionally a registered prompt, a row count.
- The gateway runs in this process (create_app over an in-memory transport), with the repo's routes.yaml and prompt
  registry. So the canary exercises what an app would: prompt rendering, guardrails, translation, fallback.
- Each answer is parsed and scored item by item. A run is compared with the baseline on the items both answered,
  and fails if its accuracy is more than `margin` below the baseline's.
- A check's "subject" is what it tests: the route's candidates (provider, model, settings) and the prompt's version
  and fingerprint. `--changed` runs only the checks whose subject differs from their baseline's, which is what a pull
  request that edits routes.yaml or adds a prompt version needs (stage 3 runs it in CI).
- `--stub` swaps every provider for ScriptedProvider, which answers a fixed share of questions correctly. It
  checks the canary's own plumbing for free, against baselines in results/canary/stub/.

Results: results/canary/runs/<check>.json (the latest run, every item) and results/canary/baselines/<check>.json.
"""

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import httpx2
import yaml
from pydantic import BaseModel, ConfigDict, Field

import app as gateway
import config
import keys
import prompts
from measure_quantization import parse_label, quiz_prompt
from paths import RESULTS_DIR, ROOT
from providers import Completion, Finish, ProviderRequest, StreamEvent, TextDelta, Usage

CHECKS_FILE = ROOT / "config" / "canary.yaml"
DATA_DIR = ROOT / "data" / "self_hosted"
OUT_DIR = RESULTS_DIR / "canary"
KEY = "gw_canary-in-memory-key"
CONCURRENCY = 4  # requests at once: the local route has 4 slots, and Claude's rate limits are far above this
MAX_ERROR_SHARE = 0.05  # more failed requests than this and the run says nothing about quality: "incomplete"
# Room for the answer and for any thinking a model does first. The first live run capped NTEE at 20 tokens, and
# Sonnet 5.5 spent all 20 thinking on 3 of 60 missions, so it answered nothing (finish "length").
MAX_TOKENS = {"ntee": 256, "arc": 10}


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: Literal["ntee", "arc"]
    route: str
    prompt: str | None = None  # a registered prompt reference, "name" or "name@v2"
    rows: int = Field(gt=0)
    temperature: float | None = None  # only for routes whose every candidate takes it (design decision 16)


class Checks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    margin: float = Field(ge=0, le=1)
    checks: dict[str, Check]


def load_checks(path: pathlib.Path = CHECKS_FILE) -> Checks:
    return Checks.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Item:
    id: str
    user: str  # the user message
    gold: str  # the right letter
    labels: str  # every letter that's a valid answer


def load_items(suite: str, rows: int) -> list[Item]:
    """The suite's first `rows` rows, so every run of a check asks the same questions."""
    if suite == "ntee":
        data = json.loads((DATA_DIR / "ntee_dev_sample.json").read_text(encoding="utf-8"))
        labels = "".join(data["groups"])
        selected = data["rows"][:rows]
        items = [Item(r["ein"], f"<mission>{r['mission']}</mission>", r["group"], labels) for r in selected]
    else:
        data = json.loads((DATA_DIR / "arc_easy_sample.json").read_text(encoding="utf-8"))
        items = [Item(r["id"], quiz_prompt(r), r["answer"], "ABCD") for r in data["rows"][:rows]]
    if len(items) < rows:
        raise ValueError(f"suite {suite} has only {len(items)} rows, not {rows}")
    return items


def parse(suite: str, answer: str, labels: str) -> list[str]:
    """The letters an answer gives, most likely first: up to three for NTEE ("B, O, N"), one for ARC ("B")."""
    if suite == "arc":
        label = parse_label(answer, labels)
        return [label] if label else []
    letters = [x for x in re.findall(r"\b([A-Z])\b", answer) if x in labels]
    return list(dict.fromkeys(letters))[:3]


def subject(check: Check, routes: dict[str, config.Route], registry: prompts.Registry) -> dict:
    """What the check tests. Two runs with the same subject differ only by the models' own variation."""
    out = {"route": check.route, "candidates": [c.model_dump(mode="json") for c in routes[check.route].candidates]}
    if check.prompt:
        prompt = registry.resolve(check.prompt)
        out["prompt"] = {"ref": prompt.ref, "sha256": prompt.sha256}
    return out


# --- asking the gateway ---


def make_app(routes: dict[str, config.Route], providers: dict | None, registry: prompts.Registry):
    """The gateway in this process. providers=None: the real ones (Claude API, Bedrock, llama-server)."""
    return gateway.create_app(routes, providers, {keys.key_hash(KEY): "canary"}, prompt_registry=registry)


def client_for(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://canary",
        headers={"authorization": f"Bearer {KEY}"}, timeout=180,
    )  # fmt: skip


async def ask(client: httpx2.AsyncClient, check: Check, item: Item) -> dict:
    body: dict = {
        "model": check.route,
        "messages": [{"role": "user", "content": item.user}],
        "max_tokens": MAX_TOKENS[check.suite],
    }
    if check.prompt:
        body["gateway"] = {"prompt": check.prompt}
    if check.temperature is not None:
        body["temperature"] = check.temperature
    response = await client.post("/v1/chat/completions", json=body)
    data = response.json()
    if response.status_code != 200:
        return {"id": item.id, "gold": item.gold, "error": data["error"].get("code") or str(response.status_code)}
    answer = data["choices"][0]["message"]["content"] or ""
    predicted = parse(check.suite, answer, item.labels)
    return {
        "id": item.id,
        "gold": item.gold,
        "answer": answer,
        "predicted": predicted,
        "correct": predicted[:1] == [item.gold],
        "top3": item.gold in predicted,
        "finish": data["choices"][0]["finish_reason"],  # "length": cut off by MAX_TOKENS
        "model": data["model"],
        "input_tokens": data["usage"]["prompt_tokens"],
        "output_tokens": data["usage"]["completion_tokens"],
    }


async def run_items(client: httpx2.AsyncClient, check: Check, items: list[Item]) -> list[dict]:
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(item: Item) -> dict:
        async with limit:
            return await ask(client, check, item)

    return list(await asyncio.gather(*(one(item) for item in items)))


# --- scoring and comparing (pure, tested) ---


def summarize(results: list[dict], suite: str) -> dict:
    answered = [r for r in results if "error" not in r]
    out = {
        "rows": len(results),
        "answered": len(answered),
        "errors": len(results) - len(answered),
        "accuracy": round(sum(r["correct"] for r in answered) / len(answered), 4) if answered else None,
        "unparseable": sum(not r["predicted"] for r in answered),
        "cut_off": sum(r.get("finish") == "length" for r in answered),
        "input_tokens": sum(r["input_tokens"] for r in answered),
        "output_tokens": sum(r["output_tokens"] for r in answered),
        "models": dict(Counter(r["model"] for r in answered)),  # more than one: a fallback answered some
    }
    if suite == "ntee":
        out["top3"] = round(sum(r["top3"] for r in answered) / len(answered), 4) if answered else None
    return out


def compare(run: dict, baseline: dict | None, margin: float) -> dict:
    """The verdict on a run: pass, fail (accuracy fell more than `margin` below the baseline's), incomplete (too
    many requests failed to say), or no_baseline. Accuracy is compared on the items both runs answered, so a
    failed request doesn't count as a wrong answer, and item by item: `broken` were right in the baseline and are
    wrong now, `fixed` the other way round. Both are reported because an unchanged score can hide a swap."""
    summary = run["summary"]
    if summary["errors"] > MAX_ERROR_SHARE * summary["rows"]:
        return {"verdict": "incomplete", "reason": f"{summary['errors']} of {summary['rows']} requests failed"}
    if baseline is None:
        return {"verdict": "no_baseline"}
    before = {r["id"]: r["correct"] for r in baseline["items"] if "error" not in r}
    now = {r["id"]: r["correct"] for r in run["items"] if "error" not in r}
    both = sorted(before.keys() & now.keys())
    if not both:
        return {"verdict": "incomplete", "reason": "no item answered in both runs"}
    baseline_accuracy = sum(before[i] for i in both) / len(both)
    accuracy = sum(now[i] for i in both) / len(both)
    change = accuracy - baseline_accuracy
    return {
        "verdict": "fail" if change < -margin - 1e-9 else "pass",
        "items_compared": len(both),
        "baseline_accuracy": round(baseline_accuracy, 4),
        "accuracy": round(accuracy, 4),
        "change": round(change, 4),
        "margin": margin,
        "broken": sum(before[i] and not now[i] for i in both),
        "fixed": sum(now[i] and not before[i] for i in both),
        "same_subject": run["subject"] == baseline["subject"],
    }


# --- a scripted model, for --stub and the tests ---


class ScriptedProvider:
    """Answers the canary's questions from the answer key, right on a fixed share of them (`accuracy`), chosen by a
    hash of the question and `seed`, so a run is exactly repeatable. Wrong answers are another valid letter. It
    stands in for every provider, so the gateway path is real and only the model is scripted."""

    supports_sampling = True

    def __init__(self, items: list[Item], accuracy: float = 0.8, seed: int = 0):
        self.key = {item.user: item for item in items}
        self.accuracy, self.seed = accuracy, seed
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    def _answer(self, request: ProviderRequest) -> str:
        item = self.key.get(request.turns[-1].text)
        if item is None:
            return "I can't answer that."
        draw = int(hashlib.sha256(f"{self.seed}:{item.id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        if draw < self.accuracy:
            return item.gold
        return next(label for label in item.labels if label != item.gold)

    async def complete(self, request: ProviderRequest) -> Completion:
        self.calls += 1
        self.requests.append(request)
        text = self._answer(request)
        return Completion(text, "stop", request.model, Usage(input_tokens=100, output_tokens=1))

    async def stream(self, request: ProviderRequest):  # -> AsyncIterator[StreamEvent]
        completion = await self.complete(request)
        events: list[StreamEvent] = [TextDelta(completion.text), Finish("stop", request.model, completion.usage)]
        for event in events:
            yield event


def scripted_providers(scripted: ScriptedProvider) -> dict:
    return {name: scripted for name in ("stub", "anthropic", "bedrock", "local")}


# --- running checks ---


async def run_check(name: str, check: Check, client: httpx2.AsyncClient, subject_: dict, margin: float,
                    baseline: dict | None) -> dict:  # fmt: skip
    items = load_items(check.suite, check.rows)
    results = await run_items(client, check, items)
    run = {
        "check": name,
        "suite": check.suite,
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "subject": subject_,
        "summary": summarize(results, check.suite),
        "items": results,
    }
    run["comparison"] = compare(run, baseline, margin)
    return run


def read_json(path: pathlib.Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def read_baseline(path: pathlib.Path, ref: str | None) -> dict | None:
    """The baseline in the working tree, or as it is at a git ref. CI passes the pull request's base (main), so a PR
    that edits a route and also commits a new baseline is still compared with the old one."""
    if ref is None:
        return read_json(path)
    shown = subprocess.run(
        ["git", "show", f"{ref}:./{path.relative_to(ROOT).as_posix()}"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8",
    )  # fmt: skip
    return json.loads(shown.stdout) if shown.returncode == 0 else None  # not there: a check the PR adds


def write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def line(run: dict) -> str:
    s, c = run["summary"], run["comparison"]
    text = f"{run['check']:<11} {c['verdict']:<11} accuracy {s['accuracy']}"
    if "baseline_accuracy" in c:
        text += f" vs baseline {c['baseline_accuracy']} on {c['items_compared']} ({c['change']:+.3f}, margin "
        text += f"{c['margin']}; {c['broken']} broken, {c['fixed']} fixed)"
    if c.get("reason"):
        text += f": {c['reason']}"
    return text


def skip_reason(subject_: dict, baseline: dict | None, changed: bool, ref: str | None) -> str | None:
    """Why `--changed` leaves a check out, or None to run it. Compared with a git ref (CI), a check with no baseline
    there is new, or the canary itself is: a run could only say "no_baseline", which can't fail, so it isn't paid
    for. The baseline the pull request commits becomes the one to compare with once it's merged."""
    if not changed:
        return None
    if baseline is None:
        return f"new         (no baseline at {ref}: nothing to compare with, not run)" if ref else None
    if baseline["subject"] == subject_:
        return "unchanged   (same route and prompt as its baseline: not run)"
    return None


async def main_async(args) -> int:
    settings = load_checks()
    names = args.checks or list(settings.checks)
    unknown = sorted(set(names) - settings.checks.keys())
    if unknown:
        raise SystemExit(f"no checks named {', '.join(unknown)} in {CHECKS_FILE.name}")
    routes, registry = config.load_routes(), prompts.Registry()
    out = OUT_DIR / "stub" if args.stub else OUT_DIR
    providers = None
    if args.stub:
        everything = [i for n in names for i in load_items(settings.checks[n].suite, settings.checks[n].rows)]
        providers = scripted_providers(ScriptedProvider(everything, accuracy=args.accuracy))
    baselines = {n: read_baseline(out / "baselines" / f"{n}.json", args.baseline_ref) for n in names}
    to_run = []
    for name in names:
        subject_ = subject(settings.checks[name], routes, registry)
        reason = skip_reason(subject_, baselines[name], args.changed, args.baseline_ref)
        if reason:
            print(f"{name:<11} {reason}")
        else:
            to_run.append((name, subject_))
    if to_run and not args.stub and not os.environ.get("ANTHROPIC_API_KEY"):
        names_ = ", ".join(n for n, _ in to_run)
        raise SystemExit(
            f"ANTHROPIC_API_KEY isn't set, and these checks would run with real models: {names_} "
            "(in CI it comes from the AI_GATEWAY_CANARY_ANTHROPIC_API_KEY repository secret)"
        )
    failed = False
    async with client_for(make_app(routes, providers, registry)) as client:
        for name, subject_ in to_run:
            check, baseline = settings.checks[name], baselines[name]
            run = await run_check(name, check, client, subject_, settings.margin, baseline)
            write_json(out / "runs" / f"{name}.json", run)
            print(line(run))
            verdict = run["comparison"]["verdict"]
            if args.save_baseline:
                if verdict == "incomplete":
                    print(f"  not saved as the baseline: {run['comparison']['reason']}")
                    failed = True
                else:
                    write_json(out / "baselines" / f"{name}.json", run)
                    print(f"  saved as the baseline for {name}")
            elif verdict in ("fail", "incomplete"):  # no_baseline: a new check, with nothing to fall below
                failed = True
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("checks", nargs="*", help="check names from config/canary.yaml (default: all)")
    parser.add_argument("--stub", action="store_true", help="a scripted model instead of real ones: free")
    parser.add_argument("--accuracy", type=float, default=0.8, help="with --stub: the scripted model's accuracy")
    parser.add_argument("--save-baseline", action="store_true", help="record each run as its check's baseline")
    parser.add_argument("--baseline-ref", help="compare with the baselines at this git ref (CI: the PR's base)")
    parser.add_argument("--changed", action="store_true", help="skip checks whose subject matches the baseline's")
    args = parser.parse_args()
    os.environ.setdefault("GATEWAY_LOG_LEVEL", "WARNING")  # not a log line per question: the summary says it
    if sys.platform == "win32":  # the gateway's database driver needs the selector loop on Windows (step 4)
        sys.exit(asyncio.run(main_async(args), loop_factory=asyncio.SelectorEventLoop))
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
