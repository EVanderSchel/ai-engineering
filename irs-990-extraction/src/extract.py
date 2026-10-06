"""Baseline extraction: page 1 of a Form 990 (as an image) -> validated Part I fields, with Claude.

How one filing is extracted:
1. Render page 1 of the IRS page-image PDF to a PNG.
2. Ask Claude to transcribe it, with structured outputs: the response is forced to match the schema in
   schema.py (every field present, right type, blank lines listed), and the SDK parses it into a
   Pydantic object.
3. Check the answer against the form's own arithmetic (checks.py). If a rule is broken, Claude misread
   something: show it the broken rules and ask once more, in the same conversation.
4. Record what it cost: tokens from the API's usage report, priced per model.

Each run is saved to data/runs/<run>/ and scored with evaluate.py, which adds it to results/history.csv:

    python src/extract.py                  # every dev filing (21, about $0.50)
    python src/extract.py --limit 3        # a quick trial
"""

import argparse
import base64
import csv
import datetime
import json
import time
from dataclasses import asdict, dataclass, field

import anthropic
import pymupdf

import checks
import evaluate
import prompts
from paths import DATA_DIR, GOLD_DIR, RAW_DIR
from schema import Form990PartI, as_answer

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
MAX_ATTEMPTS = 2  # the first answer, plus one retry when the arithmetic checks fail
RUNS_DIR = DATA_DIR / "runs"
# How page 1 is sent (step 4 compares them): "image-<N>" is a PNG whose longer side is N pixels, "pdf" is
# the page itself as a one-page PDF. Claude Sonnet 5 reads images up to 2576 px on the longer side and
# scales larger ones down; image tokens grow with pixel area (about one per 28x28 pixels).
INPUT = "image-1568"
MAX_LONG_EDGE_PX = 2576

# US dollars per million tokens: (input, output). Cache writes cost 1.25x input and cache reads 0.1x;
# this pipeline doesn't cache, but they're priced in case a later step does.
PRICES = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}


# --- Input ---------------------------------------------------------------------------------------


def page_png(pdf_path, page_number: int = 0, long_edge: int = 1568) -> bytes:
    """One page of a PDF as a PNG, scaled so its longer side is long_edge pixels."""
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_number]
        zoom = long_edge / max(page.rect.width, page.rect.height)
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


def page_pdf(pdf_path, page_number: int = 0) -> bytes:
    """One page of a PDF as a PDF of its own, unchanged (the IRS's scan, at its own resolution)."""
    with pymupdf.open(pdf_path) as doc, pymupdf.open() as single:
        single.insert_pdf(doc, from_page=page_number, to_page=page_number)
        return single.tobytes(garbage=3, deflate=True)  # garbage=3 drops the other pages' images


def _base64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def check_input(input_kind: str) -> int | None:
    """The image's long edge for "image-<N>", None for "pdf"; ValueError for anything else."""
    if input_kind == "pdf":
        return None
    kind, _, size = input_kind.partition("-")
    if kind != "image" or not size.isdigit() or not 0 < int(size) <= MAX_LONG_EDGE_PX:
        raise ValueError(f"unknown input {input_kind!r}: use pdf, or image-<pixels> up to {MAX_LONG_EDGE_PX}")
    return int(size)


def page_block(pdf_path, input_kind: str = INPUT) -> dict:
    """Page 1 as a content block: an image block for "image-<N>", a document block for "pdf"."""
    long_edge = check_input(input_kind)
    if long_edge is None:
        source = {"type": "base64", "media_type": "application/pdf", "data": _base64(page_pdf(pdf_path))}
        return {"type": "document", "source": source}
    source = {"type": "base64", "media_type": "image/png", "data": _base64(page_png(pdf_path, long_edge=long_edge))}
    return {"type": "image", "source": source}


def _first_message(block: dict) -> dict:
    return {"role": "user", "content": [block, {"type": "text", "text": "Transcribe page 1 of this return."}]}


def _as_history(content) -> list[dict]:
    """The assistant's reply, to send back before a retry. Thinking blocks go back unchanged; parsed text
    blocks lose the SDK-only parsed_output field, which the API doesn't accept."""
    return [{"type": "text", "text": b.text} if b.type == "text" else b.to_dict() for b in content]


# --- Cost ----------------------------------------------------------------------------------------


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, usage) -> None:
        for name in asdict(self):
            setattr(self, name, getattr(self, name) + (getattr(usage, name, 0) or 0))

    def cost(self, model: str) -> float:
        input_price, output_price = PRICES[model]
        return (
            self.input_tokens * input_price
            + self.cache_creation_input_tokens * input_price * 1.25
            + self.cache_read_input_tokens * input_price * 0.10
            + self.output_tokens * output_price
        ) / 1_000_000


# --- Extraction ----------------------------------------------------------------------------------


@dataclass
class Attempt:
    stop_reason: str
    answer: dict | None
    problems: list[str]
    seconds: float


