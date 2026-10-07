"""Step 7: Part I extraction for many filings at once, with the Message Batches API.

extract.py sends one request and waits for it before sending the next. A batch hands Anthropic many
requests at once; they're processed in the background (most batches finish within an hour, all within
24) at half the price. The trade is latency for cost and throughput: right for a backlog of returns,
wrong for a person waiting on one.

    python src/batch.py                    # every dev filing: submit, wait, collect, score
    python src/batch.py --no-wait          # submit and exit; come back later with --resume
    python src/batch.py --resume data/runs/<run>   # carry on with a job started earlier
    python src/batch.py --warm             # experiment: fill the cache first (made caching worse)

Every filing moves through the same steps as in extract.py, a batch at a time:
- its first request (the one extract.py makes, keyed by the filing's object ID);
- if the answer breaks the form's arithmetic, one retry in the same conversation, in a later batch;
- if a request errors (other than being invalid), expires, or is canceled, it's sent again, up to
  MAX_TRIES times;
and filings are packed into batches under the API's size limit (each carries a page image).

Everything is recorded in the run directory's batch_state.json as it happens: a batch is written down
the moment it's created and a result the moment it arrives, so an interrupted job resumes without
submitting, or paying for, anything twice. Results are saved in the usual run format, so evaluate.py
scores them.
"""

import argparse
import csv
import datetime
import json
import pathlib
import time
from dataclasses import asdict

import anthropic
from anthropic.lib._parse._transform import transform_schema
from pydantic import ValidationError

import checks
import evaluate
import prompts
import scans
from extract import (
    INPUT,
    MAX_ATTEMPTS,
    MAX_TOKENS,
    MODEL,
    PRICES,
    RUNS_DIR,
    Attempt,
    Extraction,
    Usage,
    _first_message,
    _system,
    check_input,
    extract,
    page_block,
)
from paths import GOLD_DIR
from schema import Form990PartI, as_answer

BATCH_DISCOUNT = 0.5  # every token in a batch costs half the standard price
POLL_SECONDS = 30
STATE = "batch_state.json"
MAX_TRIES = 3  # sends of the same request when it errors, expires, or is canceled
# The API takes up to 100,000 requests or 256 MB per batch. Each request carries a page image (about
# 0.5 MB as base64), so size is the real limit; stay well under it.
MAX_BATCH_REQUESTS = 100_000
MAX_BATCH_BYTES = 200 * 1024 * 1024


# --- Requests --------------------------------------------------------------------------------------


def _params(messages: list[dict], model: str) -> dict:
    """extract.py's request, with structured outputs spelled out: the SDK's parse() helper doesn't cover
    batches, so the schema goes in output_config and the JSON that comes back is validated with the
    same Pydantic model."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": _system(prompts.load("extract").template, cache=True),
        "messages": messages,
        "output_config": {"format": {"type": "json_schema", "schema": transform_schema(Form990PartI)}},
    }


def first_request(object_id: str, pdf, input_kind: str = INPUT, model: str = MODEL) -> dict:
    return {"custom_id": object_id, "params": _params([_first_message(page_block(pdf, input_kind))], model)}


def retry_request(object_id: str, pdf, history: list[dict], problems: list[str], input_kind: str, model: str) -> dict:
    """The follow-up extract.py sends when the answer breaks the arithmetic: Claude's reply, then the
    broken rules, in the same conversation."""
    retry = prompts.load("extract_retry").format(problems="\n".join(f"- {p}" for p in problems))
    messages = [
        _first_message(page_block(pdf, input_kind)),
        {"role": "assistant", "content": history},
        {"role": "user", "content": retry},
    ]
    return {"custom_id": object_id, "params": _params(messages, model)}


def chunks(requests, max_requests: int, max_bytes: int):
    """Group requests into batches under both limits. Takes requests one at a time (they're built
    lazily), so a large job never holds more than one batch's worth of page images in memory."""
    group, size = [], 0
    for req in requests:
        req_size = len(json.dumps(req))
        if group and (len(group) >= max_requests or size + req_size > max_bytes):
            yield group
            group, size = [], 0
        group.append(req)
        size += req_size
    if group:
        yield group


# --- Results ---------------------------------------------------------------------------------------


def _extraction(state: dict) -> Extraction:
    prompt = prompts.load("extract")
    return Extraction(
        answer=None,
        problems=[],
        model=state["model"],
        prompt=prompt.id,
        prompt_sha256=prompt.sha256,
        input=state["input"],
        cache=True,
        scan=state["scan"],
    )


