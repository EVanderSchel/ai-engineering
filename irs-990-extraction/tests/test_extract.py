"""extract.py with a fake client: no API calls, no credits spent."""

import base64
import json
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


BLOCK = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "cG5n"}}


@pytest.fixture
def letter_pdf(tmp_path):
    """A three-page US Letter PDF whose pages say which page they are."""
    path = tmp_path / "return.pdf"
    with pymupdf.open() as doc:
        for number in range(1, 4):
            doc.new_page(width=612, height=792).insert_text((72, 72), f"page {number}")
        doc.save(path)
    return path


def test_pdf_input_is_page_1_alone(letter_pdf):
    with pymupdf.open(stream=extract.page_pdf(letter_pdf), filetype="pdf") as single:
        assert single.page_count == 1 and "page 1" in single[0].get_text()


def test_page_block_types(letter_pdf):
    pdf = extract.page_block(letter_pdf, "pdf")
    assert pdf["type"] == "document" and pdf["source"]["media_type"] == "application/pdf"
    image = extract.page_block(letter_pdf, "image-1000")
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    height = pymupdf.Pixmap(base64.standard_b64decode(image["source"]["data"])).height
    assert height == 1000


@pytest.mark.parametrize("bad", ["image", "image-0", "image-3000", "png-1568", "PDF"])
def test_unknown_inputs_are_rejected(bad):
    with pytest.raises(ValueError):
        extract.check_input(bad)


def test_extract_records_the_input_it_was_given(good):
    result = extract.extract(FakeClient(reply(good)), BLOCK, input_kind="pdf")
    assert result.input == "pdf"


@pytest.fixture
def good():
    return gold_answers()[0]


@pytest.fixture
def misread(good):
    return good | {"total_revenue_current_year": (good["total_revenue_current_year"] or 0) + 1}


def test_a_correct_first_answer_is_returned_without_a_retry(good):
    client = FakeClient(reply(good))
    result = extract.extract(client, BLOCK)

    assert result.answer == good and result.problems == [] and len(result.attempts) == 1
    request = client.requests[0]
    assert request["model"] == "claude-sonnet-5" and request["output_format"] is Form990PartI
    assert request["system"] == extract.prompts.load("extract").template
    assert request["messages"][0]["content"][0] is BLOCK
    assert result.input == "image-1568"
    assert result.prompt == extract.prompts.load("extract").id  # the active version


def test_a_failed_check_retries_in_the_same_conversation_with_the_broken_rules(good, misread):
    client = FakeClient(reply(misread), reply(good))
    result = extract.extract(client, BLOCK)

    assert result.answer == good and result.problems == [] and len(result.attempts) == 2
    assert result.attempts[0].problems  # the first answer's problems are kept for the record
    first, assistant, retry = client.requests[1]["messages"]
    assert first == client.requests[0]["messages"][0]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}  # unchanged
    assert assistant["content"][1] == {"type": "text", "text": "{...}"}  # no SDK-only parsed_output
    assert "Line 12 = lines 8-11" in retry["content"]


def test_retries_stop_after_max_attempts_and_report_what_is_still_wrong(misread):
    client = FakeClient(reply(misread), reply(misread))
    result = extract.extract(client, BLOCK)

    assert len(result.attempts) == 2 and result.answer == misread
    assert result.problems and result.problems[0].startswith("Line 12")


def test_a_refusal_is_not_retried():
    client = FakeClient(reply(None, stop_reason="refusal"))
    result = extract.extract(client, BLOCK)

    assert result.answer is None and len(result.attempts) == 1
    assert result.problems == ["Claude stopped without an answer (refusal)"]


def test_cost_adds_up_every_attempt(good, misread):
    client = FakeClient(
        reply(misread, input_tokens=3000, output_tokens=1000), reply(good, input_tokens=4500, output_tokens=800)
    )
    result = extract.extract(client, BLOCK)

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


