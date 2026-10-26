"""The Step Functions version's steps (step 9), without AWS: each dev packet walked through them in the state
machine's order with the stand-in steps (tests/test_workflow.right_answers) must end exactly where the LangGraph graph
ends with the same stand-ins. The state machine's own definition is tested in infra/test/step-functions-stack.test.ts.
"""

import json

import pytest
from test_handlers import FakeS3, upload
from test_workflow import DEV, application, decision, right_answers

import sfn_steps
import workflow
from state import ApplicationState

STEPS = right_answers()


def roundtrip(state: ApplicationState) -> ApplicationState:
    """What crossing a state boundary does to the state: Step Functions passes JSON."""
    return ApplicationState.model_validate(json.loads(json.dumps(state.model_dump(mode="json"))))


def state_machine(state: ApplicationState, decisions: list) -> tuple[ApplicationState, int]:
    """The state machine's path, step by step (as infra/lib/step-functions-stack.ts defines it). Returns the final
    state and how many times it waited for a reviewer; `decisions` answers each wait in turn."""
    run = lambda step, s, **extra: roundtrip(sfn_steps.run(STEPS, step, s, **extra))  # noqa: E731
    branches = {step: run(step, state) for step in sfn_steps.BRANCHES}  # Parallel
    state = run("join", state, branches=branches)
    state = run("cross_check", state)
    waits = 0
    while True:
        state = run("eligibility", state)
        state = run("route", state)
        if state.route == "decline_ineligible":  # Choice
            break
        review = decisions[waits]  # the task token wait, answered
        waits += 1
        state = run("apply_review", state, decision=review.model_dump(mode="json"))
        if state.review.decision is not None:  # Choice: decided -> the letter; corrections -> the rules again
            break
    return run("draft_letter", state), waits


def graph(state: ApplicationState, decisions: list) -> tuple[ApplicationState, int]:
    app = workflow.build(STEPS, checkpointer=workflow.memory_checkpointer())
    outcome = workflow.start(app, state)
    waits = 0
    while outcome.waiting_for_review:
        outcome = workflow.resume(app, state.application_id, decisions[waits])
        waits += 1
    return outcome.state, waits


def same(a: ApplicationState, b: ApplicationState) -> None:
    """Everything a run decides or records; `calls` compared by step (stand-in calls are otherwise identical)."""
    for field in ("extraction", "research", "proposal", "eligibility", "route", "review", "letter", "letter_problems"):
        assert getattr(a, field) == getattr(b, field), field
    assert sorted(f.model_dump_json() for f in a.flags) == sorted(f.model_dump_json() for f in b.flags)
    assert sorted(c.step for c in a.calls) == sorted(c.step for c in b.calls)


@pytest.mark.parametrize("packet_id", DEV)
def test_each_dev_packet_ends_where_the_graph_ends(packet_id, tmp_path):
    approve = [decision("approve")]
    sfn, sfn_waits = state_machine(application(packet_id, tmp_path), approve)
    lg, lg_waits = graph(application(packet_id, tmp_path), approve)
    same(sfn, lg)
    assert sfn_waits == lg_waits  # paused for a reviewer exactly when the graph did


def test_corrections_go_back_through_the_rules_and_pause_again_as_in_the_graph(tmp_path):
    rounds = [
        decision(corrections={"employees": 12}),
        decision("decline", corrections={"total_revenue_current_year": 99}),
    ]
    sfn, sfn_waits = state_machine(application("gp-027", tmp_path), rounds)
    lg, lg_waits = graph(application("gp-027", tmp_path), rounds)
    same(sfn, lg)
    assert sfn_waits == lg_waits == 2
    assert sfn.review.corrections == {"employees": 12, "total_revenue_current_year": 99}  # earlier ones kept


def test_corrections_that_fail_a_rule_end_in_a_decline_by_the_rules(tmp_path):
    rounds = [decision(corrections={"total_revenue_current_year": 10})]
    sfn, _ = state_machine(application("gp-027", tmp_path), rounds)
    lg, _ = graph(application("gp-027", tmp_path), rounds)
    same(sfn, lg)
    assert sfn.route == "decline_ineligible"


def test_join_keeps_each_branchs_field_and_appends_their_additions_once(tmp_path):
    state = application("gp-039", tmp_path)  # read_proposal's stand-in finds an injection
    branches = {step: sfn_steps.run(STEPS, step, state) for step in sfn_steps.BRANCHES}
    joined = sfn_steps.join(state, branches)
    assert joined.extraction == branches["extract"].extraction
    assert joined.research == branches["research"].research
    assert joined.proposal == branches["read_proposal"].proposal
    assert [c.step for c in joined.calls] == ["extract", "read_proposal"]  # research's stand-in makes none
    with_one_flag = state.model_copy(update={"flags": branches["read_proposal"].flags or []})
    rejoined = sfn_steps.join(
        with_one_flag, {s: b.model_copy(update={"flags": with_one_flag.flags}) for s, b in branches.items()}
    )
    assert rejoined.flags == with_one_flag.flags  # a flag already in the state isn't added again by every branch


def test_an_invalid_decision_never_reaches_the_state(tmp_path):
    state = application("gp-027", tmp_path)
    with pytest.raises(ValueError):
        sfn_steps.run(STEPS, "apply_review", state, decision={"decision": "maybe", "reason": "", "reviewer": "x"})


def test_an_unknown_step_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no step 'delete_everything'"):
        sfn_steps.run(STEPS, "delete_everything", application("gp-027", tmp_path))


@pytest.fixture
def lambda_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sfn_steps, "_steps", lambda: STEPS)
    monkeypatch.setattr(sfn_steps.handlers, "_s3", lambda: FakeS3(tmp_path / "s3"))
    monkeypatch.setattr(sfn_steps.handlers, "WORK_DIR", tmp_path / "work")
    return tmp_path / "s3" / "intake"


def test_the_lambda_handler_runs_intake_then_steps_on_json(lambda_env, caplog):
    upload(lambda_env, "gp-027-sfn1")
    state = sfn_steps.handler({"step": "intake", "application_id": "gp-027-sfn1", "bucket": "intake"})
    assert state["application_id"] == "gp-027-sfn1" and state["narrative"]
    with caplog.at_level("INFO", logger="grant_review"):
        after = sfn_steps.handler({"step": "read_proposal", "state": state, "bucket": "intake"})
    assert after["proposal"] is not None
    [line] = caplog.records
    assert (line.step, line.application_id, line.model_calls) == ("read_proposal", "gp-027-sfn1", 1)
    assert state["narrative"][:40] not in caplog.text  # no applicant text in the logs


def test_extract_downloads_the_return_again_in_a_fresh_container(lambda_env, tmp_path):
    upload(lambda_env, "gp-027-sfn2")
    state = sfn_steps.handler({"step": "intake", "application_id": "gp-027-sfn2", "bucket": "intake"})
    pdf = tmp_path / "work" / "gp-027-sfn2" / "return.pdf"
    pdf.unlink()  # another container: /tmp doesn't have it
    sfn_steps.handler({"step": "extract", "state": state, "bucket": "intake"})
    assert pdf.exists()


def test_an_unsafe_application_id_is_refused_at_intake(lambda_env):
    with pytest.raises(ValueError):
        sfn_steps.handler({"step": "intake", "application_id": "../x", "bucket": "intake"})
