"""parallel.py with a fake client that records how many requests are in flight: no API, no downloads."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

import evaluate
import extract
import parallel
from schema import from_answer


class FakeClient:
    """Answers each filing with its answer key after a short pause, tracking concurrency."""

    def __init__(self, keys, fail_on=None):
        self.keys, self.fail_on = keys, fail_on
        self.lock, self.in_flight, self.peak, self.order = threading.Lock(), 0, 0, []
        self.messages = self

    def parse(self, **request):
        object_id = request["messages"][0]["content"][0]["object_id"]
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.order.append(("start", object_id, self.in_flight))
        try:
            time.sleep(0.1)
            if object_id == self.fail_on:
                raise extract.anthropic.APIConnectionError(message="credit balance is too low", request=None)
            usage = SimpleNamespace(
                input_tokens=2000, output_tokens=1000, cache_creation_input_tokens=0, cache_read_input_tokens=5000
            )
            parsed = from_answer(self.keys[object_id])
            return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, content=[], usage=usage)
        finally:
            with self.lock:
                self.in_flight -= 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    keys = {object_id: key["fields"] for object_id, key in list(evaluate.gold().items())[:10]}
    rows = [{"object_id": object_id, "band": "small"} for object_id in keys]
    monkeypatch.setattr(extract.scans, "source_pdf", lambda object_id, scan: object_id)
    monkeypatch.setattr(extract, "page_block", lambda pdf, kind: {"type": "image", "object_id": pdf})
    run_dir = tmp_path / "20261007-100000_claude-sonnet-5_v2_image-1568_cache_parallel-4"
    run_dir.mkdir()
    return keys, rows, run_dir


def test_the_first_filing_goes_alone_then_the_rest_fan_out(setup):
    keys, rows, run_dir = setup
    client = FakeClient(keys)
    summary = parallel.run(
        client, rows, run_dir, workers=4, model="claude-sonnet-5", input_kind="image-1568", scan=None
    )
    assert client.order[0] == ("start", rows[0]["object_id"], 1)
    assert all(depth >= 1 for _, _, depth in client.order) and 1 < client.peak <= 4
    assert summary["complete"] and summary["filings"] == 10 and summary["workers"] == 4
    assert evaluate.is_complete(run_dir) and evaluate.score_run(run_dir)["field_accuracy"] == 1.0


def test_parallel_is_faster_than_one_at_a_time(setup):
    keys, rows, run_dir = setup
    started = time.monotonic()
    parallel.run(
        FakeClient(keys), rows, run_dir, workers=8, model="claude-sonnet-5", input_kind="image-1568", scan=None
    )
    assert time.monotonic() - started < 10 * 0.1 * 0.6  # one at a time would take 1 s; 8 at a time about 0.3 s


def test_an_unrecoverable_error_stops_the_run_and_marks_it_incomplete(setup):
    keys, rows, run_dir = setup
    client = FakeClient(keys, fail_on=rows[3]["object_id"])
    summary = parallel.run(
        client, rows, run_dir, workers=2, model="claude-sonnet-5", input_kind="image-1568", scan=None
    )
    assert not summary["complete"] and "credit balance" in summary["error"]
    assert summary["filings"] < len(rows)
    saved = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert saved["complete"] is False and not evaluate.is_complete(run_dir)
