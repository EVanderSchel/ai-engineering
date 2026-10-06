"""part_vii.py: schema, checks, and extraction with a fake streaming client (no API, no downloads)."""

import json
from types import SimpleNamespace

import pymupdf
import pytest
from anthropic.lib._parse._transform import transform_schema

import part_vii
from paths import GOLD_DIR


def gold_parts() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))["part_vii"] for p in sorted(GOLD_DIR.glob("2*.json"))]


def as_extraction(part: dict) -> part_vii.PartVII:
    return part_vii.PartVII.model_validate({"rows": part["rows"], **part["totals"]})


@pytest.fixture
def paid():
    """An answer key with at least two paid rows."""
    return next(p for p in gold_parts() if sum(r["pay"] > 0 for r in p["rows"]) >= 2)


def test_every_answer_key_passes_the_checks():
    parts = gold_parts()
    assert len(parts) == 60
    for part in parts:
        assert part_vii.problems(part) == []


def test_every_answer_key_fits_the_schema_and_round_trips():
    for part in gold_parts():
        assert part_vii.as_answer(as_extraction(part)) == part


def test_schema_stays_under_the_union_limit():
    schema = json.dumps(transform_schema(part_vii.PartVII))
    assert schema.count('"anyOf"') <= 16  # the API rejects more nullable fields than that


def test_a_missing_paid_row_breaks_the_column_total(paid):
    row = next(r for r in paid["rows"] if r["pay"] > 0)
    paid["rows"].remove(row)
    found = part_vii.problems(paid)
    assert found and found[0].startswith("Column (D)") and f"difference of {row['pay']}" in found[0]


def test_rounding_up_to_a_dollar_per_row_is_allowed(paid):
    paid["totals"]["total_other_pay"] = (paid["totals"]["total_other_pay"] or 0) + 1
    assert part_vii.problems(paid) == []


def test_more_people_over_100k_than_line_2_is_a_problem(paid):
    paid["totals"]["people_over_100k"] = 0
    paid["rows"][0]["pay"] += 200_000
    paid["totals"]["total_pay"] = (paid["totals"]["total_pay"] or 0) + 200_000
    [found] = part_vii.problems(paid)
    assert found.startswith("Line 2")


class FakeStreamClient:
    """Plays back canned replies through the streaming interface and records each request."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.messages = self

    def stream(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        return _Stream(self.replies.pop(0))


class _Stream:
    """Stands in for the SDK's MessageStream: a context manager with get_final_message()."""

    def __init__(self, final):
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.final


def reply(part: dict, stop_reason: str = "end_turn"):
    parsed = as_extraction(part)
    block = SimpleNamespace(type="text", text="{...}", parsed_output=parsed)
    usage = SimpleNamespace(
        input_tokens=5000, output_tokens=2000, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, content=[block], usage=usage)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "return.pdf"
    with pymupdf.open() as doc:
        for _ in range(16):
            doc.new_page(width=612, height=792)
        doc.save(path)
    return path


def test_pages_are_sent_in_order_each_labeled(paid, pdf):
    client = FakeStreamClient(reply(paid))
    result = part_vii.extract(client, pdf, [7, 8, 14])
    content = client.requests[0]["messages"][0]["content"]
    labels = [block["text"] for block in content if block["type"] == "text"]
    assert labels[:3] == ["Page 7:", "Page 8:", "Page 14:"]
    assert [block["type"] for block in content].count("image") == 3
    assert client.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert result.answer == paid and result.problems == [] and len(result.attempts) == 1


def test_a_list_that_does_not_add_up_is_retried_with_the_gap(paid, pdf):
    short = {"rows": [r for r in paid["rows"] if r["pay"] == 0] + [r for r in paid["rows"] if r["pay"]][1:]}
    short["totals"] = paid["totals"]
    client = FakeStreamClient(reply(short), reply(paid))
    result = part_vii.extract(client, pdf, [7, 8])
    assert len(result.attempts) == 2 and result.answer == paid and result.problems == []
    retry = client.requests[1]["messages"][-1]["content"]
    assert "Column (D)" in retry and "skipped" in retry
    assert client.requests[1]["messages"][1]["content"] == [{"type": "text", "text": "{...}"}]


def test_cost_counts_both_attempts(paid, pdf):
    short = {"rows": [], "totals": paid["totals"]}
    result = part_vii.extract(FakeStreamClient(reply(short), reply(paid)), pdf, [7, 8])
    assert result.cost_usd == pytest.approx(2 * (5000 * 2.00 + 2000 * 10.00) / 1_000_000)
