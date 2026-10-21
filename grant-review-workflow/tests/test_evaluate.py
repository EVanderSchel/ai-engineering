"""The eval harness, without AWS: perfect stand-in steps must score perfectly; deliberate mistakes must land in the
right metric; the fingerprint must change with a prompt or a model; the test split must run once only."""

import copy

import pytest
from test_workflow import right_answers

import evaluate
import extract
import workflow


@pytest.fixture
def placeholder_pdfs(tmp_path, monkeypatch):
    """The return PDFs are gitignored (absent in CI); stand-in steps never read them, but intake checks one exists."""
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    real = evaluate.gold_packet
    monkeypatch.setattr(evaluate, "gold_packet", lambda packet_id: (*real(packet_id)[:2], pdf))


@pytest.fixture
def dev_result(placeholder_pdfs):
    app = workflow.build(right_answers(), checkpointer=workflow.memory_checkpointer())
    return evaluate.evaluate(app, "dev", reps=2)


def test_perfect_steps_score_perfectly(dev_result):
    p = dev_result["pooled"]
    assert p["applications"] == 30 and len(dev_result["per_rep"]) == 2
    assert p["wrong_automatic_declines"] == 0 and p["declines_sent_to_review"] == 0
    for metric in ("route_accuracy", "failed_rules_accuracy", "flags_exact", "extraction_field_accuracy"):
        assert p[metric] == 1.0, metric
    assert p["focus_accuracy"] == 1.0 and p["claims_accuracy"] == 1.0
    assert p["injections_caught"] == p["injected_outcome_intact"] == p["injections"] == 6  # 3 dev injections x 2
    assert p["injection_false_alarms"] == 0 and p["near_misses_flagged"] == 0
    assert all(v["recall"] == 1.0 and v["precision"] == 1.0 for v in p["flags"].values())


def test_a_report_is_written_from_a_result(dev_result):
    text = evaluate.report(dev_result)
    assert "Wrong automatic declines | **0**" in text and "Flags by kind" in text


def mistake(rows, packet_id, **changes):
    rows = copy.deepcopy(rows)
    for row in rows:
        if row["packet_id"] == packet_id and row["rep"] == 1:
            row.update(changes)
    return rows


def test_a_wrong_decline_is_counted_as_one(dev_result):
    # gp-027 is clean and eligible: declining it automatically is the costliest error.
    rows = mistake(dev_result["rows"], "gp-027", route="decline_ineligible", failed_rules=["revenue_range"])
    p = evaluate.score(rows)
    assert p["wrong_automatic_declines"] == 1 and p["route_accuracy"] < 1.0


def test_missed_and_false_flags_land_in_their_kind(dev_result):
    rows = mistake(dev_result["rows"], "gp-009", flags=[])  # planted budget mismatch, missed
    rows = mistake(rows, "gp-033", flags=["budget_mismatch"])  # a near miss, wrongly flagged
    p = evaluate.score(rows)
    assert p["flags"]["budget_mismatch"]["fn"] == 1 and p["flags"]["budget_mismatch"]["fp"] == 1
    assert p["near_misses_flagged"] == 1


def test_an_injection_that_changes_the_outcome_is_not_intact(dev_result):
    # gp-039's injection asks for approval; here it 'worked': the injection flag is caught but a flag went missing.
    rows = mistake(dev_result["rows"], "gp-026", flags=["injection"])  # staff_mismatch suppressed by its injection
    p = evaluate.score(rows)
    assert p["injections_caught"] == 6 and p["injected_outcome_intact"] == 5


def test_misread_fields_lower_extraction_accuracy(dev_result):
    rows = mistake(dev_result["rows"], "gp-036", wrong_fields=["total_revenue_current_year: read 51306, key 51308"])
    expected = 1 - 1 / (30 * len(extract.COMPARED))
    assert evaluate.score(rows)["extraction_field_accuracy"] == pytest.approx(expected, abs=1e-4)


def test_the_fingerprint_changes_with_a_prompt_or_a_model(monkeypatch):
    before = evaluate.fingerprint()["digest"]
    monkeypatch.setattr(extract, "SYSTEM", extract.SYSTEM + " ")
    assert evaluate.fingerprint()["digest"] != before
    monkeypatch.undo()
    monkeypatch.setitem(workflow.STEP_MODELS, "extract", "haiku")
    assert evaluate.fingerprint()["digest"] != before


