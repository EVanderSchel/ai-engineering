"""Stage 2 of Part VII extraction: read Section A's rows and totals from the pages stage 1 found.

How one return is extracted:
1. Render each Part VII page to a PNG (1568 px, the size step 4 chose) and send them in page order,
   each labeled with its page number.
2. Claude transcribes every row and the line 1d and line 2 totals, with structured outputs (schema
   below). The answer for a long list is long, so the request is streamed.
3. Check the rows against the form's own totals (problems below). A list that doesn't add up has a
   missing or misread row: show Claude the gap and ask once more, in the same conversation.

    python src/part_vii.py --count-tokens   # what a run would cost (free)
    python src/part_vii.py                  # every dev return, scored by score_part_vii.py

On the dev split, the pages come from the hand-checked labels (data/gold/part_vii_pages.csv), so this
stage is measured on its own; stage 1 found exactly those pages on its first run.
"""

import argparse
import base64
import datetime
import json
import time
from dataclasses import asdict, dataclass, field

import anthropic
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel, ConfigDict, create_model
from pydantic import Field as PydanticField

import find_pages
import prompts
import score_part_vii
from extract import MODEL, PRICES, Usage, _system, page_png
from fields import PART_VII_COLUMNS, PART_VII_TOTALS
from paths import DATA_DIR, RAW_DIR

RUNS_DIR = DATA_DIR / "part_vii_runs"
MAX_TOKENS = 64000  # an 88-row list is a long answer; streaming keeps a long request from timing out
MAX_ATTEMPTS = 2
LONG_EDGE_PX = 1568

# --- Schema ----------------------------------------------------------------------------------------
# Hours can be blank (and a blank "related organizations" line is different from 0.00), so they're
# nullable. The API allows at most 16 nullable fields in a schema; the row's two count once each,
# because every row shares one definition, and the four totals bring it to six.

_TYPES = {"str": str, "int": int, "decimal": float | None, "bool": bool}

PartVIIRow = create_model(
    "PartVIIRow",
    __config__=ConfigDict(extra="forbid"),
    **{f.name: (_TYPES[f.kind], PydanticField(description=f"{f.line}: {f.description}")) for f in PART_VII_COLUMNS},
)


