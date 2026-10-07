"""Step 7: Part I extraction for many filings at once, with the Message Batches API.

extract.py sends one request and waits for it before sending the next. A batch hands Anthropic every
request at once; they're processed in the background (most batches finish within an hour, all within
24) at half the price. The trade is latency for cost and throughput: right for a backlog of returns,
wrong for a person waiting on one.

    python src/batch.py                    # submit every dev filing as one batch, then wait for it
    python src/batch.py --no-wait          # submit and exit; come back later with --resume
    python src/batch.py --resume data/runs/<run>   # collect a batch submitted earlier

Each request is the same one extract.py sends (prompt, schema, page image, cached prompt), keyed by
the filing's object ID. Results are saved in the same run format, so evaluate.py scores them as usual.
The run directory also holds batch_state.json (the batch ID and what has been collected), so an
interrupted job resumes without submitting, or paying for, anything twice.
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
    page_block,
)
from paths import GOLD_DIR
from schema import Form990PartI, as_answer

BATCH_DISCOUNT = 0.5  # every token in a batch costs half the standard price
POLL_SECONDS = 30
STATE = "batch_state.json"


def request(object_id: str, pdf, input_kind: str = INPUT, model: str = MODEL) -> dict:
    """One batch request: the same request extract.py makes, with structured outputs spelled out (the
    SDK's parse() helper isn't available for batches, so the schema goes in output_config and the JSON
    that comes back is validated with the same Pydantic model)."""
    system = prompts.load("extract")
    return {
        "custom_id": object_id,
        "params": {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": _system(system.template, cache=True),
            "messages": [_first_message(page_block(pdf, input_kind))],
            "output_config": {"format": {"type": "json_schema", "schema": transform_schema(Form990PartI)}},
        },
    }


def record_from_result(result, model: str, input_kind: str, scan: str | None) -> dict:
    """A run record (the same shape extract.py writes) from one batch result."""
    prompt = prompts.load("extract")
    extraction = Extraction(
        answer=None,
        problems=[],
        model=model,
        prompt=prompt.id,
        prompt_sha256=prompt.sha256,
        input=input_kind,
        cache=True,
        scan=scan,
    )
    outcome = result.result
    if outcome.type != "succeeded":
        # errored (bad request or server error), canceled, or expired: no answer from this batch
        error = getattr(getattr(outcome, "error", None), "error", None)
        detail = f": {getattr(error, 'type', '')} {getattr(error, 'message', '')}".rstrip() if error else ""
        extraction.problems = [f"batch request {outcome.type}{detail}"]
        extraction.attempts.append(Attempt(outcome.type, None, extraction.problems, 0.0))
    else:
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
    cost = extraction.usage.cost(model) * BATCH_DISCOUNT
    return {"object_id": result.custom_id, "seconds": None, "cost_usd": cost, "batch": True, **asdict(extraction)}


# --- Job state -------------------------------------------------------------------------------------


def _load_state(run_dir: pathlib.Path) -> dict:
    return json.loads((run_dir / STATE).read_text(encoding="utf-8"))


def _save_state(run_dir: pathlib.Path, state: dict) -> None:
    (run_dir / STATE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")


def _save_summary(run_dir: pathlib.Path, state: dict) -> None:
    records = [json.loads((run_dir / f"{i}.json").read_text(encoding="utf-8")) for i in state["collected"]]
    total = Usage()
    for r in records:
        total.add(Usage(**r["usage"]))
    summary = {
        "run": run_dir.name,
        "complete": len(state["collected"]) == len(state["object_ids"]),
        "filings": len(state["collected"]),
        "of": len(state["object_ids"]),
        "error": None,
        "batch": True,
        "batch_ids": [b["id"] for b in state["batches"]],
        "submitted_at": state["submitted_at"],
        "ended_at": state.get("ended_at"),
        "cost_usd": total.cost(state["model"]) * BATCH_DISCOUNT,
        "usage": asdict(total),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")


def submit(client, object_ids: list[str], *, model: str, input_kind: str, scan: str | None) -> pathlib.Path:
    """Create the run directory, submit one batch for every filing, and record its ID before anything else
    can go wrong, so a crash after this point never leads to a second, paid submission."""
    version = prompts.load("extract").version
    run_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{model}_{version}_{input_kind}_cache"
    run_id += (f"_scan-{scan}" if scan else "") + "_batch"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    requests = [request(i, scans.source_pdf(i, scan), input_kind, model) for i in object_ids]
    batch = client.messages.batches.create(requests=requests)
    state = {
        "model": model,
        "input": input_kind,
        "scan": scan,
        "object_ids": object_ids,
        "submitted_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "batches": [{"id": batch.id, "object_ids": object_ids}],
        "collected": [],
    }
    _save_state(run_dir, state)
    _save_summary(run_dir, state)
    print(f"Submitted batch {batch.id}: {len(object_ids)} filings -> {run_dir}")
    return run_dir


def collect(client, run_dir: pathlib.Path, *, wait: bool) -> bool:
    """Save the results of every ended batch of a job; True once the whole job is collected. Results
    already saved are skipped, so collecting twice (or after a crash) changes nothing."""
    state = _load_state(run_dir)
    for entry in state["batches"]:
        if set(entry["object_ids"]) <= set(state["collected"]):
            continue
        while True:
            batch = client.messages.batches.retrieve(entry["id"])
            counts = batch.request_counts
            if batch.processing_status == "ended" or not wait:
                break
            print(f"  {entry['id']}: {counts.processing} processing, {counts.succeeded} succeeded so far")
            time.sleep(POLL_SECONDS)
        if batch.processing_status != "ended":
            print(f"  {entry['id']} is still {batch.processing_status}: run again with --resume {run_dir}")
            continue
        for result in client.messages.batches.results(entry["id"]):
            if result.custom_id in state["collected"]:
                continue
            record = record_from_result(result, state["model"], state["input"], state["scan"])
            (run_dir / f"{result.custom_id}.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            state["collected"].append(result.custom_id)
        state["ended_at"] = batch.ended_at.isoformat(timespec="seconds") if batch.ended_at else None
        _save_state(run_dir, state)
    _save_summary(run_dir, state)
    return len(state["collected"]) == len(state["object_ids"])


# --- Command line ----------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--limit", type=int, help="only the first N filings of the split (default: all)")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    parser.add_argument("--input", default=INPUT, help="how to send page 1: image-<pixels> or pdf")
    parser.add_argument("--scan", choices=sorted(scans.LEVELS), help="read a simulated scan")
    parser.add_argument("--resume", type=pathlib.Path, help="collect a job submitted earlier")
    parser.add_argument("--no-wait", dest="wait", action="store_false", help="don't wait for the batch to end")
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
        run_dir = args.resume
    else:
        with (GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
            object_ids = [row["object_id"] for row in csv.DictReader(f) if row["split"] == args.split][: args.limit]
        run_dir = submit(client, object_ids, model=args.model, input_kind=args.input, scan=args.scan)

    started = time.monotonic()
    if not collect(client, run_dir, wait=args.wait):
        return
    print(f"Collected in {time.monotonic() - started:.0f}s of waiting\n")
    score = evaluate.score_run(run_dir)
    evaluate.record(score)
    print(evaluate.report(score))


if __name__ == "__main__":
    main()
