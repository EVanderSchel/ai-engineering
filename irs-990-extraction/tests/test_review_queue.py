"""review_queue.py: building the queue, the page, and applying corrections (no API, no downloads)."""

import copy
import json

import pytest

import evaluate
import review
import review_queue
from fields import FIELD_NAMES

RULE = review.Rule("second run", second_run=True, checks=True)


@pytest.fixture
def records():
    """Two filings: one extracted perfectly, one with a misread employee count that a second run reads right."""
    keys = list(evaluate.gold().items())[:2]
    base = [
        {
            "object_id": object_id,
            "answer": dict(key["fields"]),
            "problems": [],
            "attempts": [{}],
            "usage": {"output_tokens": 900},
            "unsure": [],
            "scan": None,
            "model": "claude-sonnet-5",
            "prompt": "extract/v3",
            "prompt_sha256": "abc",
            "cost_usd": 0.02,
            "seconds": 10.0,
        }
        for object_id, key in keys
    ]
    seconds = {r["object_id"]: copy.deepcopy(r) for r in base}
    base[1]["answer"]["employees"] = (base[1]["answer"]["employees"] or 0) + 7
    return base, seconds


def test_only_flagged_filings_and_fields_are_queued(records):
    base, seconds = records
    items = review_queue.queue(base, RULE, seconds)
    assert [item["object_id"] for item in items] == [base[1]["object_id"]]
    [field] = items[0]["fields"]
    assert field["name"] == "employees" and field["disagrees"]
    assert (
        field["value"] == base[1]["answer"]["employees"]
        and field["second"] == seconds[base[1]["object_id"]]["answer"]["employees"]
    )
    assert "Part I line 5" in field["label"]


def test_a_filing_that_failed_its_checks_is_queued_whole_with_the_reason(records):
    base, seconds = records
    base[0]["problems"] = ["Line 12 = lines 8-11: ..."]
    [item] = [i for i in review_queue.queue(base, RULE, seconds) if i["object_id"] == base[0]["object_id"]]
    assert item["whole"] and len(item["fields"]) == len(FIELD_NAMES) and "arithmetic" in item["reason"]


def test_the_page_lists_each_field_and_escapes_values(records, monkeypatch):
    base, seconds = records
    monkeypatch.setattr(review_queue, "_page_jpeg", lambda object_id, scan: "AAAA")
    base[1]["answer"]["mission"] = "<b>HELP</b> & SUPPORT"
    seconds[base[1]["object_id"]]["answer"]["mission"] = "HELP AND SUPPORT"
    page = review_queue.render("run-1", RULE, review_queue.queue(base, RULE, seconds), {})
    assert page.count('<tr data-field="') == 2  # employees and mission
    assert "&lt;b&gt;HELP&lt;/b&gt; &amp; SUPPORT" in page and "<b>HELP</b>" not in page
    assert 'data-kind="int"' in page and "Download corrections" in page


def test_a_blank_value_starts_with_blank_ticked(records, monkeypatch):
    base, seconds = records
    monkeypatch.setattr(review_queue, "_page_jpeg", lambda object_id, scan: "AAAA")
    base[1]["answer"]["volunteers"] = None
    seconds[base[1]["object_id"]]["answer"]["volunteers"] = 0
    page = review_queue.render("run-1", RULE, review_queue.queue(base, RULE, seconds), {})
    row = page[page.index('data-field="volunteers"') :].split("</tr>")[0]
    assert "disabled" in row and "checked" in row and "second run: 0" in row


def test_applying_a_perfect_reviewers_corrections_fixes_every_flagged_error(records, tmp_path):
    base, seconds = records
    run = tmp_path / "20261007-090000_claude-sonnet-5_v3"
    run.mkdir()
    for record in base:
        (run / f"{record['object_id']}.json").write_text(json.dumps(record), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"run": run.name, "complete": True}), encoding="utf-8")
    items = review_queue.queue(base, RULE, seconds)
    corrections = review_queue.simulate(run.name, items, RULE)

    reviewed = review_queue.apply(corrections, runs_dir=tmp_path)
    assert reviewed.name == run.name + "_reviewed"
    assert evaluate.score_run(run)["field_accuracy"] < 1.0
    assert evaluate.score_run(reviewed)["field_accuracy"] == 1.0
    record = json.loads((reviewed / f"{base[1]['object_id']}.json").read_text(encoding="utf-8"))
    assert record["reviewed_fields"] == ["employees"] and record["review_rule"] == RULE.name


def test_short_reviews_come_before_whole_documents(records):
    base, seconds = records
    base[0]["problems"] = ["Line 12 = lines 8-11: ..."]  # the first filing becomes a whole-document review
    items = review_queue.queue(base, RULE, seconds)
    assert [item["whole"] for item in items] == [False, True]
    assert items[0]["object_id"] == base[1]["object_id"]


def test_the_page_has_contents_progress_and_a_check_button_per_field(records, monkeypatch):
    base, seconds = records
    monkeypatch.setattr(review_queue, "_page_jpeg", lambda object_id, scan: "AAAA")
    page = review_queue.render("run-1", RULE, review_queue.queue(base, RULE, seconds), {})
    oid = base[1]["object_id"]
    assert f'<a href="#f-{oid}">' in page and f'<section id="f-{oid}"' in page
    assert "0 of 1 fields checked" in page
    assert page.count('class="check"') == 1 and "Looks right" in page
    assert "unreviewed" in page  # the download lists fields nobody checked


def test_unreviewed_fields_keep_the_extracted_value_and_are_recorded(records, tmp_path):
    base, seconds = records
    base[1]["answer"]["volunteers"] = 4321  # a second error, flagged too
    seconds[base[1]["object_id"]]["answer"]["volunteers"] = None
    run = tmp_path / "20261007-090000_claude-sonnet-5_v3"
    run.mkdir()
    for record in base:
        (run / f"{record['object_id']}.json").write_text(json.dumps(record), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"run": run.name, "complete": True}), encoding="utf-8")
    oid = base[1]["object_id"]
    key = evaluate.gold()[oid]["fields"]
    corrections = {
        "run": run.name,
        "rule": RULE.name,
        "corrections": {oid: {"employees": key["employees"]}},  # reviewed and fixed
        "unreviewed": {oid: ["volunteers"]},  # nobody looked
    }
    reviewed = review_queue.apply(corrections, runs_dir=tmp_path)
    record = json.loads((reviewed / f"{oid}.json").read_text(encoding="utf-8"))
    assert record["answer"]["employees"] == key["employees"] and record["answer"]["volunteers"] == 4321
    assert record["reviewed_fields"] == ["employees"] and record["unreviewed_fields"] == ["volunteers"]
