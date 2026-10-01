"""Baseline extraction: page 1 of a Form 990 (as an image) -> validated Part I fields, with Claude.

How one filing is extracted:
1. Render page 1 of the IRS page-image PDF to a PNG.
2. Ask Claude to transcribe it, with structured outputs: the response is forced to match the schema in
   schema.py (every field present, right type, null for blank lines), and the SDK parses it into a
   Pydantic object.
3. Check the answer against the form's own arithmetic (checks.py). If a rule is broken, Claude misread
   something: show it the broken rules and ask once more, in the same conversation.
4. Record what it cost: tokens from the API's usage report, priced per model.

Run a few dev filings and print a quick comparison with their answer keys (the real, normalized eval
is step 3):

    python src/extract.py --split dev --limit 3
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
import prompts
from paths import DATA_DIR, GOLD_DIR, RAW_DIR
from schema import Form990PartI, as_answer

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
MAX_ATTEMPTS = 2  # the first answer, plus one retry when the arithmetic checks fail
LONG_EDGE_PX = 1568  # larger images are scaled down by the API anyway; step 4 tests other sizes
RUNS_DIR = DATA_DIR / "runs"

# US dollars per million tokens: (input, output). Cache writes cost 1.25x input and cache reads 0.1x;
# this pipeline doesn't cache, but they're priced in case a later step does.
PRICES = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}


# --- Input ---------------------------------------------------------------------------------------


def page_png(pdf_path, page_number: int = 0, long_edge: int = LONG_EDGE_PX) -> bytes:
    """One page of a PDF as a PNG, scaled so its longer side is long_edge pixels."""
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_number]
        zoom = long_edge / max(page.rect.width, page.rect.height)
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


def _first_message(png: bytes) -> dict:
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(png).decode()},
    }
    return {"role": "user", "content": [image, {"type": "text", "text": "Transcribe page 1 of this return."}]}


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
    usage: Usage = field(default_factory=Usage)
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return self.usage.cost(self.model)


def extract(client, png: bytes, *, model: str = MODEL, max_attempts: int = MAX_ATTEMPTS) -> Extraction:
    """Transcribe page 1, checking the result and retrying once with the broken rules if it fails."""
    system, retry = prompts.load("extract"), prompts.load("extract_retry")
    result = Extraction(answer=None, problems=[], model=model, prompt=system.id, prompt_sha256=system.sha256)
    messages = [_first_message(png)]

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system.template,
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


def _gold(object_id: str) -> dict:
    return json.loads((GOLD_DIR / f"{object_id}.json").read_text(encoding="utf-8"))["fields"]


def _mismatches(answer: dict | None, expected: dict) -> dict:
    """Fields whose extracted value isn't exactly the answer key's. Exact comparison on purpose: step 3
    decides what normalization is fair (case, spacing in the mission). This is only a first look."""
    if answer is None:
        return dict.fromkeys(expected, "no answer")
    return {
        name: {"expected": expected[name], "got": answer[name]} for name in expected if answer[name] != expected[name]
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--limit", type=int, default=3, help="how many filings (each one costs a few cents)")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    args = parser.parse_args()

    with (GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
        rows = [row for row in csv.DictReader(f) if row["split"] == args.split][: args.limit]

    run_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{args.model}_{prompts.load('extract').version}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    client = anthropic.Anthropic()
    total = Usage()
    summary = []

    for row in rows:
        object_id = row["object_id"]
        pdf = RAW_DIR / "pdf" / f"{object_id}.pdf"
        if not pdf.exists():
            raise SystemExit(f"{pdf} is missing: run `python src/build_gold.py` to download the gold-set PDFs")

        started = time.monotonic()
        result = extract(client, page_png(pdf), model=args.model)
        seconds = round(time.monotonic() - started, 1)
        total.add(result.usage)
        wrong = _mismatches(result.answer, _gold(object_id))

        record = {"object_id": object_id, "band": row["band"], "seconds": seconds, "cost_usd": result.cost_usd}
        record |= {"wrong": wrong, **asdict(result)}
        (run_dir / f"{object_id}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        summary.append({k: record[k] for k in ("object_id", "band", "seconds", "cost_usd")} | {"wrong": len(wrong)})

        print(
            f"{object_id} ({row['band']}): {len(wrong)} of {len(_gold(object_id))} fields differ, "
            f"{len(result.attempts)} attempt(s), checks {'passed' if not result.problems else 'FAILED'}, "
            f"{seconds}s, ${result.cost_usd:.4f}"
        )
        for name, diff in wrong.items():
            print(f"    {name}: {diff}")

    cost = total.cost(args.model)
    totals = {"run": run_id, "filings": len(rows), "cost_usd": cost, "usage": asdict(total), "filings_detail": summary}
    (run_dir / "summary.json").write_text(json.dumps(totals, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(rows)} filings, ${cost:.4f} total, ${cost / max(len(rows), 1):.4f} per filing. Saved to {run_dir}")


if __name__ == "__main__":
    main()
