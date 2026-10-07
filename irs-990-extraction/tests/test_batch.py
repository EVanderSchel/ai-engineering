"""batch.py with a fake Batches API: no API calls, no downloads."""

import json
from types import SimpleNamespace

import pymupdf
import pytest

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


STATE = {"model": "claude-sonnet-5", "input": "image-1568", "scan": None}


def usage():
    return SimpleNamespace(
        input_tokens=2000, output_tokens=1000, cache_creation_input_tokens=0, cache_read_input_tokens=5000
    )


def succeeded(custom_id, answer, stop_reason="end_turn", text=None):
    text = text if text is not None else from_answer(answer).model_dump_json()
    thinking = SimpleNamespace(type="thinking", to_dict=lambda: {"type": "thinking", "thinking": "", "signature": "s"})
    content = [thinking, SimpleNamespace(type="text", text=text)]
    message = SimpleNamespace(stop_reason=stop_reason, content=content, usage=usage())
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message))


def failed(custom_id, kind, error_type="api_error"):
    error = SimpleNamespace(error=SimpleNamespace(type=error_type, message="something went wrong"))
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=kind, error=error))


# --- Requests and results --------------------------------------------------------------------------


def test_a_first_request_is_extract_pys_request_keyed_by_object_id(pdf):
    req = batch.first_request("111", pdf)
    params = req["params"]
    assert req["custom_id"] == "111" and params["output_config"]["format"]["type"] == "json_schema"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [m["role"] for m in params["messages"]] == ["user"]


