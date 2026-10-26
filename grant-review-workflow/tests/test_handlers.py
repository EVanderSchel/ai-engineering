"""The Lambda handlers, without AWS: stand-in steps (tests/test_workflow.right_answers), an in-memory
checkpointer and status store, and a fake S3 serving a packet from a temporary folder."""

import datetime as dt
import json
import shutil

import pymupdf
import pytest
from pydantic import ValidationError
from test_workflow import right_answers

import applications
import handlers
import workflow
from intake import gold_packet


class FakeS3:
    """download_file from a folder laid out like the intake bucket: <bucket>/<application_id>/<name>."""

    def __init__(self, root):
        self.root = root

    def download_file(self, bucket, key, filename):
        shutil.copyfile(self.root / bucket / key, filename)


@pytest.fixture
def lambda_env(tmp_path, monkeypatch):
    app = workflow.build(right_answers(), checkpointer=workflow.memory_checkpointer())
    monkeypatch.setattr(handlers, "_workflow", lambda: app)
    monkeypatch.setattr(handlers, "_s3", lambda: FakeS3(tmp_path / "s3"))
    store = applications.MemoryStore()
    monkeypatch.setattr(handlers, "_applications", lambda: store)
    monkeypatch.setattr(handlers, "WORK_DIR", tmp_path / "work")
    return tmp_path / "s3" / "intake"


def upload(bucket, application_id, packet="gp-027", narrative=None):
    """A gold packet uploaded as an applicant would, with a one-page placeholder return."""
    form, text, _ = gold_packet(packet)
    folder = bucket / application_id
    folder.mkdir(parents=True)
    (folder / "narrative.md").write_text(text if narrative is None else narrative, encoding="utf-8")
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(folder / "return.pdf")
    (folder / "form.json").write_text(json.dumps(form), encoding="utf-8")


def created(key):
    """The shape of S3's 'Object Created' event as EventBridge delivers it."""
    return {
        "version": "0",
        "detail-type": "Object Created",
        "source": "aws.s3",
        "detail": {"bucket": {"name": "intake"}, "object": {"key": key, "size": 200}},
    }


def decision(**changes):
    base = {
        "decision": "approve",
        "reason": "Strong outcomes and a realistic budget.",
        "reviewer": "reviewer@example.org",
        "decided_at": dt.datetime(2025, 7, 1, tzinfo=dt.UTC).isoformat(),
    }
    return {**base, **changes}


def test_an_upload_runs_the_workflow_until_it_waits_for_the_reviewer(lambda_env, caplog):
    upload(lambda_env, "gp-027-app1")
    with caplog.at_level("INFO", logger="grant_review"):
        result = handlers.start(created("gp-027-app1/form.json"))
    assert result["route"] == "review" and result["waiting_for_review"] and not result["letter_ready"]
    [record] = caplog.records
    assert record.event == "started" and record.application_id == "gp-027-app1" and record.waiting_for_review
    assert "## Who it serves" not in caplog.text  # no applicant text in the logs


def test_a_retried_event_continues_the_saved_thread(lambda_env):
    upload(lambda_env, "gp-027-app2")
    first = handlers.start(created("gp-027-app2/form.json"))
    again = handlers.start(created("gp-027-app2/form.json"))  # Lambda's retry, or a duplicate event
    assert again == first


def test_a_decision_resumes_the_application_and_returns_its_letter(lambda_env):
    upload(lambda_env, "gp-027-app3")
    handlers.start(created("gp-027-app3/form.json"))
    result = handlers.resume({"application_id": "gp-027-app3", "decision": decision()})
    assert not result["waiting_for_review"] and result["letter_ready"] and result["letter"]


def test_each_run_writes_where_the_application_now_stands(lambda_env):
    store = handlers._applications()
    upload(lambda_env, "gp-027-app7")
    handlers.start(created("gp-027-app7/form.json"))
    assert [r.application_id for r in store.list("waiting_for_review")] == ["gp-027-app7"]
    handlers.resume({"application_id": "gp-027-app7", "decision": decision()})
    assert store.list("waiting_for_review") == [] and store.get("gp-027-app7").decision == "approve"


def test_a_rejected_packet_writes_no_record(lambda_env):
    upload(lambda_env, "gp-027-app8", narrative="   ")
    handlers.start(created("gp-027-app8/form.json"))
    assert handlers._applications().get("gp-027-app8") is None


def test_an_invalid_decision_never_reaches_the_graph(lambda_env):
    upload(lambda_env, "gp-027-app4")
    handlers.start(created("gp-027-app4/form.json"))
    with pytest.raises(ValidationError):
        handlers.resume({"application_id": "gp-027-app4", "decision": decision(reason="")})
    assert handlers.resume({"application_id": "gp-027-app4", "decision": decision()})["letter_ready"]  # not stuck


def test_only_a_waiting_application_can_be_resumed(lambda_env):
    with pytest.raises(ValueError, match="isn't waiting"):
        handlers.resume({"application_id": "gp-027-never", "decision": decision()})


def test_a_malformed_packet_is_rejected_without_a_retry(lambda_env):
    upload(lambda_env, "gp-027-app5", narrative="   ")
    result = handlers.start(created("gp-027-app5/form.json"))
    assert result == {"application_id": "gp-027-app5", "rejected": ["narrative is empty"]}


@pytest.mark.parametrize("key", ["gp-027-app6/narrative.md", "../escape/form.json", "form.json", "a b/form.json"])
def test_only_a_form_json_under_a_safe_id_starts_a_run(lambda_env, key):
    with pytest.raises(ValueError):
        handlers.start(created(key))


def test_the_checkpoint_library_logs_warnings_only():
    import logging

    assert logging.getLogger("langgraph_checkpoint_aws.checkpoint.dynamodb.unified_repository").getEffectiveLevel() == (
        logging.WARNING
    )
