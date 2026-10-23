"""The reviewer UI's API, without AWS: applications run with stand-in steps (tests/test_workflow.right_answers) into an
in-memory checkpointer and status store, then read back through the handler as API Gateway would call it."""

import datetime as dt
import io
import json

import pytest
from test_workflow import application, decision, right_answers

import api
import applications
import handlers
import workflow

NOW = dt.datetime(2025, 7, 1, 12, tzinfo=dt.UTC)


def wire(monkeypatch, steps):
    """Stand-in steps and in-memory stores behind the API and the Lambda handlers both, as one deployment shares the
    checkpoint and status tables. A decision runs handlers.resume in-process, as the Resume function would."""
    checkpointer = workflow.memory_checkpointer()
    graph = workflow.build(steps, checkpointer=checkpointer)
    store = applications.MemoryStore()
    monkeypatch.setattr(api, "_workflow", lambda: workflow.reader(checkpointer))
    monkeypatch.setattr(api, "_applications", lambda: store)
    monkeypatch.setattr(handlers, "_workflow", lambda: graph)
    monkeypatch.setattr(handlers, "_applications", lambda: store)
    monkeypatch.setattr(api, "_resume", handlers.resume)

    def start(app_state):
        outcome = workflow.start(graph, app_state)
        store.put(applications.record(outcome, NOW))
        return outcome

    def resume(application_id, review):
        outcome = workflow.resume(graph, application_id, review)
        store.put(applications.record(outcome, NOW))
        return outcome

    return start, resume


@pytest.fixture
def run(monkeypatch):
    """Runs applications with stand-in steps and records them, as the Lambda handlers do; the API reads both."""
    return wire(monkeypatch, right_answers())


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
        {"error": "status must be one of ['waiting_for_review', 'deciding', 'decided', 'declined_by_rules']"},
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


# --- Decisions (stage 3) ----------------------------------------------------------------------------------------

REVIEWER = "reviewer@example.org"
APPROVE = {"decision": "approve", "corrections": {}, "reason": "Clear outcomes and a realistic budget."}


def post_decision(application_id, body, email=REVIEWER):
    """A decision as API Gateway delivers it, with the claims its JWT authorizer verified (none: not signed in)."""
    claims = {"requestContext": {"authorizer": {"jwt": {"claims": {"email": email}}}}} if email else {}
    response = api.handler(
        {
            "routeKey": "POST /applications/{application_id}/decision",
            "pathParameters": {"application_id": application_id},
            "body": body if isinstance(body, str) else json.dumps(body),
            **claims,
        }
    )
    return response["statusCode"], json.loads(response["body"])


def test_a_decision_resumes_the_application_and_returns_its_new_evidence(run, tmp_path):
    start, _ = run
    start(application("gp-027", tmp_path))
    status, body = post_decision("gp-027", APPROVE)
    assert status == 200 and body["record"]["status"] == "decided" and body["record"]["decision"] == "approve"
    assert body["review"]["reviewer"] == REVIEWER and body["review"]["reason"] == APPROVE["reason"]
    assert body["letter"] == "Letter for gp-027" and not body["waiting_for_review"]
    assert get("/applications")[1]["applications"] == []  # out of the queue


def test_the_reviewer_and_time_come_from_the_sign_in_and_the_server_never_the_body(run, tmp_path):
    start, _ = run
    start(application("gp-027", tmp_path))
    status, body = post_decision("gp-027", {**APPROVE, "reviewer": "someone-else@example.org"})
    assert status == 400 and "reviewer: Extra inputs are not permitted" in body["error"]
    assert post_decision("gp-027", APPROVE, email=None) == (401, {"error": "sign in to decide"})
    before = dt.datetime.now(dt.UTC)
    decided_at = dt.datetime.fromisoformat(post_decision("gp-027", APPROVE)[1]["review"]["decided_at"])
    assert before <= decided_at <= dt.datetime.now(dt.UTC)


def test_corrections_alone_rerun_the_rules_and_wait_again(run, tmp_path):
    start, _ = run
    start(application("gp-027", tmp_path))
    corrected = {"decision": None, "corrections": {"employees": 12}, "reason": "Employees misread."}
    status, body = post_decision("gp-027", corrected)
    assert status == 200 and body["waiting_for_review"] and body["record"]["status"] == "waiting_for_review"
    assert body["review"]["corrections"] == {"employees": 12}
    status, body = post_decision("gp-027", {**corrected, "corrections": {"total_revenue_current_year": 10}})
    assert status == 200 and body["record"]["status"] == "declined_by_rules"  # the rules decline on the new figure


