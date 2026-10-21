"""extract: page 1 of the applicant's Form 990 (an image) -> the Part I figures the workflow needs.

Adapted from Project 2 (irs-990-extraction), keeping what it measured: page 1 rendered at 1568 px on the
longer side (lower resolution misread digits; higher cost more for nothing), the EIN copied as printed with
its dash and the dash removed in code (asking the model to drop it scrambled digits), and every cell read on
its own (blank versus 0 was the main remaining error). Differences: the call goes through Bedrock's Converse
API, only the 12 figures this workflow uses are read, and a blank line is null rather than listed apart.

    uv run python src/extract.py               # the 15 dev packets on Sonnet 4.6, scored against the answer keys
    uv run python src/extract.py --model haiku
"""

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Literal

import pymupdf
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

import bedrock
import fund_rules
import paths
from intake import gold_packet
from state import Extraction, ReturnFigures

LONG_EDGE_PX = 1568
MAX_TOKENS = 2000
REVENUE_LINES = [
    "contributions_and_grants_current_year",
    "program_service_revenue_current_year",
    "investment_income_current_year",
    "other_revenue_current_year",
]

# The system prompt, kept as a file so it's versioned and diffed like code. v1 is Project 2's prompt v2,
# trimmed to these fields, with null for blank lines.
PROMPT_VERSION = "v1"
SYSTEM = (paths.ROOT / "prompts" / "extract" / f"{PROMPT_VERSION}.txt").read_text(encoding="utf-8").strip()


class PageOne(BaseModel):
    """The figures printed on page 1 (header and Part I) of a Form 990, transcribed exactly. null means the
    line is blank on the form."""

    form_type: Literal["990", "990-EZ", "990-PF", "other"] = Field(
        description="The form number printed at the top left"
    )
    ein_as_printed: str = Field(description="Box D, exactly as printed, including the dash")
    organization_name: str = Field(description="Box C: the filer's name, every line of it, not a DBA or care-of line")
    tax_period_end: dt.date | None = Field(description="Line A: the last day of the tax year, YYYY-MM-DD")
    employees: int | None = Field(description="Part I line 5: total number of individuals employed")
    contributions_and_grants_current_year: int | None = Field(description="Line 8, Current Year column")
    program_service_revenue_current_year: int | None = Field(description="Line 9, Current Year column")
    investment_income_current_year: int | None = Field(description="Line 10, Current Year column")
    other_revenue_current_year: int | None = Field(description="Line 11, Current Year column")
    total_revenue_current_year: int | None = Field(description="Line 12, Current Year column")
    total_expenses_current_year: int | None = Field(description="Line 18, Current Year column")
    total_assets_end_of_year: int | None = Field(description="Line 20, End of Year column")
    total_liabilities_end_of_year: int | None = Field(description="Line 21, End of Year column")


def page_png(pdf_path: str | pathlib.Path, page_number: int = 0, long_edge: int = LONG_EDGE_PX) -> bytes:
    """One page of a PDF as a PNG whose longer side is long_edge pixels."""
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_number]
        zoom = long_edge / max(page.rect.width, page.rect.height)
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


def revenue_adds_up(figures: ReturnFigures) -> bool | None:
    """Line 12 should equal lines 8-11 (a blank line counts as 0, as on the form). None if line 12 is blank."""
    if figures.total_revenue_current_year is None:
        return None
    parts = sum(getattr(figures, name) or 0 for name in REVENUE_LINES)
    return parts == figures.total_revenue_current_year


def to_extraction(page: PageOne) -> Extraction:
    fields = page.model_dump(exclude={"form_type", "ein_as_printed"})
    figures = ReturnFigures(ein=page.ein_as_printed.replace("-", "").strip(), **fields)
    return Extraction(form_type=page.form_type, figures=figures, revenue_adds_up=revenue_adds_up(figures))


def extract(client, pdf_path: str | pathlib.Path, model: str = "sonnet") -> tuple[Extraction, bedrock.CallRecord]:
    content = [
        {"image": {"format": "png", "source": {"bytes": page_png(pdf_path)}}},
        {"text": "Transcribe page 1 of this return."},
    ]
    page, record = bedrock.structured(
        client, step="extract", model=model, system=SYSTEM, content=content, schema=PageOne, max_tokens=MAX_TOKENS
    )
    return to_extraction(page), record


