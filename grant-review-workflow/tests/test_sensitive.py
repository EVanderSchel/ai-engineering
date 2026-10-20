"""The sensitive-data check: identifiers are found and flagged without repeating them; ordinary figures, dates,
phone numbers and EINs aren't."""

import pytest

import evaluate
import sensitive
from intake import gold_packet
from state import ApplicationForm, ApplicationState

# Test values: 4111 1111 1111 1111 is the card networks' published test number; the SSN is in an issuable range.
FOUND = [
    ("Board chair Jo Lee (SSN 123-45-6789) signs for us.", "a Social Security number"),
    ("Her number is 123 45 6789.", "a Social Security number"),
    ("Pay by card 4111 1111 1111 1111 if needed.", "a payment card number"),
    ("Please deposit to routing number 071000013.", "a bank routing number"),
    ("Our account #: 000123456789 at First Bank.", "a bank account number"),
]
NOT_FOUND = [
    "Our EIN is 41-2121187 and our budget is $1,250,000.",
    "The return for the period ending 2025-06-30 shows 25 employees.",
    "Call 515-555-0134 or (515) 555-0134.",
    "SSA never issues 000-12-3456, 666-12-3456, 900-12-3456, 123-00-4567 or 123-45-0000.",
    "Order 4111 1111 1111 1112 failed its checksum.",
    "We will open a separate account for the project.",
]


@pytest.mark.parametrize(("text", "what"), FOUND)
def test_identifiers_are_found(text, what):
    assert [w for w, _ in sensitive.find(text)] == [what]


@pytest.mark.parametrize("text", NOT_FOUND)
def test_ordinary_text_is_left_alone(text):
    assert sensitive.find(text) == []


def application(narrative, name="Prairie Food Bank", title=None):
    form = ApplicationForm(ein="412121187", organization_name=name, amount_requested=20000, project_title=title)
    return ApplicationState.model_construct(application_id="a", form=form, narrative=narrative)


def test_the_flag_names_the_field_and_place_but_never_the_value():
    text = "x" * 1500 + " SSN 123-45-6789."
    [flag] = sensitive.flags(application(text))
    assert flag.kind == "sensitive_data" and flag.step == "cross_check"
    assert "narrative" in flag.evidence and "1,505" in flag.evidence and "6789" not in flag.evidence


def test_form_fields_are_checked_too():
    flags = sensitive.flags(application("Clean.", title="Repairs (card 4111-1111-1111-1111)"))
    assert [f.evidence.split(" contains")[0] for f in flags] == ["The project title"]


@pytest.mark.parametrize("packet", evaluate.packets("dev"))
def test_no_dev_packet_trips_the_check(packet):
    form, narrative, _ = gold_packet(packet)
    assert sensitive.flags(application(narrative, form["organization_name"], form.get("project_title"))) == []
