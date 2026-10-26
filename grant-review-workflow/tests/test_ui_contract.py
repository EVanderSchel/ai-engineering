"""The reviewer UI's copy of the API (step 8): its JSON Schema and its fixtures must be what the API produces now.

The fixtures are the dev packets run through the stand-in steps (no model calls) and read back through api.handler,
so the UI is built against real responses. Regenerate both after changing the API:

    $env:UPDATE_UI_CONTRACT = "1"; uv run pytest tests/test_ui_contract.py; Remove-Item Env:UPDATE_UI_CONTRACT
    cd ui; npm run types
"""

import datetime as dt
import json
import os
import re

import pytest
from test_workflow import DEV, application, decision, right_answers

import api
import applications
import paths
import workflow
from state import RubricScore

UI = paths.ROOT / "ui" / "src"
UPDATE = os.environ.get("UPDATE_UI_CONTRACT") == "1"
NOW = dt.datetime(2025, 7, 1, 12, tzinfo=dt.UTC)
DECIDED = {"gp-003": "approve", "gp-028": "decline"}  # two reviewed applications, so the UI has finished ones too


def with_rubric(steps: workflow.Steps) -> workflow.Steps:
    """right_answers leaves the rubric empty; the UI needs scores with quotes, so quote each narrative's first
    sentence about the project (the applicant's own words, as the real step must)."""

    def read_proposal(state):
        result = steps.read_proposal(state)
        project = state.narrative.split("## The project", 1)[-1].strip()
        quote = re.split(r"(?<=\.)\s", project, maxsplit=1)[0]
        scores = {"clear_outcomes": 4, "budget_matches_request": 3, "feasible_for_size": 4}
        rubric = [RubricScore(criterion=c, score=s, quote=quote) for c, s in scores.items()]
        return {**result, "proposal": result["proposal"].model_copy(update={"rubric": rubric})}

    return workflow.Steps(steps.extract, steps.research, read_proposal, steps.draft_letter)


def seed(graph, store, folder) -> None:
    """The dev packets run into a graph and status store: 7 waiting, 2 decided, 6 declined by the rules. The UI's
    fixtures are made this way, and tests/local_api.py starts this way."""
    for hour, packet in enumerate(DEV):
        received = dt.datetime(2025, 6, 2, 8, tzinfo=dt.UTC) + dt.timedelta(hours=hour)
        outcome = workflow.start(graph, application(packet, folder).model_copy(update={"received_at": received}))
        if packet in DECIDED:
            outcome = workflow.resume(graph, packet, decision(DECIDED[packet]))
        store.put(applications.record(outcome, NOW))


def generate(tmp_path, monkeypatch) -> dict[str, str]:
    """Every file the UI keeps, by path relative to ui/src, from the current API."""
    checkpointer = workflow.memory_checkpointer()
    graph = workflow.build(with_rubric(right_answers()), checkpointer=checkpointer)
    store = applications.MemoryStore()
    monkeypatch.setattr(api, "_workflow", lambda: workflow.reader(checkpointer))
    monkeypatch.setattr(api, "_applications", lambda: store)
    seed(graph, store, tmp_path)

    def body(route, **event):
        signed_in = {
            "requestContext": {"authorizer": {"jwt": {"claims": {"username": "r", "cognito:groups": "[reviewers]"}}}}
        }
        response = api.handler({"routeKey": route, **event, **signed_in})
        assert response["statusCode"] == 200, response
        return json.dumps(json.loads(response["body"]), indent=2, ensure_ascii=False) + "\n"

    files = {"api/schema.json": json.dumps(api.contract(), indent=2) + "\n"}
    for status in ("waiting_for_review", "decided", "declined_by_rules"):
        files[f"fixtures/queue/{status}.json"] = body("GET /applications", queryStringParameters={"status": status})
    for packet in DEV:
        files[f"fixtures/applications/{packet}.json"] = body(
            "GET /applications/{application_id}", pathParameters={"application_id": packet}
        )
    return files


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        return generate(tmp_path_factory.mktemp("ui"), monkeypatch)


def test_the_ui_has_the_current_schema_and_fixtures(generated):
    if UPDATE:
        for relative, text in generated.items():
            (UI / relative).parent.mkdir(parents=True, exist_ok=True)
            (UI / relative).write_text(text, encoding="utf-8", newline="\n")
    stale = [r for r, text in generated.items() if not (UI / r).exists() or (UI / r).read_text("utf-8") != text]
    assert not stale, f"regenerate (see this file's docstring): {stale}"
    kept = {p.relative_to(UI).as_posix() for p in (UI / "fixtures").rglob("*.json")}
    assert kept == {r for r in generated if r.startswith("fixtures/")}, "remove fixtures the API no longer produces"


def test_the_fixtures_cover_every_status_and_the_risky_cases(generated):
    queues = {r: json.loads(t)["applications"] for r, t in generated.items() if r.startswith("fixtures/queue/")}
    assert all(queues.values())  # none empty
    waiting = {a["application_id"]: a for a in queues["fixtures/queue/waiting_for_review.json"]}
    assert "injection" in waiting["gp-039"]["flags"]  # text addressed to an AI, which the UI must show inertly
    assert {a["decision"] for a in queues["fixtures/queue/decided.json"]} == {"approve", "decline"}