def test_cache_marks_the_system_prompt(good):
    client = FakeClient(reply(good), reply(good))
    extract.extract(client, BLOCK)
    extract.extract(client, BLOCK, cache=True)
    plain, cached = (request["system"] for request in client.requests)
    assert isinstance(plain, str)
    assert cached == [{"type": "text", "text": plain, "cache_control": {"type": "ephemeral"}}]


def test_an_api_error_stops_the_run_and_marks_it_incomplete(good, tmp_path, monkeypatch):
    class Broke(FakeClient):
        def parse(self, **request):
            if len(self.requests) == 1:
                raise extract.anthropic.APIConnectionError(message="credit balance is too low", request=None)
            return super().parse(**request)

    # Stand-ins for the downloaded PDFs (data/raw is gitignored, so CI doesn't have them; page_block is
    # faked, so the files are never read, only checked for).
    raw = tmp_path / "raw"
    (raw / "pdf").mkdir(parents=True)
    for object_id in extract.evaluate.gold():
        (raw / "pdf" / f"{object_id}.pdf").touch()

    client = Broke(reply(good))
    monkeypatch.setattr(extract.anthropic, "Anthropic", lambda: client)
    monkeypatch.setattr(extract.scans, "RAW_DIR", raw)
    monkeypatch.setattr(extract, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(extract, "page_block", lambda pdf, kind: BLOCK)
    monkeypatch.setattr("sys.argv", ["extract.py", "--limit", "3"])

    with pytest.raises(SystemExit, match="Stopped after 1 of 3"):
        extract.main()
    [run] = (tmp_path / "runs").iterdir()
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["complete"] is False and summary["filings"] == 1 and summary["of"] == 3
    assert "credit balance" in summary["error"]
    assert not extract.evaluate.is_complete(run)


def test_confidence_needs_a_prompt_that_explains_it(good, monkeypatch):
    monkeypatch.setenv("IRS990_PROMPT_EXTRACT", "v2")
    with pytest.raises(ValueError, match="unsure_fields"):
        extract.extract(FakeClient(reply(good)), BLOCK, confidence=True)


def test_confidence_asks_for_doubts_and_records_them(good, monkeypatch):
    from schema import Form990PartIWithDoubts

    monkeypatch.setenv("IRS990_PROMPT_EXTRACT", "v3")
    answer = reply(good)
    answer.parsed_output = Form990PartIWithDoubts(**answer.parsed_output.model_dump(), unsure_fields=["volunteers"])
    client = FakeClient(answer)
    result = extract.extract(client, BLOCK, confidence=True)
    assert client.requests[0]["output_format"] is Form990PartIWithDoubts
    assert result.unsure == ["volunteers"] and result.answer == good and result.prompt == "extract/v3"
    plain = extract.extract(FakeClient(reply(good)), BLOCK)
    assert plain.unsure is None  # not asked, which is different from "asked, and sure of everything"


def test_a_thinking_budget_is_sent_only_when_set(good):
    client = FakeClient(reply(good), reply(good))
    plain = extract.extract(client, BLOCK, model="claude-haiku-4-5")
    budgeted = extract.extract(client, BLOCK, model="claude-haiku-4-5", thinking_budget=4000)
    assert "thinking" not in client.requests[0]
    assert client.requests[1]["thinking"] == {"type": "enabled", "budget_tokens": 4000}
    assert (plain.thinking_budget, budgeted.thinking_budget) == (None, 4000)


def test_an_answer_that_is_not_valid_json_is_one_failed_filing_not_a_crash():
    class Broken(FakeClient):
        def parse(self, **request):  # what the SDK does with a malformed answer: validate it, and raise
            self.requests.append(request)
            Form990PartI.model_validate_json("{not json")

    result = extract.extract(Broken(), BLOCK, model="claude-haiku-4-5")
    assert result.answer is None and "valid JSON" in result.problems[0]
    assert [a.stop_reason for a in result.attempts] == ["invalid_json"]