class PartVII(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[PartVIIRow] = PydanticField(description="Every row of Section A, line 1a, in the order printed")
    total_pay: int | None = PydanticField(description=PART_VII_TOTALS[0].line)
    total_pay_related: int | None = PydanticField(description=PART_VII_TOTALS[1].line)
    total_other_pay: int | None = PydanticField(description=PART_VII_TOTALS[2].line)
    people_over_100k: int | None = PydanticField(description=PART_VII_TOTALS[3].line)


def as_answer(extraction: PartVII) -> dict:
    """The extraction in the answer keys' form: {"rows": [...], "totals": {...}}."""
    values = extraction.model_dump(mode="json")
    rows = values.pop("rows")
    return {"rows": rows, "totals": values}


# --- Checks ----------------------------------------------------------------------------------------
# Both rules hold on all 60 answer keys (tests/test_part_vii.py). The column totals can be off by
# rounding: one return's column (F) is $1 more than its rows, so a column may be off by up to $1 per row.

COLUMN_TOTALS = [
    ("pay", "total_pay", "(D)"),
    ("pay_related", "total_pay_related", "(E)"),
    ("other_pay", "total_other_pay", "(F)"),
]


def problems(answer: dict) -> list[str]:
    rows, totals = answer["rows"], answer["totals"]
    found = []
    for column, total, letter in COLUMN_TOTALS:
        added = sum(row[column] for row in rows)
        printed = totals[total] or 0  # a blank total line is 0
        if abs(added - printed) > len(rows):
            found.append(
                f"Column {letter}: the {len(rows)} rows add up to {added}, but line 1d, column {letter} is "
                f"{printed} (a difference of {printed - added})"
            )
    over = sum(row["pay"] > 100_000 for row in rows)
    if over > (totals["people_over_100k"] or 0):
        found.append(
            f"Line 2: {over} rows have more than $100,000 in column (D), but line 2 says "
            f"{totals['people_over_100k']} people received more than $100,000"
        )
    return found


# --- Extraction ------------------------------------------------------------------------------------


def _first_message(pdf_path, pages: list[int]) -> dict:
    content = []
    for page in pages:
        png = page_png(pdf_path, page_number=page - 1, long_edge=LONG_EDGE_PX)
        data = base64.standard_b64encode(png).decode()
        content.append({"type": "text", "text": f"Page {page}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    content.append({"type": "text", "text": "Transcribe Part VII, Section A from these pages."})
    return {"role": "user", "content": content}


def _call(client, **request):
    with client.messages.stream(**request) as stream:
        return stream.get_final_message()


@dataclass
class Attempt:
    stop_reason: str
    rows: int | None
    problems: list[str]
    seconds: float


@dataclass
class Extraction:
    answer: dict | None
    problems: list[str]
    pages: list[int]
    model: str
    prompt: str
    prompt_sha256: str
    usage: Usage = field(default_factory=Usage)
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return self.usage.cost(self.model)


def extract(client, pdf_path, pages: list[int], *, model: str = MODEL, max_attempts: int = MAX_ATTEMPTS) -> Extraction:
    system, retry = prompts.load("extract_part_vii"), prompts.load("extract_part_vii_retry")
    result = Extraction(None, [], pages, model, system.id, system.sha256)
    messages = [_first_message(pdf_path, pages)]

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        response = _call(
            client,
            model=model,
            max_tokens=MAX_TOKENS,
            system=_system(system.template, cache=True),
            messages=messages,
            output_format=PartVII,
        )
        result.usage.add(response.usage)
        if response.stop_reason in ("refusal", "max_tokens"):
            result.answer, result.problems = None, [f"Claude stopped without an answer ({response.stop_reason})"]
        else:
            result.answer = as_answer(response.parsed_output)
            result.problems = problems(result.answer)
        rows = None if result.answer is None else len(result.answer["rows"])
        result.attempts.append(
            Attempt(response.stop_reason, rows, result.problems, round(time.monotonic() - started, 1))
        )
        if result.answer is None or not result.problems or attempt == max_attempts:
            return result
        messages += [
            {"role": "assistant", "content": [_history(b) for b in response.content]},
            {"role": "user", "content": retry.format(problems="\n".join(f"- {p}" for p in result.problems))},
        ]
    raise AssertionError("unreachable")


def _history(block) -> dict:
    """A reply block to send back before a retry, without the SDK-only parsed_output field."""
    return {"type": "text", "text": block.text} if block.type == "text" else block.to_dict()


# --- Command line ----------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only the first N labeled returns")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    parser.add_argument("--count-tokens", action="store_true", help="estimate the cost without running (free)")
    args = parser.parse_args()

    labeled = find_pages.labels()  # dev returns only
    object_ids = list(labeled)[: args.limit]
    client = anthropic.Anthropic()
    system = prompts.load("extract_part_vii")

    if args.count_tokens:
        counts = {}
        for object_id in object_ids:
            counts[object_id] = client.messages.count_tokens(
                model=args.model,
                system=system.template,
                messages=[_first_message(RAW_DIR / "pdf" / f"{object_id}.pdf", labeled[object_id])],
                output_config={"format": {"type": "json_schema", "schema": transform_schema(PartVII)}},
            ).input_tokens
            print(f"{object_id}: {len(labeled[object_id])} pages, {counts[object_id]} input tokens")
        input_price, _ = PRICES[args.model]
        mean = sum(counts.values()) / len(counts)
        print(f"\nMean {mean:.0f} input tokens: ${mean * input_price / 1e6:.4f} input per return, plus output")
        return

    run_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{args.model}_{system.version}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    total = Usage()

    def save_summary(complete: bool, done: int, error: str | None = None) -> None:
        summary = {"run": run_id, "complete": complete, "filings": done, "of": len(object_ids), "error": error}
        summary |= {"cost_usd": total.cost(args.model), "usage": asdict(total)}
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")

    for done, object_id in enumerate(object_ids):
        started = time.monotonic()
        try:
            result = extract(client, RAW_DIR / "pdf" / f"{object_id}.pdf", labeled[object_id], model=args.model)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            save_summary(complete=False, done=done, error=f"{type(e).__name__}: {e}")
            raise SystemExit(f"Stopped after {done} of {len(object_ids)} returns: {e}") from e
        seconds = round(time.monotonic() - started, 1)
        total.add(result.usage)
        record = {"object_id": object_id, "seconds": seconds, "cost_usd": result.cost_usd, **asdict(result)}
        (run_dir / f"{object_id}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
        rows = "no answer" if result.answer is None else f"{len(result.answer['rows'])} rows"
        print(
            f"{object_id}: {rows}, {len(result.attempts)} attempt(s), "
            f"checks {'passed' if not result.problems else 'FAILED'}, {seconds}s, ${result.cost_usd:.4f}"
        )

    save_summary(complete=True, done=len(object_ids))
    print(f"\n{len(object_ids)} returns, ${total.cost(args.model):.4f} total. Saved to {run_dir}\n")
    score = score_part_vii.score_run(run_dir)
    score_part_vii.record(score)
    print(score_part_vii.report(score))


if __name__ == "__main__":
    main()
