"""The personal-data detector and redactor, against the labelled cases in data/guardrails/pii_cases.json."""

import json
import random
import re
import time

import pytest

import paths
import pii

CASES = json.loads((paths.ROOT / "data" / "guardrails" / "pii_cases.json").read_text(encoding="utf-8"))["cases"]

# Where the detector and the labels disagree, on purpose and on record: the two known misses, and one false alarm
# (a retina image file name looks like an email address). A change to any of them should be noticed and decided.
KNOWN_DIFFERENCES = {
    "His social is 123456789, put it in the field.": [],
    "My social is one two three, four five, six seven eight nine.": [],
    "The image is saved as logo@2x.png in the assets folder.": ["email"],
}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["text"][:40])
def test_each_labelled_case(case):
    expected = KNOWN_DIFFERENCES.get(case["text"], case["expect"])
    assert [f.kind for f in pii.find(case["text"])] == expected


def test_known_misses_are_labelled_as_such():
    labelled = {c["text"] for c in CASES if "known_miss" in c}
    misses = {text for text, kinds in KNOWN_DIFFERENCES.items() if kinds == []}
    assert labelled == misses


def test_redaction_replaces_each_value_with_its_kind():
    text = "Mail jo@example.com or call (515) 555-0134; SSN 123-45-6789."
    redacted, findings = pii.redact(text)
    assert redacted == "Mail [EMAIL] or call [PHONE]; SSN [SSN]."
    assert [text[f.start : f.end] for f in findings] == ["jo@example.com", "(515) 555-0134", "123-45-6789"]


def test_only_the_chosen_kinds_are_found_or_redacted():
    text = "Mail jo@example.com, SSN 123-45-6789."
    assert [f.kind for f in pii.find(text, pii.IDENTIFIERS)] == ["ssn"]
    assert pii.redact(text, ("email",))[0] == "Mail [EMAIL], SSN 123-45-6789."


def test_findings_never_overlap():
    # 4111 1111 1111 1111 is a card; its first digits are not also a phone number or anything else.
    findings = pii.find("Pay 4111 1111 1111 1111 now")
    assert [f.kind for f in findings] == ["card"]
    assert all(a.end <= b.start for a, b in zip(findings, findings[1:], strict=False))


def test_text_without_personal_data_is_unchanged():
    text = "Summarise the attached minutes in three bullet points."
    assert pii.redact(text) == (text, [])


@pytest.mark.parametrize(
    "text", ["%" * 100_000, "%a" * 50_000, "x@" + "a." * 50_000 + "1", "a@" * 50_000, "a.@" * 33_000]
)
def test_long_adversarial_text_is_scanned_in_linear_time(text):
    # code scanning alert 16: 100,000 '%' took over 10 s with the email regex. The checks run on the event loop, so a
    # slow pattern stalls every request in the process.
    started = time.perf_counter()
    pii.find(text)
    assert time.perf_counter() - started < 1.0


def test_the_email_scan_finds_exactly_what_the_email_regex_found():
    regex = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
    rng = random.Random(7)
    for alphabet in ["ab1.@ %", "abZ9.-_%+@ é\n", "co.m@x-_", "aB.@1é²ß-+ "]:
        for _ in range(5_000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 28)))
            assert pii._emails(text) == [m.span() for m in regex.finditer(text)], text
