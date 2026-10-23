"""The graph's orchestration, with stand-in steps: parallel branches, merged lists, routing, and every dev packet
ending exactly as labeled when each step returns the right answer. The steps themselves were tested in step 2."""

import datetime as dt
import json
import time

import pytest
from pydantic import ValidationError

import extract
import paths
import read_proposal
import workflow
from bedrock import CallRecord
from intake import gold_packet, intake
from state import ApplicationState, Claims, PeriodFigures, ProposalReading, Research, ReturnFigures, ReviewerDecision

DEV = sorted(
    p.name
    for p in paths.GOLD_DIR.glob("gp-*")
    if json.loads((p / "label.json").read_text(encoding="utf-8"))["split"] == "dev"
)


def call(step: str) -> CallRecord:
    return CallRecord(
        step=step,
        model="stand-in",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.001,
        latency_ms=1,
        wall_ms=1,
        stop_reason="tool_use",
    )


def right_answers(delay: float = 0.0) -> workflow.Steps:
    """Stand-in steps that return what a perfect model would, from the packet's spec and answer key."""

    def packet(state: ApplicationState):
        folder = paths.GOLD_DIR / state.application_id[:6]  # "gp-039", also from eval IDs like gp-039-<run>-r2
        spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
        meta = json.loads((folder / "narrative.json").read_text(encoding="utf-8"))
        key = json.loads((paths.FILINGS_DIR / f"{spec['object_id']}.json").read_text(encoding="utf-8"))["fields"]
        figures = ReturnFigures.model_validate({k: v for k, v in key.items() if k in ReturnFigures.model_fields})
        return spec, meta, key, figures

    def extract_node(state):
        time.sleep(delay)
        *_, figures = packet(state)
        return {
            "extraction": extract.Extraction(
                form_type="990", figures=figures, revenue_adds_up=extract.revenue_adds_up(figures)
            ),
            "calls": [call("extract")],
        }

    def research_node(state):
        time.sleep(delay)
        spec, _, key, figures = packet(state)
        period = figures.tax_period_end.strftime("%Y-%m")
        record = PeriodFigures(revenue=figures.total_revenue_current_year, expenses=figures.total_expenses_current_year)
        result = Research(
            found=True,
            registered_name=key["organization_name"],
            name_matches_form=spec["plant"] != "name_mismatch",
            figures_by_period={period: record},
        )
        return {"research": result, "calls": []}

    def read_proposal_node(state):
        time.sleep(delay)
        spec, meta, *_ = packet(state)
        injected = [meta["injection_text"]] if meta["injection_text"] else []
        reading = ProposalReading(
            focus_area=spec["focus_area"], claims=Claims(**spec["claims"]), rubric=[], suspected_instructions=injected
        )
        return {"proposal": reading, "flags": [], "calls": [call("read_proposal")]}

    def letter_node(state):
        return {"letter": f"Letter for {state.application_id}", "letter_problems": [], "calls": [call("draft_letter")]}

    return workflow.Steps(extract_node, research_node, read_proposal_node, letter_node)


def application(packet_id: str, tmp_path) -> ApplicationState:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    form, narrative, _ = gold_packet(packet_id)
    return intake(form, narrative, pdf, application_id=packet_id, received_at=dt.datetime(2025, 6, 1, tzinfo=dt.UTC))


def app(steps=None):
    return workflow.build(steps or right_answers(), checkpointer=workflow.memory_checkpointer())


def run_until_pause(application: ApplicationState, steps=None) -> ApplicationState:
    return workflow.start(app(steps), application).state


@pytest.mark.parametrize("packet_id", DEV)
def test_with_right_answers_each_dev_packet_ends_as_labeled(packet_id, tmp_path):
    label = json.loads((paths.GOLD_DIR / packet_id / "label.json").read_text(encoding="utf-8"))
    final = run_until_pause(application(packet_id, tmp_path))
    assert final.route == label["route"]
    assert final.eligibility.failed_rules == label["failed_rules"]
    assert sorted(f.kind for f in final.flags) == label["flags"]
    # A decline by the rules gets its letter; a review pauses for a person, with no letter yet.
    assert (final.letter is not None) == (label["route"] == "decline_ineligible")


def test_the_three_reading_steps_run_in_parallel(tmp_path):
    slow = app(right_answers(delay=0.4))
    started = time.perf_counter()
    workflow.start(slow, application("gp-027", tmp_path))
    assert time.perf_counter() - started < 0.8  # one after another would take 1.2 s


def test_parallel_additions_to_shared_lists_are_merged(tmp_path):
    """read_proposal and cross_check both add flags, and three steps add call records: all must survive."""
    final = run_until_pause(application("gp-039", tmp_path))  # injected, reviewed
    assert sorted(c.step for c in final.calls) == ["extract", "read_proposal"]
    assert [f.kind for f in final.flags] == ["injection"]
    declined = run_until_pause(application("gp-023", tmp_path))  # injected, declined
    assert sorted(c.step for c in declined.calls) == ["draft_letter", "extract", "read_proposal"]
    assert declined.cost_usd == pytest.approx(0.003)