def read_result(result, state: dict) -> tuple[str, Extraction | None, list[dict] | None]:
    """What one batch result means: ("ok", extraction, history) when Claude answered (history is its
    reply, for a retry), ("retry", None, None) for a failure worth sending again, or ("failed",
    extraction, None) for one that isn't (an invalid request)."""
    outcome = result.result
    extraction = _extraction(state)
    if outcome.type != "succeeded":
        error = getattr(getattr(outcome, "error", None), "error", None)
        if outcome.type == "errored" and getattr(error, "type", "") == "invalid_request_error":
            extraction.problems = [f"batch request invalid: {getattr(error, 'message', '')}"]
            extraction.attempts.append(Attempt("invalid_request", None, extraction.problems, 0.0))
            return "failed", extraction, None
        return "retry", None, None
    message = outcome.message
    extraction.usage.add(message.usage)
    if message.stop_reason in ("refusal", "max_tokens"):
        extraction.problems = [f"Claude stopped without an answer ({message.stop_reason})"]
    else:
        text = next(b.text for b in message.content if b.type == "text")
        try:
            extraction.answer = as_answer(Form990PartI.model_validate_json(text))
            extraction.problems = checks.problems(extraction.answer)
        except ValidationError as e:
            extraction.problems = [f"the answer didn't match the schema: {e.error_count()} error(s)"]
    extraction.attempts.append(Attempt(message.stop_reason, extraction.answer, extraction.problems, 0.0))
    history = [{"type": "text", "text": b.text} if b.type == "text" else b.to_dict() for b in message.content]
    return "ok", extraction, history


def _record(extraction: Extraction, object_id: str, *, batch: bool = True) -> dict:
    cost = extraction.usage.cost(extraction.model) * (BATCH_DISCOUNT if batch else 1)
    return {"object_id": object_id, "seconds": None, "cost_usd": cost, "batch": batch, **asdict(extraction)}


def _merge(earlier: dict, later: Extraction) -> Extraction:
    """A retry's extraction with the first attempt's tokens and attempt added in front."""
    later.usage.add(Usage(**earlier["usage"]))
    later.attempts = [Attempt(**a) for a in earlier["attempts"]] + later.attempts
    return later


# --- The job ---------------------------------------------------------------------------------------