@dataclass
class Extraction:
    answer: dict | None  # the last attempt's answer; None if Claude stopped without one
    problems: list[str]  # rules the final answer still breaks (empty = passed every check)
    model: str
    prompt: str
    prompt_sha256: str
    input: str = INPUT  # how page 1 was sent, e.g. "image-1568" or "pdf"
    cache: bool = False  # whether the system prompt was marked for prompt caching
    usage: Usage = field(default_factory=Usage)
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return self.usage.cost(self.model)


def _system(template: str, cache: bool) -> str | list[dict]:
    """The system prompt. With cache, it's marked as the end of a cacheable prefix: every request sends
    the same prompt and schema, so after the first one they can be read from the cache at a tenth of the
    input price for 5 minutes (each read restarts the 5 minutes). Writing the cache costs 1.25x once."""
    if not cache:
        return template
    return [{"type": "text", "text": template, "cache_control": {"type": "ephemeral"}}]


def extract(
    client,
    block: dict,
    *,
    input_kind: str = INPUT,
    cache: bool = False,
    model: str = MODEL,
    max_attempts: int = MAX_ATTEMPTS,
) -> Extraction:
    """Transcribe page 1 (block, from page_block), checking the result and retrying once with the broken
    rules if it fails. input_kind names how the page was sent, for the record."""
    system, retry = prompts.load("extract"), prompts.load("extract_retry")
    result = Extraction(
        answer=None,
        problems=[],
        model=model,
        prompt=system.id,
        prompt_sha256=system.sha256,
        input=input_kind,
        cache=cache,
    )
    messages = [_first_message(block)]

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=_system(system.template, cache),
            messages=messages,
            output_format=Form990PartI,
        )
        result.usage.add(response.usage)

        if response.stop_reason in ("refusal", "max_tokens"):
            # No usable answer, and asking again the same way won't change that.
            result.answer, result.problems = None, [f"Claude stopped without an answer ({response.stop_reason})"]
        else:
            result.answer = as_answer(response.parsed_output)
            result.problems = checks.problems(result.answer)
        result.attempts.append(
            Attempt(response.stop_reason, result.answer, result.problems, round(time.monotonic() - started, 1))
        )
        if result.answer is None or not result.problems or attempt == max_attempts:
            return result

        messages += [
            {"role": "assistant", "content": _as_history(response.content)},
            {"role": "user", "content": retry.format(problems="\n".join(f"- {p}" for p in result.problems))},
        ]
    raise AssertionError("unreachable")


# --- Command line --------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--limit", type=int, help="only the first N filings of the split (default: all)")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    parser.add_argument(
        "--input", default=INPUT, help="how to send page 1: image-<pixels> (default %(default)s) or pdf"
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="cache the prompt and schema across requests (default on; --no-cache to compare)",
    )
    parser.add_argument("--final", action="store_true", help="required for --split test: see the README")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        parser.error("the test split is held out for final scores; add --final if this is one")
    try:
        check_input(args.input)
    except ValueError as e:
        parser.error(str(e))

    with (GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
        rows = [row for row in csv.DictReader(f) if row["split"] == args.split][: args.limit]

    version = prompts.load("extract").version
    run_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{args.model}_{version}_{args.input}" + ("_cache" * args.cache)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    client = anthropic.Anthropic()
    total = Usage()

    def save_summary(complete: bool, done: int, error: str | None = None) -> None:
        totals = {"run": run_id, "complete": complete, "filings": done, "of": len(rows), "error": error}
        totals |= {"cost_usd": total.cost(args.model), "usage": asdict(total)}
        (run_dir / "summary.json").write_text(json.dumps(totals, indent=2) + "\n", encoding="utf-8", newline="\n")

    for done, row in enumerate(rows):
        object_id = row["object_id"]
        pdf = RAW_DIR / "pdf" / f"{object_id}.pdf"
        if not pdf.exists():
            raise SystemExit(f"{pdf} is missing: run `python src/build_gold.py` to download the gold-set PDFs")

        started = time.monotonic()
        block = page_block(pdf, args.input)
        try:
            result = extract(client, block, input_kind=args.input, cache=args.cache, model=args.model)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            # The SDK has already retried rate limits, server errors, and dropped connections. What's
            # left (no credit, a bad key, a rejected request) would fail for every filing: stop, and
            # mark the run incomplete so evaluate.py doesn't score a partial run as a full one.
            save_summary(complete=False, done=done, error=f"{type(e).__name__}: {e}")
            raise SystemExit(f"Stopped after {done} of {len(rows)} filings: {e}") from e
        seconds = round(time.monotonic() - started, 1)
        total.add(result.usage)

        record = {"object_id": object_id, "band": row["band"], "seconds": seconds, "cost_usd": result.cost_usd}
        record |= asdict(result)
        (run_dir / f"{object_id}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(
            f"{object_id} ({row['band']}): {len(result.attempts)} attempt(s), "
            f"checks {'passed' if not result.problems else 'FAILED'}, {seconds}s, ${result.cost_usd:.4f}"
        )

    save_summary(complete=True, done=len(rows))
    print(f"\n{len(rows)} filings, ${total.cost(args.model):.4f} total. Saved to {run_dir}\n")

    score = evaluate.score_run(run_dir)
    evaluate.record(score)
    print(evaluate.report(score))


if __name__ == "__main__":
    main()
