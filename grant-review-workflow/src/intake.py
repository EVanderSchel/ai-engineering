"""Intake: the workflow's first step, plain code. It turns a submitted packet into an ApplicationState, or
rejects it with every problem listed. A rejected packet never reaches a model, so a malformed submission
costs nothing.

Intake checks that a packet is well-formed, not that the application is eligible: an EIN with the wrong
number of digits is rejected here, a request over the fund's maximum is declined later by the rules.
"""

import datetime as dt
import json
import pathlib
import uuid

from pydantic import ValidationError

import paths
from state import ApplicationForm, ApplicationState

MAX_NARRATIVE_CHARS = 20_000  # about 3,500 words; the fund asks for 300-800
MAX_PDF_BYTES = 50 * 1024 * 1024


class IntakeError(ValueError):
    """The packet is malformed. `problems` lists every reason, so the applicant can fix them all at once."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def intake(
    form: dict,
    narrative: str,
    return_pdf: pathlib.Path,
    application_id: str | None = None,
    received_at: dt.datetime | None = None,
) -> ApplicationState:
    problems = []
    parsed = None
    try:
        parsed = ApplicationForm.model_validate(form)
    except ValidationError as e:
        problems += [f"form.{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]

    if not narrative.strip():
        problems.append("narrative is empty")
    elif len(narrative) > MAX_NARRATIVE_CHARS:
        problems.append(f"narrative is {len(narrative):,} characters, over the {MAX_NARRATIVE_CHARS:,} limit")

    if not return_pdf.is_file():
        problems.append(f"return PDF not found: {return_pdf}")
    else:
        size = return_pdf.stat().st_size
        with open(return_pdf, "rb") as f:
            is_pdf = f.read(5) == b"%PDF-"
        if not is_pdf:
            problems.append("return file is not a PDF")
        elif size > MAX_PDF_BYTES:
            problems.append(
                f"return PDF is {size / 1024 / 1024:.0f} MB, over the {MAX_PDF_BYTES // 1024 // 1024} MB limit"
            )

    if problems:
        raise IntakeError(problems)
    return ApplicationState(
        application_id=application_id or uuid.uuid4().hex,
        received_at=received_at or dt.datetime.now(dt.UTC),
        form=parsed,
        narrative=narrative,
        return_pdf=str(return_pdf),
    )


def return_pdf_path(object_id: str) -> pathlib.Path:
    """Where a gold packet's return PDF is kept once downloaded (step 2c): data/raw/returns/, gitignored."""
    return paths.RAW_DIR / "returns" / f"{object_id}.pdf"


def gold_packet(packet_id: str) -> tuple[dict, str, pathlib.Path]:
    """A gold-set packet's form, narrative and return PDF path, ready for intake()."""
    folder = paths.GOLD_DIR / packet_id
    form = json.loads((folder / "form.json").read_text(encoding="utf-8"))
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    narrative = (folder / "narrative.md").read_text(encoding="utf-8")
    return form, narrative, return_pdf_path(spec["object_id"])