class Job:
    """One run directory's batch job, with its state on disk after every change."""

    def __init__(self, run_dir: pathlib.Path):
        self.run_dir = run_dir
        self.state = json.loads((run_dir / STATE).read_text(encoding="utf-8"))

    @classmethod
    def create(cls, object_ids: list[str], *, model: str, input_kind: str, scan: str | None) -> Job:
        version = prompts.load("extract").version
        run_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{model}_{version}_{input_kind}_cache"
        run_id += (f"_scan-{scan}" if scan else "") + "_batch"
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True)
        state = {
            "model": model,
            "input": input_kind,
            "scan": scan,
            "object_ids": object_ids,
            "started_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "ended_at": None,
            "batches": [],
            # per filing: which request it needs next, how often that request has been sent, the batch
            # it's in, whether it's finished, and for a retry the first attempt and Claude's reply
            "filings": {i: {"step": "first", "tries": 0, "batch": None, "done": False} for i in object_ids},
        }
        (run_dir / STATE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")
        job = cls(run_dir)
        job.save()
        return job

    def save(self) -> None:
        (self.run_dir / STATE).write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8", newline="\n")
        self._save_summary()

    def _save_summary(self) -> None:
        done = [i for i, f in self.state["filings"].items() if f["done"]]
        total, cost = Usage(), 0.0
        for object_id in done:
            record = json.loads((self.run_dir / f"{object_id}.json").read_text(encoding="utf-8"))
            total.add(Usage(**record["usage"]))
            cost += record["cost_usd"]
        summary = {
            "run": self.run_dir.name,
            "complete": len(done) == len(self.state["object_ids"]),
            "filings": len(done),
            "of": len(self.state["object_ids"]),
            "error": None,
            "batch": True,
            "batch_ids": [b["id"] for b in self.state["batches"]],
            "started_at": self.state["started_at"],
            "ended_at": self.state["ended_at"],
            "cost_usd": cost,
            "usage": asdict(total),
        }
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")

    def _finish(self, object_id: str, record: dict) -> None:
        (self.run_dir / f"{object_id}.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        filing = self.state["filings"][object_id]
        filing.update(done=True, batch=None)
        filing.pop("carry", None)

    @property
    def complete(self) -> bool:
        return all(f["done"] for f in self.state["filings"].values())

    def warm(self, client) -> None:
        """Send the first unfinished filing on its own, at the standard price: it writes the prompt and
        schema to the cache, so the batch that follows straight after can read them instead."""
        object_id = next(i for i, f in self.state["filings"].items() if not f["done"] and f["batch"] is None)
        pdf = scans.source_pdf(object_id, self.state["scan"])
        result = extract(client, page_block(pdf, self.state["input"]), input_kind=self.state["input"], cache=True)
        result.scan = self.state["scan"]
        self._finish(object_id, _record(result, object_id, batch=False))
        self.save()

    def submit_pending(self, client) -> int:
        """Send every filing that needs a request, in batches under the size limits. Returns how many."""
        pending = [i for i, f in self.state["filings"].items() if not f["done"] and f["batch"] is None]

        def requests():
            for object_id in pending:
                filing = self.state["filings"][object_id]
                pdf = scans.source_pdf(object_id, self.state["scan"])
                if filing["step"] == "first":
                    yield first_request(object_id, pdf, self.state["input"], self.state["model"])
                else:
                    carry = filing["carry"]
                    yield retry_request(
                        object_id,
                        pdf,
                        carry["history"],
                        carry["record"]["problems"],
                        self.state["input"],
                        self.state["model"],
                    )

        for group in chunks(requests(), MAX_BATCH_REQUESTS, MAX_BATCH_BYTES):
            batch = client.messages.batches.create(requests=group)
            ids = [r["custom_id"] for r in group]
            self.state["batches"].append({"id": batch.id, "object_ids": ids, "processed": False})
            for object_id in ids:
                filing = self.state["filings"][object_id]
                filing.update(batch=batch.id, tries=filing["tries"] + 1)
            self.save()  # written down before anything else can go wrong
            print(f"  submitted {batch.id}: {len(ids)} request(s)")
        return len(pending)

    def process(self, client, entry: dict, ended_at) -> None:
        """Read an ended batch's results into the job. Each result is applied once (state is saved per
        batch, and a batch already processed is skipped)."""
        for result in client.messages.batches.results(entry["id"]):
            object_id = result.custom_id
            filing = self.state["filings"][object_id]
            if filing["done"] or filing["batch"] != entry["id"]:
                continue  # already applied
            kind, extraction, history = read_result(result, self.state)
            carry = filing.get("carry")
            if kind == "retry":
                if filing["tries"] < MAX_TRIES:
                    filing["batch"] = None  # send the same request again
                    continue
                if carry:  # the retry kept failing: the first attempt is the answer
                    self._finish(object_id, carry["record"])
                else:
                    extraction = _extraction(self.state)
                    extraction.problems = [f"batch request {result.result.type} {MAX_TRIES} times"]
                    self._finish(object_id, _record(extraction, object_id))
                continue
            if carry:
                extraction = _merge(carry["record"], extraction)
            needs_retry = kind == "ok" and extraction.answer and extraction.problems and not carry
            if needs_retry and MAX_ATTEMPTS > 1:
                record = _record(extraction, object_id)
                filing.update(step="retry", tries=0, batch=None, carry={"record": record, "history": history})
                continue
            self._finish(object_id, _record(extraction, object_id))
        entry["processed"] = True
        if ended_at:
            self.state["ended_at"] = ended_at.isoformat(timespec="seconds")
        self.save()

    def run(self, client, *, wait: bool) -> bool:
        """Submit what's pending, wait for and process ended batches, and repeat until every filing is
        finished. True when the job is complete; False if it stopped because wait is off."""
        while not self.complete:
            self.submit_pending(client)
            open_batches = [b for b in self.state["batches"] if not b["processed"]]
            progressed = False
            for entry in open_batches:
                batch = client.messages.batches.retrieve(entry["id"])
                if batch.processing_status == "ended":
                    self.process(client, entry, batch.ended_at)
                    progressed = True
                else:
                    counts = batch.request_counts
                    print(f"  {entry['id']}: {batch.processing_status}, {counts.processing} processing")
            if self.complete:
                break
            if not progressed:
                if not wait:
                    print(f"Not finished: run again with --resume {self.run_dir}")
                    return False
                time.sleep(POLL_SECONDS)
        return True


# --- Command line ----------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--limit", type=int, help="only the first N filings of the split (default: all)")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    parser.add_argument("--input", default=INPUT, help="how to send page 1: image-<pixels> or pdf")
    parser.add_argument("--scan", choices=sorted(scans.LEVELS), help="read a simulated scan")
    parser.add_argument(
        "--warm",
        action="store_true",
        help="experiment: send the first filing on its own to fill the cache (it didn't help)",
    )
    parser.add_argument("--resume", type=pathlib.Path, help="carry on with a job started earlier")
    parser.add_argument("--no-wait", dest="wait", action="store_false", help="don't wait for batches to end")
    parser.add_argument("--final", action="store_true", help="required for --split test")
    args = parser.parse_args()
    if args.split == "test" and not args.final and not args.resume:
        parser.error("the test split is held out for final scores; add --final if this is one")
    try:
        check_input(args.input)
    except ValueError as e:
        parser.error(str(e))

    client = anthropic.Anthropic()
    if args.resume:
        job = Job(args.resume)
    else:
        with (GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
            object_ids = [row["object_id"] for row in csv.DictReader(f) if row["split"] == args.split][: args.limit]
        job = Job.create(object_ids, model=args.model, input_kind=args.input, scan=args.scan)
        print(f"Job {job.run_dir}")
        if args.warm:
            job.warm(client)

    started = time.monotonic()
    if not job.run(client, wait=args.wait):
        return
    print(f"Finished after {time.monotonic() - started:.0f}s\n")
    score = evaluate.score_run(job.run_dir)
    evaluate.record(score)
    print(evaluate.report(score))


if __name__ == "__main__":
    main()
