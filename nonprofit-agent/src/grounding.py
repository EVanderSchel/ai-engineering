"""Whether the figures in an answer come from tool results: the "grounded" check.

Used twice: by the agent before it returns an answer (a guardrail: figures no tool returned are sent back to
the model to retrieve or remove), and by the eval's grader afterwards (src/grade.py).

A figure is a dollar amount or a percentage. It "matches" a value within a small tolerance (1% for dollars,
0.1 point for percentages) or when it equals the value rounded to the precision written ("$13 million" for
12,797,138; "68%" for 67.64%). Signs are ignored: "fell by $34,955" reports -34,955.
"""

import json
import re
from dataclasses import dataclass

USD_TOLERANCE = 0.01  # relative
PCT_TOLERANCE = 0.1  # percentage points

UNITS = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}
NUMBER = re.compile(
    r"(?P<dollar>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<pct>%|percent\b)|\s?(?P<unit>thousand|million|billion|bn|[kKmMbB])\b)?"
)


@dataclass(frozen=True)
class Figure:
    kind: str  # "usd" or "pct"
    value: float  # dollars, or percentage points (67.64 for 67.64%)
    decimals: int  # as written: "12.8 million" -> 1
    unit: float  # multiplier as written: "12.8 million" -> 1e6


def figures(text: str) -> list[Figure]:
    """Dollar amounts and percentages in text. A bare number counts as dollars only when written with
    thousands separators ("12,797,138"), so years, EIN digits and counts ("27 affiliates") are ignored."""
    found = []
    for m in NUMBER.finditer(text):
        raw = m["num"]
        number = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        if m["pct"]:
            found.append(Figure("pct", number, decimals, 1.0))
        elif m["unit"]:
            unit = UNITS[m["unit"].lower()]
            found.append(Figure("usd", number * unit, decimals, unit))
        elif m["dollar"] or "," in raw:
            found.append(Figure("usd", number, decimals, 1.0))
    return found


def matches(true_value: float, figure: Figure) -> bool:
    """Whether a written figure reports true_value (same kind assumed): within tolerance, or the true value
    rounded to the figure's own precision."""
    true_value = abs(true_value)
    if figure.kind == "pct":
        if abs(figure.value - true_value) <= PCT_TOLERANCE:
            return True
    elif abs(figure.value - true_value) <= USD_TOLERANCE * max(true_value, 1):
        return True
    return round(true_value / figure.unit, figure.decimals) == round(figure.value / figure.unit, figure.decimals)


def _numbers_in(value) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [float(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _numbers_in(v)]
    if isinstance(value, list):
        return [n for v in value for n in _numbers_in(v)]
    return []


def tool_numbers(trajectory: list[dict]) -> list[float]:
    """Every number in every successful tool result."""
    numbers = []
    for event in trajectory:
        if event["type"] == "tool" and not event["is_error"]:
            numbers += _numbers_in(json.loads(event["output"]))
    return numbers


def ungrounded(answer: str, trajectory: list[dict]) -> list[Figure]:
    """Figures in the answer that no tool returned. Tool ratios are fractions (0.6764), so a percentage
    is checked against each number both as is and x100."""
    numbers = tool_numbers(trajectory)
    missing = []
    for f in figures(answer):
        candidates = numbers if f.kind == "usd" else numbers + [n * 100 for n in numbers]
        if not any(matches(n, f) for n in candidates):
            missing.append(f)
    return missing


def describe(figure: Figure) -> str:
    return f"${figure.value:,.0f}" if figure.kind == "usd" else f"{figure.value:g}%"