@pytest.mark.parametrize(
    "body, message",
    [
        ({**APPROVE, "reason": ""}, "reason: String should have at least 1 character"),
        ({**APPROVE, "reason": "x" * 2001}, "reason: String should have at most 2000 characters"),
        ({**APPROVE, "decision": "maybe"}, "decision: Input should be 'approve' or 'decline'"),
        ({**APPROVE, "corrections": {"salary": 1}}, "not figures on the return: ['salary']"),
        ("not json", "body: Invalid JSON"),
    ],
)
def test_an_invalid_decision_is_refused_and_the_application_still_waits(run, tmp_path, body, message):
    start, _ = run
    start(application("gp-027", tmp_path))
    status, response = post_decision("gp-027", body)
    assert status == 400 and message in response["error"]
    assert get("/applications/gp-027")[1]["record"]["status"] == "waiting_for_review"


def test_only_one_decision_gets_through(run, tmp_path):
    start, _ = run
    start(application("gp-027", tmp_path))
    assert post_decision("gp-027", APPROVE)[0] == 200
    status, body = post_decision("gp-027", {**APPROVE, "decision": "decline"})
    assert status == 409 and "isn't waiting" in body["error"]
    assert get("/applications/gp-027")[1]["record"]["decision"] == "approve"


def test_an_application_being_decided_refuses_a_second_reviewer(run, tmp_path):
    start, _ = run
    start(application("gp-027", tmp_path))
    assert api._applications().claim("gp-027", dt.datetime.now(dt.UTC))  # someone else's request, still running
    assert post_decision("gp-027", APPROVE)[0] == 409


def test_a_decision_for_an_unknown_application_is_not_found(run):
    assert post_decision("gp-999", APPROVE)[0] == 404


def test_a_resume_that_never_reached_the_graph_puts_the_application_back(run, tmp_path, monkeypatch):
    start, _ = run
    start(application("gp-027", tmp_path))

    def outage(payload):
        raise api.ResumeFailed("ServiceUnavailableException")

    monkeypatch.setattr(api, "_resume", outage)
    status, body = post_decision("gp-027", APPROVE)
    assert status == 502 and "nothing changed" in body["error"]
    assert get("/applications/gp-027")[1]["record"]["status"] == "waiting_for_review"
    monkeypatch.setattr(api, "_resume", handlers.resume)
    assert post_decision("gp-027", APPROVE)[0] == 200  # and the retry goes through


def test_a_decision_taken_but_not_finished_stays_claimed_and_is_logged(monkeypatch, tmp_path, caplog):
    steps = right_answers()

    def letter_outage(state):
        raise RuntimeError("Bedrock unavailable")

    start, _ = wire(monkeypatch, workflow.Steps(steps.extract, steps.research, steps.read_proposal, letter_outage))
    start(application("gp-027", tmp_path))
    with caplog.at_level("INFO", logger="grant_review"):
        status, body = post_decision("gp-027", APPROVE)
    assert status == 502 and "needs finishing" in body["error"]
    assert get("/applications/gp-027")[1]["record"]["status"] == "deciding"  # not back in the queue: it can't resume
    assert [r.application_id for r in caplog.records if r.levelname == "ERROR"] == ["gp-027"]


def test_the_resume_function_is_invoked_with_the_decision_and_its_errors_raise(monkeypatch):
    calls = []

    class FakeLambda:
        def invoke(self, FunctionName, Payload):
            calls.append((FunctionName, json.loads(Payload)))
            error = {"errorType": "ValueError", "errorMessage": "may quote applicant text"}
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(json.dumps(error).encode())}

    monkeypatch.setenv("RESUME_FUNCTION", "GrantReviewWorkflow-Resume")
    monkeypatch.setattr(api, "_lambda", lambda: FakeLambda())
    with pytest.raises(api.ResumeFailed, match="^ValueError$"):
        api._resume({"application_id": "gp-027", "decision": {}})
    assert calls == [("GrantReviewWorkflow-Resume", {"application_id": "gp-027", "decision": {}})]