def test_an_unverifiable_reading_becomes_a_flag(monkeypatch, tmp_path):
    reading = ProposalReading(focus_area="health", claims=Claims(), rubric=[])
    monkeypatch.setattr(
        read_proposal,
        "read_proposal",
        lambda client, narrative, name, title, model: (reading, ["quote not found"], call("read_proposal")),
    )
    update = workflow.bedrock_steps(client=None).read_proposal(application("gp-027", tmp_path))
    assert [f.kind for f in update["flags"]] == ["unverified_reading"] and update["flags"][
        0
    ].evidence == "quote not found"


def test_the_graph_has_the_designed_shape():
    graph = workflow.build(right_answers()).get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    for step in ("extract", "research", "read_proposal"):
        assert ("__start__", step) in edges and (step, "cross_check") in edges
    assert {
        ("cross_check", "eligibility"),
        ("eligibility", "route"),
        ("route", "draft_letter"),
        ("route", "human_review"),
        ("human_review", "draft_letter"),
        ("human_review", "eligibility"),
    } <= edges


def test_the_readme_diagram_is_the_compiled_graph():
    readme = (paths.ROOT / "README.md").read_text(encoding="utf-8")
    drawn = readme.split("<!-- graph:start -->\n```mermaid\n")[1].split("\n```\n<!-- graph:end -->")[0]
    assert drawn.strip() == workflow.diagram(), (
        'redraw it: uv run python -c "import workflow; print(workflow.diagram())"'
    )


# --- Pausing for the reviewer (step 4a) --------------------------------------------------------------------

NOW = dt.datetime(2025, 7, 1, tzinfo=dt.UTC)


def decision(kind=None, corrections=None, reason="Checked the return."):
    return ReviewerDecision(decision=kind, corrections=corrections or {}, reason=reason, reviewer="me", decided_at=NOW)


def test_a_reviewed_application_pauses_with_what_the_reviewer_needs(tmp_path):
    outcome = workflow.start(app(), application("gp-039", tmp_path))  # eligible, injection flagged
    assert outcome.waiting_for_review and outcome.state.letter is None and outcome.state.review is None
    request = outcome.review_request
    assert request["application_id"] == "gp-039" and request["failed_rules"] == []
    assert [f["kind"] for f in request["flags"]] == ["injection"]
    assert request["figures"]["total_revenue_current_year"] == 441406


def test_a_decline_by_the_rules_never_pauses(tmp_path):
    outcome = workflow.start(app(), application("gp-023", tmp_path))
    assert not outcome.waiting_for_review and outcome.state.letter is not None and outcome.state.review is None


def test_resuming_with_a_decision_finishes_with_a_letter(tmp_path):
    graph = app()
    workflow.start(graph, application("gp-039", tmp_path))
    done = workflow.resume(graph, "gp-039", decision("approve", reason="Strong plan."))
    assert not done.waiting_for_review
    assert done.state.review.decision == "approve" and done.state.letter == "Letter for gp-039"


def test_corrections_rerun_the_rules_and_can_decline(tmp_path):
    """A reviewer finds revenue was misread: the corrected figure fails the revenue rule, so the rules decline,
    which they may, since the reviewer has now checked the figures."""
    graph = app()
    workflow.start(graph, application("gp-039", tmp_path))
    done = workflow.resume(graph, "gp-039", decision(corrections={"total_revenue_current_year": 41_406}))
    assert not done.waiting_for_review
    assert done.state.eligibility.failed_rules == ["revenue_range"] and done.state.route == "decline_ineligible"
    assert done.state.letter is not None


def test_corrections_that_keep_it_eligible_pause_again_and_are_kept(tmp_path):
    graph = app()
    workflow.start(graph, application("gp-039", tmp_path))
    again = workflow.resume(graph, "gp-039", decision(corrections={"total_revenue_current_year": 450_000}))
    assert again.waiting_for_review and again.review_request["figures"]["total_revenue_current_year"] == 441406
    done = workflow.resume(graph, "gp-039", decision("approve", reason="Strong plan."))
    assert done.state.review.corrections == {"total_revenue_current_year": 450_000}  # kept from the first round
    assert done.state.review.decision == "approve" and done.state.letter is not None


def test_a_paused_application_survives_a_new_process(tmp_path):
    """The checkpointer, not the running program, holds the paused state: a fresh graph over the same saver
    (a restart, or another server) resumes it, and no reading step runs again."""
    saver = workflow.memory_checkpointer()
    calls = []

    def counting(steps):
        def wrap(name, node):
            def run(state):
                calls.append(name)
                return node(state)

            return run

        return workflow.Steps(
            *(wrap(n, getattr(steps, n)) for n in ("extract", "research", "read_proposal", "draft_letter"))
        )

    workflow.start(workflow.build(counting(right_answers()), checkpointer=saver), application("gp-039", tmp_path))
    restarted = workflow.build(counting(right_answers()), checkpointer=saver)
    done = workflow.resume(restarted, "gp-039", decision("approve", reason="Strong plan."))
    assert done.state.letter is not None
    assert sorted(calls) == ["draft_letter", "extract", "read_proposal", "research"]  # each exactly once


