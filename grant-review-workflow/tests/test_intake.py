"""Intake: every gold packet is accepted, malformed packets are rejected with every problem listed, and
nothing about eligibility is decided here."""

import datetime as dt

import pytest

import intake
import narratives
import paths

PACKETS = narratives.packets_with_narratives()  # a packet without its narrative yet can't be an application


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "return.pdf"
    path.write_bytes(b"%PDF-1.7\n...")
    return path


GOOD_FORM = {"ein": "12-3456789", "organization_name": "Org", "amount_requested": 20_000, "project_title": "T"}


@pytest.mark.parametrize("packet_id", PACKETS)
def test_every_gold_packet_passes_intake(packet_id, pdf):
    """Including the ones that must be declined: ineligible is a decision for the rules, not intake."""
    form, narrative, _ = intake.gold_packet(packet_id)
    application = intake.intake(form, narrative, pdf, application_id=packet_id)
    assert application.form.ein == form["ein"] and application.narrative == narrative


def test_a_good_packet_becomes_a_fresh_state(pdf):
    received = dt.datetime(2025, 6, 1, tzinfo=dt.UTC)
    application = intake.intake(GOOD_FORM, "We request $20,000.", pdf, received_at=received)
    assert application.form.ein == "123456789"
    assert application.received_at == received and len(application.application_id) == 32
    assert application.extraction is None and application.flags == [] and application.calls == []


def test_every_problem_is_reported_at_once(tmp_path):
    not_pdf = tmp_path / "return.pdf"
    not_pdf.write_text("hello")
    bad_form = {"ein": "123", "organization_name": "", "amount_requested": 0}
    with pytest.raises(intake.IntakeError) as raised:
        intake.intake(bad_form, "   ", not_pdf)
    problems = raised.value.problems
    assert any("ein" in p for p in problems)
    assert any("organization_name" in p for p in problems)
    assert any("amount_requested" in p for p in problems)
    assert "narrative is empty" in problems
    assert "return file is not a PDF" in problems


def test_a_missing_return_is_rejected(tmp_path):
    with pytest.raises(intake.IntakeError, match="not found"):
        intake.intake(GOOD_FORM, "text", tmp_path / "missing.pdf")


def test_an_overlong_narrative_is_rejected(pdf):
    with pytest.raises(intake.IntakeError, match="over the"):
        intake.intake(GOOD_FORM, "x" * (intake.MAX_NARRATIVE_CHARS + 1), pdf)


def test_a_request_over_the_funds_maximum_still_passes_intake(pdf):
    """The $50,000 maximum is an eligibility rule: intake only checks the amount is a positive number."""
    assert intake.intake({**GOOD_FORM, "amount_requested": 75_000}, "text", pdf).form.amount_requested == 75_000


def test_gold_return_pdfs_are_kept_in_the_gitignored_raw_folder():
    _, _, path = intake.gold_packet("gp-001")
    assert path.parent == paths.RAW_DIR / "returns" and path.suffix == ".pdf"