# --- Checking on the dev packets -------------------------------------------------------------------------

COMPARED = ["ein", "tax_period_end", "employees", *REVENUE_LINES, "total_revenue_current_year"]
COMPARED += ["total_expenses_current_year", "total_assets_end_of_year", "total_liabilities_end_of_year"]


def answer_key(object_id: str) -> dict:
    return json.loads((paths.FILINGS_DIR / f"{object_id}.json").read_text(encoding="utf-8"))["fields"]


def compare(extraction: Extraction, key: dict) -> list[str]:
    """The figures that differ from the IRS answer key, as 'field: read X, key Y'."""
    read = extraction.figures.model_dump(mode="json")
    return [f"{name}: read {read[name]}, key {key.get(name)}" for name in COMPARED if read[name] != key.get(name)]


def name_read_right(extraction: Extraction, key: dict) -> bool:
    """Whether the return's name (box C) was read as the answer key has it, compared the way cross_check compares
    names (case, punctuation, a leading THE and corporate suffixes don't count). Scored on its own, not with the
    figures: a name read as "Fire & Rescue Dept" for "FIRE AND RESCUE DEPT" isn't wrong for the name check."""
    from research import normalize_name  # research imports the ProPublica client; only scoring needs it here

    read = extraction.figures.organization_name
    return read is not None and normalize_name(read) == normalize_name(key.get("organization_name") or "")


def score_dev(model: str) -> dict:
    client = bedrock.make_client()
    rows = []
    for folder in sorted(paths.GOLD_DIR.glob("gp-*")):
        label = json.loads((folder / "label.json").read_text(encoding="utf-8"))
        if label["split"] != "dev":
            continue
        form, _, pdf = gold_packet(folder.name)
        object_id = json.loads((folder / "spec.json").read_text(encoding="utf-8"))["object_id"]
        extraction, record = extract(client, pdf, model)
        rules = fund_rules.failed_rules(
            extraction.figures.model_dump(mode="json"), extraction.form_type, form["amount_requested"]
        )
        row = {
            "packet_id": folder.name,
            "wrong_fields": compare(extraction, answer_key(object_id)),
            "revenue_adds_up": extraction.revenue_adds_up,
            "failed_rules": rules,
            "label_failed_rules": label["failed_rules"],
            "cost_usd": record.cost_usd,
            "latency_ms": record.latency_ms,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
        }
        rows.append(row)
        status = "ok" if not row["wrong_fields"] else "; ".join(row["wrong_fields"])
        rules_note = "" if rules == label["failed_rules"] else f" | RULES {rules} vs label {label['failed_rules']}"
        print(f"{folder.name}: {status}{rules_note} (${record.cost_usd:.4f}, {record.latency_ms} ms)")
    n_fields = len(rows) * len(COMPARED)
    wrong = sum(len(r["wrong_fields"]) for r in rows)
    summary = {
        "model": model,
        "model_id": bedrock.MODELS[model].model_id,
        "date": dt.date.today().isoformat(),
        "packets": len(rows),
        "field_accuracy": round(1 - wrong / n_fields, 4),
        "wrong_fields": wrong,
        "fields_checked": n_fields,
        "eligibility_agrees": sum(r["failed_rules"] == r["label_failed_rules"] for r in rows),
        "revenue_not_adding_up": sum(r["revenue_adds_up"] is False for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "median_latency_ms": sorted(r["latency_ms"] for r in rows)[len(rows) // 2],
        "rows": rows,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run extract on the 15 dev packets and score it.")
    parser.add_argument("--model", choices=sorted(bedrock.MODELS), default="sonnet")
    args = parser.parse_args()
    try:
        summary = score_dev(args.model)
    except ClientError as e:
        print(f"Bedrock error: {e}", file=sys.stderr)
        return 1
    out = paths.ROOT / "results" / f"extract-dev-{args.model}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"\n{summary['model']}: {summary['field_accuracy']:.1%} of fields right "
        f"({summary['wrong_fields']} wrong of {summary['fields_checked']}), eligibility agrees on "
        f"{summary['eligibility_agrees']}/{summary['packets']}, revenue not adding up on "
        f"{summary['revenue_not_adding_up']}, ${summary['cost_usd']:.4f} total, "
        f"median {summary['median_latency_ms']} ms. Saved {out.relative_to(paths.ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