def test_an_invalid_decision_is_rejected_before_it_reaches_the_graph(tmp_path):
    """LangGraph saves a resume value before the node checks it, so a bad value sent to the graph would be
    replayed on every later resume and the thread would be stuck. resume() only takes a ReviewerDecision, which
    can't be built from invalid input: the graph never sees it, and the application stays cleanly paused."""
    graph = app()
    workflow.start(graph, application("gp-039", tmp_path))
    with pytest.raises(ValidationError):
        workflow.resume(graph, "gp-039", ReviewerDecision.model_validate({"decision": "maybe"}))
    still = workflow.outcome(graph, "gp-039")
    assert still.waiting_for_review and still.state.review is None
    assert workflow.resume(graph, "gp-039", decision("decline", reason="Out of focus.")).state.letter is not None


# --- Durability: a crash mid-run (step 4b) -------------------------------------------------------------------


def test_after_a_crash_finished_steps_are_not_run_again(tmp_path):
    """read_proposal fails (a crash, an outage) while extract and research succeed in the same parallel step.
    LangGraph saved their results as pending writes, so resuming the thread reruns only read_proposal: the model
    calls already paid for aren't paid for twice.

    The crash comes after extract and research have finished, as it would with real calls taking seconds. Raised
    at once, it can land while a sibling is still finishing on a slow runner: LangGraph's executor is then shutting
    down and that sibling's write is lost, so it rightly runs again (it flaked on CI, PR #90; the evaluate test's
    outage case has the same guard)."""
    import threading

    saver = workflow.memory_checkpointer()
    steps = right_answers()
    calls = []
    failures = iter([True])  # read_proposal fails once, then works
    finished = {"extract": threading.Event(), "research": threading.Event()}

    def counted(name, node):
        def run(state):
            calls.append(name)
            if name == "read_proposal" and next(failures, False):
                assert all(done.wait(timeout=10) for done in finished.values()), "siblings never finished"
                raise ConnectionError("Bedrock unreachable")
            result = node(state)
            if name in finished:
                finished[name].set()
            return result

        return run

    counting = workflow.Steps(
        *(counted(n, getattr(steps, n)) for n in ("extract", "research", "read_proposal", "draft_letter"))
    )
    graph = workflow.build(counting, checkpointer=saver)
    with pytest.raises(ConnectionError):
        workflow.start(graph, application("gp-023", tmp_path))  # declined by the rules: runs to its letter
    assert sorted(calls) == ["extract", "read_proposal", "research"]

    graph.invoke(None, workflow.thread("gp-023"))  # resume from the last checkpoint
    done = workflow.outcome(graph, "gp-023")
    assert done.state.letter is not None and done.state.route == "decline_ineligible"
    assert sorted(calls) == ["draft_letter", "extract", "read_proposal", "read_proposal", "research"]
    assert sorted(c.step for c in done.state.calls) == ["draft_letter", "extract", "read_proposal"]  # no duplicates


def test_checkpoints_may_only_be_rebuilt_into_our_state_classes():
    allowed = dict(workflow.checkpoint_classes())
    assert ("state", "ApplicationState") in workflow.checkpoint_classes() and allowed["bedrock"] == "CallRecord"
    for name in ("ReviewerDecision", "Flag", "PeriodFigures", "ProposalReading"):
        assert ("state", name) in workflow.checkpoint_classes()


def test_a_full_run_round_trips_through_the_checkpoints_without_warnings(tmp_path, recwarn):
    graph = app()
    workflow.start(graph, application("gp-039", tmp_path))
    workflow.resume(graph, "gp-039", decision("approve", reason="Strong plan."))
    assert not [w for w in recwarn if "unregistered type" in str(w.message)]


def test_stage_timings_come_from_the_checkpoint_history(tmp_path):
    graph = app(right_answers(delay=0.2))
    workflow.start(graph, application("gp-023", tmp_path))
    stages = workflow.stage_timings(graph, "gp-023")
    assert [s["stage"] for s in stages] == [
        "extract+research+read_proposal",
        "cross_check",
        "eligibility",
        "route",
        "draft_letter",
    ]
    assert 0.2 <= stages[0]["seconds"] < 0.4  # the three steps ran together


def test_the_dynamodb_checkpointer_uses_the_allowlist_and_the_ttl(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    saver = workflow.dynamodb_checkpointer()  # builds clients only: no AWS call
    assert saver.table_name == "grant-review-checkpoints" and saver.client.meta.region_name == "us-east-2"
    assert saver.repo.ttl_seconds == 30 * 24 * 3600
    blocked = workflow.serializer()._allowed_msgpack_modules
    assert ("state", "ApplicationState") in blocked and saver.serde._allowed_msgpack_modules == blocked


def test_an_application_id_cant_be_started_twice(tmp_path):
    graph = app()
    workflow.start(graph, application("gp-039", tmp_path))
    with pytest.raises(workflow.ApplicationExists):
        workflow.start(graph, application("gp-039", tmp_path))
