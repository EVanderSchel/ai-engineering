"""Identifiers that never belong in a grant application (step 6c): found in code, flagged, never repeated.

An applicant sometimes pastes more than they should: a board member's Social Security number, the bank details
for the award. The fund doesn't need these and shouldn't keep them, so the reviewer is told to have the applicant
resubmit without them. Each finding names the field and where in it, never the value: the flag's evidence is
stored in checkpoints and shown in the reviewer's UI.

Patterns are deliberately narrow, so ordinary figures and the EIN don't trip them:
- Social Security numbers only with separators (123-45-6789, 123 45 6789), and only numbers the SSA could issue
  (a bare 9-digit number is as likely an EIN);
- card numbers of 13-19 digits that pass the Luhn checksum;
- bank routing and account numbers only right after the words "routing" or "account".

Contact details (names, emails, phones, addresses, ages) aren't flagged: grant narratives name staff and describe
the people they serve ("children between the ages of 8 and 14"), and the guardrail's contact-detail findings on
dev were all of that kind (results/guardrail-dev-v1.md).
"""

import re

from state import ApplicationState, Flag

SSN = re.compile(r"(?<![\d-])(\d{3})[- ](\d{2})[- ](\d{4})(?![\d-])")
CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
BANK = re.compile(r"\b(routing|account)\b(?:\s+(?:number|no\.?|#))?[\s:#]*(\d[\d -]{4,20}\d)", re.IGNORECASE)


def _ssn_could_be_issued(area: str, group: str, serial: str) -> bool:
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def find(text: str) -> list[tuple[str, int]]:
    """(what, character offset) for each identifier in the text, in order."""
    found = []
    for m in SSN.finditer(text):
        if _ssn_could_be_issued(*m.groups()):
            found.append(("a Social Security number", m.start()))
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if _luhn(digits) and not SSN.fullmatch(m.group()):
            found.append(("a payment card number", m.start()))
    for m in BANK.finditer(text):
        found.append((f"a bank {m.group(1).lower()} number", m.start(2)))
    return sorted(found, key=lambda f: f[1])


def flags(state: ApplicationState) -> list[Flag]:
    fields = {
        "organization name": state.form.organization_name,
        "project title": state.form.project_title or "",
        "narrative": state.narrative,
    }
    found = []
    for field, text in fields.items():
        for what, offset in find(text):
            evidence = (
                f"The {field} contains what looks like {what} (at character {offset:,}). The fund doesn't need it: "
                "ask the applicant to resubmit without it."
            )
            found.append(Flag(step="cross_check", kind="sensitive_data", evidence=evidence))
    return found
