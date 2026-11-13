"""Personal data in a request or an answer: found in code, and optionally replaced before it leaves the gateway.

Plain language: before a message goes to a model provider, the gateway can spot a Social Security number, a card
number, bank details, an email address or a phone number in it, and either note that it was there (flag), swap it
for a placeholder like [EMAIL] (redact), or refuse the request (block). The detector never stores or logs the value
it found, only what kind it was and where.

Two kinds, because they differ in how often they're wanted:

- **identifiers** (`ssn`, `card`, `bank`): numbers that almost never belong in a prompt. Adapted from
  grant-review-workflow's `sensitive.py` (step 6c there), with the same deliberately narrow patterns: SSNs only
  with separators and in ranges the SSA could issue (a bare 9-digit number is as likely an EIN), card numbers only
  if they pass the Luhn checksum, bank numbers only right after the words "routing" or "account".
- **contact details** (`email`, `phone`): common in ordinary work ("email the venue at ..."), so a team may want
  them redacted but rarely blocked. Phones are North American numbers written the usual ways.

Regular expressions, not a model: free, deterministic, about 0.2 ms per kilobyte of text on this project's PC (five
passes, one per pattern; results/guardrails.md), so about 1 ms for a typical prompt against a second or so for the
model call; and every miss and false alarm can be explained. The price is coverage: names, addresses and dates of
birth aren't detected (they need a model and still miss often), and numbers written oddly ("one two three...") get
through.
"""

import re
from dataclasses import dataclass

IDENTIFIERS = ("ssn", "card", "bank")
CONTACT = ("email", "phone")
KINDS = IDENTIFIERS + CONTACT

SSN = re.compile(r"(?<![\d-])(\d{3})[- ](\d{2})[- ](\d{4})(?![\d-])")
CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
BANK = re.compile(r"\b(routing|account)\b(?:\s+(?:number|no\.?|#))?[\s:#]*(\d[\d -]{4,20}\d)", re.IGNORECASE)
# Email addresses are found by scanning out from each "@" (_emails below), not by a regular expression. The pattern
# they follow, as a regex for reference:
#     (?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])
# Searched for with that regex, a long run of local-part characters with no "@" was rescanned from every position in
# it: quadratic time, 10 s for 80,000 "%" (code scanning alert 16, py/polynomial-redos). The scan reads each character
# a bounded number of times, and finds exactly what the regex found (tests/test_pii.py compares them).
LOCAL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-")
LABEL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
NOT_BEFORE = re.compile(r"[\w.%+-]")  # one character: what can't come right before an address
NOT_AFTER = re.compile(r"[\w-]")  # one character: what can't come right after one
# (515) 555-0134, 515-555-0134, 515.555.0134, +1 515 555 0134. Separators are required: a bare 10-digit number is
# as likely an order or account number. Area codes and exchanges start 2-9, as in the North American plan.
PHONE = re.compile(
    r"(?<![\w+])(?:\+?1[ .-]?)?(?:\(([2-9]\d{2})\)\s?|([2-9]\d{2})[ .-])([2-9]\d{2})[ .-](\d{4})(?![\w-])"
)


@dataclass(frozen=True)
class Finding:
    kind: str  # one of KINDS
    start: int
    end: int


def _ssn_could_be_issued(area: str, group: str, serial: str) -> bool:
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _domain_end(text: str, i: int) -> int | None:
    """Where the domain starting at text[i] ends: dot-separated labels, the last at least two letters and not followed
    by a word character or "-". The longest such domain, as the regex's greedy repetition would take; None if none."""
    labels, pos = [], i
    while True:
        end = pos
        while end < len(text) and text[end] in LABEL_CHARS:
            end += 1
        if end == pos:
            break
        labels.append((pos, end))
        if end < len(text) and text[end] == ".":
            pos = end + 1
        else:
            break
    for start, end in reversed(labels[1:]):  # a domain has at least two labels
        label = text[start:end]
        if len(label) >= 2 and label.isascii() and label.isalpha() and not NOT_AFTER.match(text, end):
            return end
    return None


def _emails(text: str) -> list[tuple[int, int]]:
    """(start, end) of each email address, left to right and not overlapping, as the regex above finds them."""
    spans: list[tuple[int, int]] = []
    last_end, at = 0, text.find("@")
    while at != -1:
        start = at
        while start > last_end and text[start - 1] in LOCAL_CHARS:
            start -= 1
        before_ok = start == 0 or not NOT_BEFORE.match(text, start - 1)
        if start < at and before_ok:  # a local part, starting where its run starts
            end = _domain_end(text, at + 1)
            if end is not None:
                spans.append((start, end))
                last_end = end
        at = text.find("@", max(at + 1, last_end))
    return spans


def find(text: str, kinds: tuple[str, ...] = KINDS) -> list[Finding]:
    """Every piece of personal data of the given kinds, in order, without overlaps (the earlier, longer one wins)."""
    found: list[Finding] = []
    if "ssn" in kinds:
        found += [Finding("ssn", m.start(), m.end()) for m in SSN.finditer(text) if _ssn_could_be_issued(*m.groups())]
    if "card" in kinds:
        for m in CARD.finditer(text):
            if _luhn(re.sub(r"\D", "", m.group())) and not SSN.fullmatch(m.group()):
                found.append(Finding("card", m.start(), m.end()))
    if "bank" in kinds:
        found += [Finding("bank", m.start(2), m.end(2)) for m in BANK.finditer(text)]
    if "email" in kinds:
        found += [Finding("email", start, end) for start, end in _emails(text)]
    if "phone" in kinds:
        found += [Finding("phone", m.start(), m.end()) for m in PHONE.finditer(text)]
    found.sort(key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Finding] = []
    for finding in found:
        if not kept or finding.start >= kept[-1].end:
            kept.append(finding)
    return kept


def redact(text: str, kinds: tuple[str, ...] = KINDS) -> tuple[str, list[Finding]]:
    """The text with each finding replaced by a placeholder such as [EMAIL], and the findings (positions in the
    original text)."""
    findings = find(text, kinds)
    parts, last = [], 0
    for f in findings:
        parts += [text[last : f.start], f"[{f.kind.upper()}]"]
        last = f.end
    parts.append(text[last:])
    return "".join(parts), findings
