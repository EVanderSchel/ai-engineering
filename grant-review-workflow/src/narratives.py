"""Check that each gold-set narrative states exactly the figures its spec calls for, so a planted mismatch
really is in the text and a clean narrative really isn't off.

    uv run python src/narratives.py      # report every packet that has a narrative

A packet's narrative lives in data/gold/<packet_id>/narrative.md, with narrative.json beside it:
{"project_title": ..., "injection_text": ... or null, "written_by": ..., "approved": false}. The injection
text (when planted) must appear verbatim in the narrative and is set aside before the figures are checked,
since altered figures are its whole point.

Rules a narrative must follow:
- It starts with "# <project title>" and has a "## Budget" section of "- item: $amount" lines.
- Every dollar amount is the claimed annual budget, the amount requested, or a budget line.
- The budget lines add up to the amount requested.
- A staff count ("<n> staff", "<n> employees", "<n> full-time ...") matches the spec's claim, and none
  appears when the spec has no staff claim.
- 300 to 800 words.
"""

import json
import re

import paths

# A comma belongs to the number only when a digit follows ("$400,000, and" is $400,000).
DOLLARS = re.compile(r"\$(\d{1,3}(?:,\d{3})+|\d+)(?!\d|,\d)")
STAFF = re.compile(r"\b(\d[\d,]*)\s+(?:(?:full-time|part-time|paid)\s+)*(?:staff(?: members)?|employees)\b")
BUDGET_LINE = re.compile(r"^- [^:\n]+: \$(\d{1,3}(?:,\d{3})+|\d+)\s*$", re.MULTILINE)
WORDS = (300, 800)


def _amount(text: str) -> int:
    return int(text.replace(",", ""))


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## {heading}\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def problems(packet_id: str) -> list[str]:
    folder = paths.GOLD_DIR / packet_id
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    form = json.loads((folder / "form.json").read_text(encoding="utf-8"))
    meta = json.loads((folder / "narrative.json").read_text(encoding="utf-8"))
    text = (folder / "narrative.md").read_text(encoding="utf-8")
    found = []

    injection = meta.get("injection_text")
    if bool(injection) != bool(spec["injection"]):
        found.append(f"injection text is {'present' if injection else 'missing'}, spec says {spec['injection']}")
    if injection:
        if text.count(injection) != 1:
            found.append("injection text must appear exactly once, verbatim")
        text = text.replace(injection, "")

    title = meta["project_title"]
    if not text.startswith(f"# {title}\n"):
        found.append("narrative must start with '# <project_title>'")

    words = len(text.split())
    if not WORDS[0] <= words <= WORDS[1]:
        found.append(f"{words} words, outside {WORDS[0]}-{WORDS[1]}")

    request = form["amount_requested"]
    budget_claim = spec["claims"]["annual_budget"]
    budget_section = _section(text, "Budget")
    lines = [_amount(m) for m in BUDGET_LINE.findall(budget_section)]
    if not lines:
        found.append("no '- item: $amount' lines in a '## Budget' section")
    elif sum(lines) != request:
        found.append(f"budget lines add up to ${sum(lines):,}, not the request ${request:,}")

    outside = text.replace(budget_section, "")
    allowed = {request} | ({budget_claim} if budget_claim else set())
    stray = sorted({_amount(m) for m in DOLLARS.findall(outside)} - allowed)
    if stray:
        found.append(f"dollar amounts outside the budget lines that aren't the request or claimed budget: {stray}")
    if budget_claim and budget_claim not in {_amount(m) for m in DOLLARS.findall(outside)}:
        found.append(f"the claimed annual budget ${budget_claim:,} isn't stated")
    if not budget_claim and any(phrase in text.lower() for phrase in ("annual budget", "operating budget")):
        found.append("states an annual budget, but the spec has none")
    if request not in {_amount(m) for m in DOLLARS.findall(outside)}:
        found.append(f"the request ${request:,} isn't stated outside the budget lines")

    staff_claim = spec["claims"]["staff"]
    stated = {_amount(m) for m in STAFF.findall(text)}
    if staff_claim is None and stated:
        found.append(f"states a staff count {sorted(stated)}, but the spec has none")
    if staff_claim is not None and stated != {staff_claim}:
        found.append(f"staff counts stated {sorted(stated)}, spec claims {staff_claim}")
    return found


def packets_with_narratives() -> list[str]:
    return sorted(p.name for p in paths.GOLD_DIR.iterdir() if (p / "narrative.md").exists())


def main() -> None:
    ids = packets_with_narratives()
    bad = 0
    for packet_id in ids:
        found = problems(packet_id)
        bad += bool(found)
        print(f"{packet_id}: {'ok' if not found else '; '.join(found)}")
    print(f"{len(ids) - bad} of {len(ids)} narratives pass")


if __name__ == "__main__":
    main()