@pytest.mark.parametrize("split", ["test", "holdout"])
def test_a_held_out_split_needs_final(monkeypatch, split):
    monkeypatch.setattr("sys.argv", ["evaluate.py", "--split", split])
    with pytest.raises(SystemExit):
        evaluate.main()


@pytest.mark.parametrize("split", ["test", "holdout"])
def test_a_held_out_split_runs_only_once(monkeypatch, tmp_path, split):
    monkeypatch.setattr(evaluate, "RESULTS", tmp_path)
    (tmp_path / f"eval-{split}.json").write_text("{}")
    monkeypatch.setattr("sys.argv", ["evaluate.py", "--split", split, "--final"])
    with pytest.raises(SystemExit):
        evaluate.main()


def test_rubric_consistency_handles_repeats():
    rows = [
        {"packet_id": "gp-002", "rubric": {"clear_outcomes": 4, "budget_matches_request": 5, "feasible_for_size": 3}},
        {"packet_id": "gp-002", "rubric": {"clear_outcomes": 4, "budget_matches_request": 5, "feasible_for_size": 4}},
    ]
    result = evaluate.rubric_consistency(rows)
    assert result["clear_outcomes"]["same_score_every_repeat"] == 1.0
    assert result["feasible_for_size"]["max_change_between_repeats"] == 1


# --- Surviving transient AWS errors -------------------------------------------------------------------------


def flaky(failures: list[str]):
    """Stand-in steps whose read_proposal raises the given AWS error codes, one per call, before working.

    The outage hits only after extract and research have finished, as it would with real calls taking seconds:
    otherwise, on a slow runner, LangGraph cancels a sibling still running when read_proposal fails, and that
    sibling rightly runs again (it never finished), which isn't what this checks."""
    import threading

    from botocore.exceptions import ClientError

    steps = right_answers()
    calls = []
    errors = iter(failures)
    finished = {"extract": threading.Event(), "research": threading.Event()}

    def read_proposal(state):
        calls.append("read_proposal")
        code = next(errors, None)
        if code:
            assert all(done.wait(timeout=10) for done in finished.values()), "siblings never finished"
            raise ClientError({"Error": {"Code": code, "Message": "x"}}, "Converse")
        return steps.read_proposal(state)

    def counted(name, node):
        def run(state):
            calls.append(name)
            result = node(state)
            finished[name].set()
            return result

        return run

    flaky_steps = workflow.Steps(
        counted("extract", steps.extract), counted("research", steps.research), read_proposal, steps.draft_letter
    )
    return workflow.build(flaky_steps, checkpointer=workflow.memory_checkpointer()), calls


def test_an_outage_that_clears_is_survived_without_paying_twice(placeholder_pdfs):
    graph, calls = flaky(["ServiceUnavailableException", "ThrottlingException"])
    waits = []
    row = evaluate.run_case(graph, "gp-039", 1, "t", sleep=waits.append)
    assert row["transient_errors_survived"] == ["ServiceUnavailableException", "ThrottlingException"]
    assert waits == [30, 90]
    assert calls.count("extract") == 1 and calls.count("research") == 1  # not rerun after the outages
    assert row["route"] == "review" and row["flags"] == ["injection"]
    assert row["return_name_read"]  # the stand-in extract reads the answer key's name
    assert row["public_record_name_matches"] is True


def test_an_outage_that_doesnt_clear_becomes_a_recorded_failure(placeholder_pdfs):
    graph, _ = flaky(["ServiceUnavailableException"] * 3)
    with pytest.raises(evaluate.CaseFailed):
        evaluate.run_case(graph, "gp-039", 1, "t", sleep=lambda seconds: None)


def test_an_error_waiting_wont_fix_stops_at_once(placeholder_pdfs):
    from botocore.exceptions import ClientError

    graph, _ = flaky(["AccessDeniedException"])
    with pytest.raises(ClientError):
        evaluate.run_case(graph, "gp-039", 1, "t", sleep=lambda seconds: pytest.fail("shouldn't wait"))
