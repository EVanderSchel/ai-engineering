"""The reviewer UI's API, without AWS: applications run with stand-in steps (tests/test_workflow.right_answers) into an
in-memory checkpointer and status store, then read back through the handler as API Gateway would call it."""

import datetime as dt
import json

import pytest
from test_workflow import application, decision, right_answers

import api
import applications
import workflow

NOW = dt.datetime(2025, 7, 1, 12, tzinfo=dt.UTC)


@pytest.fixture
def run(monkeypatch):
    """Runs an application with stand-in steps and records it, as the Lambda handlers do; the API reads both."""
    checkpointer = workflow.memory_checkpointer()
    graph = workflow.build(right_answers(), checkpointer=checkpointer)
    store = applications.MemoryStore()
    monkeypatch.setattr(api, "_workflow", lambda: workflow.reader(checkpointer))
    monkeypatch.setattr(api, "_applications", lambda: store)

    def start(app_state):
        outcome = workflow.start(graph, app_state)
        store.put(applications.record(outcome, NOW))
        return outcome

    def resume(application_id, review):
        outcome = workflow.resume(graph, application_id, review)
        store.put(applications.record(outcome, NOW))
        return outcome

    return start, resume


def get(path, **query):
    """A request as an API Gateway HTTP API delivers it (payload format 2.0)."""
    route, params = path, None
    if path.startswith("/applications/"):
        route, params = "/applications/{application_id}", {"application_id": path.removeprefix("/applications/")}
    response = api.handler(
        {"routeKey": f"GET {route}", "pathParameters": params, "queryStringParameters": query or None}
    )
    assert response["headers"]["content-type"] == "application/json"
    return response["statusCode"], json.loads(response["body"])


def test_the_queue_lists_waiting_applications_oldest_first(run, tmp_path):
    start, _ = run
    late = application("gp-039", tmp_path).model_copy(update={"received_at": dt.datetime(2025, 6, 2, tzinfo=dt.UTC)})
    start(late)
    start(application("gp-027", tmp_path))  # received 2025-06-01
    start(application("gp-018", tmp_path))  # declined by the rules: not in the queue
    status, body = get("/applications")
    assert status == 200
    assert [a["application_id"] for a in body["applications"]] == ["gp-027", "gp-039"]
    assert body["applications"][1]["flags"] == ["injection"]
    _, declined = get("/applications", status="declined_by_rules")
    assert [a["application_id"] for a in declined["applications"]] == ["gp-018"]


def test_an_unknown_status_is_refused(run):
    assert get("/applications", status="everything") == (
        400,
        {"error": "status must be one of ['waiting_for_review', 'decided', 'declined_by_rules']"},
    )


def test_one_application_has_the_evidence_a_reviewer_needs(run, tmp_path):
    start, _ = run
    start(application("gp-039", tmp_path))
    status, body = get("/applications/gp-039")
    assert status == 200 and body["waiting_for_review"] and body["record"]["status"] == "waiting_for_review"
    assert [r["rule"] for r in body["rules"]] == list(api.fund_rules.RULES) and all(r["passed"] for r in body["rules"])
    assert body["form"]["organization_name"] and body["narrative"]
    assert body["extraction"]["figures"]["total_revenue_current_year"]
    assert body["research"]["found"] and body["proposal"]["suspected_instructions"]
    assert [f["kind"] for f in body["flags"]] == ["injection"] and body["flags"][0]["evidence"]
    assert body["review"] is None and body["letter"] is None


def test_a_failed_rule_shows_as_failed(run, tmp_path):
    start, _ = run
    start(application("gp-018", tmp_path))  # revenue out of range
    _, body = get("/applications/gp-018")
    assert [r["rule"] for r in body["rules"] if not r["passed"]] == ["revenue_range"]
    assert not body["waiting_for_review"] and body["letter"]


def test_a_decided_application_shows_the_decision_and_letter(run, tmp_path):
    start, resume = run
    start(application("gp-027", tmp_path))
    resume("gp-027", decision("approve"))
    _, body = get("/applications/gp-027")
    assert body["record"]["decision"] == "approve" and body["review"]["reason"] and body["letter"]


def test_the_reading_graph_can_never_call_a_model(run, tmp_path):
    with pytest.raises(RuntimeError, match="only reads"):
        workflow.start(api._workflow(), application("gp-027", tmp_path))  # even if someone ran it, its steps refuse


@pytest.mark.parametrize("application_id", ["missing", "gp-027"])
def test_an_unknown_application_is_not_found(run, application_id):
    assert get(f"/applications/{application_id}")[0] == 404  # gp-027 was never run in this test


@pytest.mark.parametrize("application_id", ["..%2Fx", "a b", ""])
def test_an_unsafe_id_is_refused(run, application_id):
    assert get(f"/applications/{application_id}")[0] == 400


def test_an_unknown_route_is_not_found(run):
    response = api.handler(
        {"routeKey": "DELETE /applications/{application_id}", "pathParameters": {"application_id": "a"}}
    )
    assert response["statusCode"] == 404


def test_the_log_line_has_no_applicant_text(run, tmp_path, caplog):
    start, _ = run
    state = application("gp-039", tmp_path)
    start(state)
    with caplog.at_level("INFO", logger="grant_review"):
        get("/applications/gp-039")
    [line] = caplog.records
    assert line.route == "GET /applications/{application_id}" and line.status_code == 200
    assert state.narrative[:40] not in caplog.text and state.form.organization_name not in caplog.text
