"""batch.py with a fake Batches API: no API calls, no downloads."""

import json
from types import SimpleNamespace

import pymupdf
import pytest
from test_checks import gold_answers

import batch
import evaluate
from schema import from_answer


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "return.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=612, height=792)
        doc.save(path)
    return path


def usage(output_tokens=1000):
    return SimpleNamespace(
        input_tokens=2000, output_tokens=output_tokens, cache_creation_input_tokens=0, cache_read_input_tokens=5000
    )


def succeeded(custom_id, answer, stop_reason="end_turn", text=None):
    text = text if text is not None else from_answer(answer).model_dump_json()
    message = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)], usage=usage())
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message))


def failed(custom_id, kind):
    error = SimpleNamespace(error=SimpleNamespace(type="api_error", message="overloaded"))
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=kind, error=error))


def test_a_request_is_extract_pys_request_keyed_by_object_id(pdf):
    req = batch.request("111", pdf)
    params = req["params"]
    assert req["custom_id"] == "111"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert "blank_lines" in params["output_config"]["format"]["schema"]["properties"]
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][0]["content"][0]["type"] == "image"


def test_a_succeeded_result_becomes_a_scored_record_at_half_price():
    answer = gold_answers()[0]
    record = batch.record_from_result(succeeded("111", answer), "claude-sonnet-5", "image-1568", None)
    assert record["answer"] == answer and record["problems"] == [] and record["batch"] is True
    full = (2000 * 2.00 + 5000 * 0.20 + 1000 * 10.00) / 1_000_000
    assert record["cost_usd"] == pytest.approx(full * 0.5)
    assert record["seconds"] is None  # no time per filing in a batch


@pytest.mark.parametrize("kind", ["errored", "expired", "canceled"])
def test_a_request_that_did_not_run_is_recorded_without_an_answer(kind):
    record = batch.record_from_result(failed("111", kind), "claude-sonnet-5", "image-1568", None)
    assert record["answer"] is None and record["problems"][0].startswith(f"batch request {kind}")
    assert record["cost_usd"] == 0


def test_running_out_of_tokens_or_bad_json_leaves_no_answer():
    answer = gold_answers()[0]
    out = batch.record_from_result(succeeded("1", answer, "max_tokens"), "claude-sonnet-5", "image-1568", None)
    assert out["answer"] is None and "max_tokens" in out["problems"][0]
    bad = batch.record_from_result(succeeded("1", answer, text='{"ein": 5}'), "claude-sonnet-5", "image-1568", None)
    assert bad["answer"] is None and "schema" in bad["problems"][0]


class FakeBatches:
    """Stands in for client.messages.batches: one batch that ends after `polls` retrievals."""

    def __init__(self, answers, polls=1):
        self.answers, self.polls, self.created, self.result_calls = answers, polls, [], 0

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, batch_id):
        self.polls -= 1
        ended = self.polls <= 0
        counts = SimpleNamespace(processing=0 if ended else len(self.answers), succeeded=len(self.answers))
        return SimpleNamespace(
            processing_status="ended" if ended else "in_progress", request_counts=counts, ended_at=None
        )

    def results(self, batch_id):
        self.result_calls += 1
        return [succeeded(object_id, answer) for object_id, answer in self.answers.items()]


@pytest.fixture
def job(tmp_path, monkeypatch, pdf):
    keys = list(evaluate.gold().items())[:3]
    answers = {object_id: key["fields"] for object_id, key in keys}
    fake = FakeBatches(answers, polls=2)
    client = SimpleNamespace(messages=SimpleNamespace(batches=fake))
    monkeypatch.setattr(batch, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(batch.scans, "source_pdf", lambda object_id, scan: pdf)
    return client, fake, list(answers)


def test_submit_records_the_batch_before_waiting(job):
    client, fake, object_ids = job
    run_dir = batch.submit(client, object_ids, model="claude-sonnet-5", input_kind="image-1568", scan=None)
    state = json.loads((run_dir / batch.STATE).read_text(encoding="utf-8"))
    assert state["batches"] == [{"id": "msgbatch_1", "object_ids": object_ids}] and state["collected"] == []
    assert [r["custom_id"] for r in fake.created[0]] == object_ids
    assert not evaluate.is_complete(run_dir)  # nothing collected yet: not scored as a run


def test_collect_waits_saves_every_result_and_is_safe_to_repeat(job):
    client, fake, object_ids = job
    run_dir = batch.submit(client, object_ids, model="claude-sonnet-5", input_kind="image-1568", scan=None)
    assert batch.collect(client, run_dir, wait=False) is False  # still in progress: come back later
    assert batch.collect(client, run_dir, wait=True) is True
    assert evaluate.is_complete(run_dir) and evaluate.score_run(run_dir)["field_accuracy"] == 1.0
    calls = fake.result_calls
    assert batch.collect(client, run_dir, wait=True) is True  # collecting again changes nothing
    assert fake.result_calls == calls and len(fake.created) == 1  # no new download, never a second batch