def test_a_retry_request_continues_the_conversation_with_the_broken_rules(pdf):
    history = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "{...}"}]
    req = batch.retry_request("111", pdf, history, ["Line 12 = lines 8-11: ..."], "image-1568", "claude-sonnet-5")
    messages = req["params"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == history and "Line 12 = lines 8-11" in messages[2]["content"]


def test_requests_are_grouped_under_both_limits():
    reqs = [{"custom_id": str(i), "params": {"x": "y" * 100}} for i in range(10)]
    size = len(json.dumps(reqs[0]))
    groups = list(batch.chunks(iter(reqs), max_requests=4, max_bytes=10_000))
    assert [len(g) for g in groups] == [4, 4, 2]
    groups = list(batch.chunks(iter(reqs), max_requests=100, max_bytes=3 * size))
    assert [len(g) for g in groups] == [3, 3, 3, 1]


def test_reading_results():
    answer = evaluate.gold()[next(iter(evaluate.gold()))]["fields"]
    kind, extraction, history = batch.read_result(succeeded("1", answer), STATE)
    assert kind == "ok" and extraction.answer == answer and extraction.problems == []
    assert history[0]["type"] == "thinking" and history[1] == {
        "type": "text",
        "text": from_answer(answer).model_dump_json(),
    }
    assert batch.read_result(failed("1", "errored"), STATE)[0] == "retry"
    assert batch.read_result(failed("1", "expired"), STATE)[0] == "retry"
    kind, extraction, _ = batch.read_result(failed("1", "errored", "invalid_request_error"), STATE)
    assert kind == "failed" and extraction.problems[0].startswith("batch request invalid")
    kind, extraction, _ = batch.read_result(succeeded("1", answer, text='{"ein": 5}'), STATE)
    assert kind == "ok" and extraction.answer is None and "schema" in extraction.problems[0]


# --- The job ---------------------------------------------------------------------------------------


class FakeBatches:
    """Stands in for client.messages.batches. `script` maps each object ID to the results it gets, one per
    time it's sent; every batch ends at its second retrieval."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.batches, self.retrievals = {}, {}

    def create(self, requests):
        batch_id = f"msgbatch_{len(self.batches) + 1}"
        self.batches[batch_id] = [self.script[r["custom_id"]].pop(0)(r) for r in requests]
        self.retrievals[batch_id] = 0
        return SimpleNamespace(id=batch_id)

    def retrieve(self, batch_id):
        self.retrievals[batch_id] += 1
        ended = self.retrievals[batch_id] >= 2
        counts = SimpleNamespace(processing=0 if ended else len(self.batches[batch_id]))
        return SimpleNamespace(
            processing_status="ended" if ended else "in_progress", request_counts=counts, ended_at=None
        )

    def results(self, batch_id):
        return self.batches[batch_id]


@pytest.fixture
def setup(tmp_path, monkeypatch, pdf):
    keys = list(evaluate.gold().items())[:3]
    monkeypatch.setattr(batch, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(batch.scans, "source_pdf", lambda object_id, scan: pdf)
    monkeypatch.setattr(batch, "POLL_SECONDS", 0)
    return {object_id: key["fields"] for object_id, key in keys}


def run(script, answers, wait=True):
    fake = FakeBatches(script)
    client = SimpleNamespace(messages=SimpleNamespace(batches=fake))
    job = batch.Job.create(list(answers), model="claude-sonnet-5", input_kind="image-1568", scan=None)
    done = job.run(client, wait=wait)
    return job, fake, client, done


def ok(answer):
    return lambda req: succeeded(req["custom_id"], answer)


def test_a_clean_job_finishes_in_one_batch(setup):
    script = {object_id: [ok(answer)] for object_id, answer in setup.items()}
    job, fake, _, done = run(script, setup)
    assert done and len(fake.batches) == 1
    assert evaluate.is_complete(job.run_dir) and evaluate.score_run(job.run_dir)["field_accuracy"] == 1.0


def test_without_waiting_the_job_stops_and_resumes_without_resubmitting(setup):
    script = {object_id: [ok(answer)] for object_id, answer in setup.items()}
    job, fake, client, done = run(script, setup, wait=False)
    assert not done and len(fake.batches) == 1 and not evaluate.is_complete(job.run_dir)
    resumed = batch.Job(job.run_dir)  # as if from a new process: everything comes from batch_state.json
    assert resumed.run(client, wait=False) and len(fake.batches) == 1  # collected, never sent twice
    assert evaluate.is_complete(job.run_dir)


def test_failed_requests_are_sent_again_and_invalid_ones_are_not(setup):
    (a, answer_a), (b, answer_b), (c, answer_c) = setup.items()
    script = {
        a: [lambda req: failed(a, "errored"), lambda req: failed(a, "expired"), ok(answer_a)],  # third time lucky
        b: [lambda req: failed(b, "errored", "invalid_request_error")],  # never resent
        c: [lambda req: failed(c, "errored")] * 3,  # gives up after MAX_TRIES
    }
    job, fake, _, done = run(script, setup)
    assert done and len(fake.batches) == 3
    records = {i: json.loads((job.run_dir / f"{i}.json").read_text(encoding="utf-8")) for i in setup}
    assert records[a]["answer"] == answer_a
    assert records[b]["answer"] is None and "invalid" in records[b]["problems"][0]
    assert records[c]["answer"] is None and "3 times" in records[c]["problems"][0]


def test_an_answer_that_breaks_the_arithmetic_gets_one_retry(setup):
    (a, answer_a), (b, answer_b), (c, answer_c) = setup.items()
    misread = answer_a | {"total_revenue_current_year": (answer_a["total_revenue_current_year"] or 0) + 1}
    sent = []

    def first(req):
        sent.append(req)
        return succeeded(a, misread)

    def second(req):
        sent.append(req)
        return succeeded(a, answer_a)

    script = {a: [first, second], b: [ok(answer_b)], c: [ok(answer_c)]}
    job, fake, _, done = run(script, setup)
    assert done and len(fake.batches) == 2 and len(fake.batches["msgbatch_2"]) == 1  # only the retry
    assert [m["role"] for m in sent[1]["params"]["messages"]] == ["user", "assistant", "user"]
    record = json.loads((job.run_dir / f"{a}.json").read_text(encoding="utf-8"))
    assert record["answer"] == answer_a and record["problems"] == [] and len(record["attempts"]) == 2
    assert record["usage"]["output_tokens"] == 2000  # both attempts' tokens
    assert record["cost_usd"] == pytest.approx(2 * (2000 * 2.00 + 5000 * 0.20 + 1000 * 10.00) / 1e6 * 0.5)


def test_a_retry_that_still_fails_its_checks_keeps_both_attempts(setup):
    (a, answer_a), (b, answer_b), (c, answer_c) = setup.items()
    misread = answer_a | {"total_revenue_current_year": (answer_a["total_revenue_current_year"] or 0) + 1}
    script = {a: [ok(misread), ok(misread)], b: [ok(answer_b)], c: [ok(answer_c)]}
    job, fake, _, done = run(script, setup)
    record = json.loads((job.run_dir / f"{a}.json").read_text(encoding="utf-8"))
    assert done and len(record["attempts"]) == 2 and record["problems"]  # no third attempt
    assert evaluate.score_run(job.run_dir)["check_failures"] == 1
