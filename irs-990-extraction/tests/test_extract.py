"""extract.py with a fake client: no API calls, no credits spent."""

from types import SimpleNamespace

import pymupdf
import pytest
from test_checks import gold_answers

import extract
from schema import Form990PartI, from_answer


class FakeBlock(SimpleNamespace):
    def to_dict(self):
        return dict(vars(self))


def reply(answer: dict | None, stop_reason: str = "end_turn", input_tokens: int = 3000, output_tokens: int = 1000):
    parsed = None if answer is None else from_answer(answer)
    content = [
        FakeBlock(type="thinking", thinking="", signature="sig"),
        FakeBlock(type="text", text="{...}", parsed_output=parsed),
    ]
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=0,
    )
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, content=content, usage=usage)


class FakeClient:
    """Plays back canned replies and records what each request sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = self

    def parse(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        return self.replies.pop(0)


@pytest.fixture
def good():
    return gold_answers()[0]


@pytest.fixture
def misread(good):
    return good | {"total_revenue_current_year": (good["total_revenue_current_year"] or 0) + 1}


def test_a_correct_first_answer_is_returned_without_a_retry(good):
    client = FakeClient(reply(good))
    result = extract.extract(client, b"png")

    assert result.answer == good and result.problems == [] and len(result.attempts) == 1
    request = client.requests[0]
    assert request["model"] == "claude-sonnet-5" and request["output_format"] is Form990PartI
    assert request["system"] == extract.prompts.load("extract").template
    assert request["messages"][0]["content"][0]["type"] == "image"
    assert result.prompt == "extract/v1"


def test_a_failed_check_retries_in_the_same_conversation_with_the_broken_rules(good, misread):
    client = FakeClient(reply(misread), reply(good))
    result = extract.extract(client, b"png")

    assert result.answer == good and result.problems == [] and len(result.attempts) == 2
    assert result.attempts[0].problems  # the first answer's problems are kept for the record
    first, assistant, retry = client.requests[1]["messages"]
    assert first == client.requests[0]["messages"][0]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}  # unchanged
    assert assistant["content"][1] == {"type": "text", "text": "{...}"}  # no SDK-only parsed_output
    assert "Line 12 = lines 8-11" in retry["content"]


def test_retries_stop_after_max_attempts_and_report_what_is_still_wrong(misread):
    client = FakeClient(reply(misread), reply(misread))
    result = extract.extract(client, b"png")

    assert len(result.attempts) == 2 and result.answer == misread
    assert result.problems and result.problems[0].startswith("Line 12")


def test_a_refusal_is_not_retried():
    client = FakeClient(reply(None, stop_reason="refusal"))
    result = extract.extract(client, b"png")

    assert result.answer is None and len(result.attempts) == 1
    assert result.problems == ["Claude stopped without an answer (refusal)"]


def test_cost_adds_up_every_attempt(good, misread):
    client = FakeClient(
        reply(misread, input_tokens=3000, output_tokens=1000), reply(good, input_tokens=4500, output_tokens=800)
    )
    result = extract.extract(client, b"png")

    assert (result.usage.input_tokens, result.usage.output_tokens) == (7500, 1800)
    assert result.cost_usd == pytest.approx((7500 * 2.00 + 1800 * 10.00) / 1_000_000)


def test_cache_tokens_are_priced_at_their_own_rates():
    usage = extract.Usage(
        input_tokens=1_000_000, cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000
    )
    assert usage.cost("claude-sonnet-5") == pytest.approx(2.00 + 2.50 + 0.20)


def test_page_is_rendered_to_a_png_with_the_requested_long_edge(tmp_path):
    pdf = tmp_path / "letter.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=612, height=792)  # US Letter, in points
        doc.save(pdf)

    png = extract.page_png(pdf, long_edge=1568)
    image = pymupdf.Pixmap(png)  # pixel size (opened as a document, an image is measured in points)
    assert png.startswith(b"\x89PNG") and (image.width, image.height) == (round(1568 * 612 / 792), 1568)


def test_the_test_split_needs_a_deliberate_flag(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["extract.py", "--split", "test"])
    with pytest.raises(SystemExit):
        extract.main()
    assert "held out" in capsys.readouterr().err
