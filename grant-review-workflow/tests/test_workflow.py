"""The graph's orchestration, with stand-in steps: parallel branches, merged lists, routing, and every dev packet
ending exactly as labeled when each step returns the right answer. The steps themselves were tested in step 2."""

import datetime as dt
import json
import time

import pytest

import extract
import paths
import read_proposal
import workflow
from bedrock import CallRecord
from intake import gold_packet, intake
from state import ApplicationState, Claims, PeriodFigures, ProposalReading, Research, ReturnFigures

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
        folder = paths.GOLD_DIR / state.application_id
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


@pytest.mark.parametrize("packet_id", DEV)
def test_with_right_answers_each_dev_packet_ends_as_labeled(packet_id, tmp_path):
    label = json.loads((paths.GOLD_DIR / packet_id / "label.json").read_text(encoding="utf-8"))
    final = workflow.run(workflow.build(right_answers()), application(packet_id, tmp_path))
    assert final.route == label["route"]
    assert final.eligibility.failed_rules == label["failed_rules"]
    assert sorted(f.kind for f in final.flags) == label["flags"]
    # Only a decline by the rules gets a letter now; a review waits for a person (step 4).
    assert (final.letter is not None) == (label["route"] == "decline_ineligible")


def test_the_three_reading_steps_run_in_parallel(tmp_path):
    app = workflow.build(right_answers(delay=0.4))
    started = time.perf_counter()
    workflow.run(app, application("gp-027", tmp_path))
    assert time.perf_counter() - started < 0.8  # one after another would take 1.2 s


def test_parallel_additions_to_shared_lists_are_merged(tmp_path):
    """read_proposal and cross_check both add flags, and three steps add call records: all must survive."""
    final = workflow.run(workflow.build(right_answers()), application("gp-039", tmp_path))  # injected, reviewed
    assert sorted(c.step for c in final.calls) == ["extract", "read_proposal"]
    assert [f.kind for f in final.flags] == ["injection"]
    declined = workflow.run(workflow.build(right_answers()), application("gp-023", tmp_path))  # injected, declined
    assert sorted(c.step for c in declined.calls) == ["draft_letter", "extract", "read_proposal"]
    assert declined.cost_usd == pytest.approx(0.003)


def test_an_unverifiable_reading_becomes_a_flag(monkeypatch, tmp_path):
    reading = ProposalReading(focus_area="health", claims=Claims(), rubric=[])
    monkeypatch.setattr(
        read_proposal,
        "read_proposal",
        lambda client, narrative, model: (reading, ["quote not found"], call("read_proposal")),
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
        ("route", "__end__"),
    } <= edges


def test_the_readme_diagram_is_the_compiled_graph():
    readme = (paths.ROOT / "README.md").read_text(encoding="utf-8")
    drawn = readme.split("<!-- graph:start -->\n```mermaid\n")[1].split("\n```\n<!-- graph:end -->")[0]
    assert drawn.strip() == workflow.diagram(), (
        'redraw it: uv run python -c "import workflow; print(workflow.diagram())"'
    )
