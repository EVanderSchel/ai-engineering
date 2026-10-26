"""The comparison harness (step 9) without AWS: both versions stood in for in memory with the stand-in steps (the
LangGraph graph with a memory checkpointer behind the real LangGraphOnLambda; the state machine's path behind the real
StepFunctions class), so the same decisions reach both and their outcomes can be compared. The measures from an
execution's history are tested on a recorded-shape history."""

import datetime as dt
import itertools
import json
from types import SimpleNamespace

import pytest
from test_sfn_steps import STEPS, roundtrip
from test_workflow import application

import compare
import sfn_steps
import workflow
from state import ReviewerDecision


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):  # noqa: N803 (boto3's names)
        self.items[Item["application_id"]] = Item

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        item = self.items.get(Key["application_id"])
        return {"Item": item} if item else {}


class FakeStepFunctions:
    """The state machine's path (infra/lib/step-functions-stack.ts) with the stand-in steps: pauses at HumanReview
    after leaving a task token in the table, as the steps function's await_review does."""

    def __init__(self, table, states):
        self.table, self.states, self.runs, self.tokens = table, states, {}, itertools.count(1)

    def _path(self, state):
        run = lambda step, s, **extra: roundtrip(sfn_steps.run(STEPS, step, s, **extra))  # noqa: E731
        state = run("join", state, branches={step: run(step, state) for step in sfn_steps.BRANCHES})
        state = run("cross_check", state)
        while True:
            state = run("eligibility", state)
            state = run("route", state)
            if state.route == "decline_ineligible":
                break
            token = f"tok-{next(self.tokens)}"
            self.table.put_item(Item={"application_id": state.application_id, "task_token": token, "ttl": 1})
            state = run("apply_review", state, decision=json.loads((yield token)))
            if state.review.decision is not None:
                break
        return run("draft_letter", state)

    def _advance(self, name, value):
        execution = self.runs[name]
        try:
            execution["token"] = execution["path"].send(value)
        except StopIteration as done:
            execution.update(status="SUCCEEDED", output=done.value.model_dump_json(), token=None)

    def start_execution(self, stateMachineArn, name, input):  # noqa: A002, N803
        assert name not in self.runs, "an execution name is used once"
        self.runs[name] = {"status": "RUNNING", "path": self._path(self.states[json.loads(input)["application_id"]])}
        self._advance(name, None)

    def describe_execution(self, executionArn):  # noqa: N803
        execution = self.runs[executionArn.rsplit(":", 1)[1]]
        return {"status": execution["status"], "output": execution.get("output")}

    def send_task_success(self, taskToken, output):  # noqa: N803
        [name] = [n for n, e in self.runs.items() if e.get("token") == taskToken]
        self._advance(name, output)

    def get_paginator(self, operation):
        return type("Pages", (), {"paginate": lambda self, executionArn: [{"events": []}]})()


class FakeLambda:
    """The Resume function: the decision into the graph, validated first, as handlers.resume does."""

    def __init__(self, app):
        self.app = app

    def invoke(self, FunctionName, Payload):  # noqa: N803
        event = json.loads(Payload)
        workflow.resume(self.app, event["application_id"], ReviewerDecision.model_validate(event["decision"]))
        return {"StatusCode": 200}


def versions(tmp_path, packet_id, application_id):
    app = workflow.build(STEPS, checkpointer=workflow.memory_checkpointer())
    lg = compare.LangGraphOnLambda.__new__(compare.LangGraphOnLambda)
    lg.app, lg.lam, lg.resume_function, lg.resumes = app, FakeLambda(app), "Resume", compare.Counter()
    table = FakeTable()
    sf = compare.StepFunctions.__new__(compare.StepFunctions)
    sf.reviews, sf.used_tokens, sf.bucket = table, set(), "intake"
    sf.state_machine = "arn:aws:states:us-east-2:111122223333:stateMachine:GrantReview-workflow-sfn"
    state = application(packet_id, tmp_path).model_copy(update={"application_id": application_id})
    sf.sfn = FakeStepFunctions(table, {application_id: state})
    start_upload = lambda: workflow.start(app, state)  # noqa: E731 (the upload's event runs Start)
    return [lg, sf], start_upload


def run(tmp_path, packet_id):
    both, start_upload = versions(tmp_path, packet_id, f"{packet_id}-cmpT")
    clock = itertools.count(0, 0.25).__next__
    return compare.run_packet(both, packet_id, f"{packet_id}-cmpT", start_upload, clock=clock, sleep=lambda s: None)


@pytest.mark.parametrize("packet_id", ["gp-003", "gp-018", "gp-039"])  # approved; declined by the rules; injection
def test_both_versions_get_the_same_decisions_and_reach_the_same_outcome(tmp_path, packet_id):
    result = run(tmp_path, packet_id)
    lg, sf = result["langgraph"], result["step_functions"]
    assert lg["status"] == sf["status"] == "done"
    assert compare.agreement([result]) == {"both_finished": 1, "same_outcome": 1, "differences": {}}
    reviewed = lg["row"]["route"] == "review"
    assert lg["decision"] == sf["decision"] == ("approve" if reviewed else None)
    assert [r["status"] for r in lg["rounds"]] == (["waiting", "done"] if reviewed else ["done"])
    assert lg["letter"] and sf["letter"]


def test_langgraph_is_running_while_its_parallel_steps_have_saved_results_but_the_next_checkpoint_isnt_written():
    def task(name, result, interrupts=()):
        return SimpleNamespace(name=name, result=result, interrupts=interrupts)

    def snapshot(next_, tasks):
        return SimpleNamespace(values={"application_id": "x"}, next=next_, tasks=tasks)

    lg = compare.LangGraphOnLambda.__new__(compare.LangGraphOnLambda)
    saved = [task(n, {"done": True}) for n in ("extract", "research", "read_proposal")]
    for snap, expected in [
        (snapshot((), saved), "running"),  # LangGraph's `next` leaves out tasks that have results: empty, not finished
        (snapshot(("human_review",), [task("human_review", None, ("pause",))]), "waiting"),
        (snapshot((), ()), "done"),
    ]:
        lg.app = SimpleNamespace(get_state=lambda config, s=snap: s)
        assert lg.status("x") == expected


def test_corrections_then_a_decline_take_two_rounds_in_both(tmp_path):
    result = run(tmp_path, "gp-027")
    for name in ("langgraph", "step_functions"):
        assert [r["status"] for r in result[name]["rounds"]] == ["waiting", "waiting", "done"], name
        assert result[name]["decision"] == "decline"
    assert result["langgraph"]["lambda_invocations"] == 3  # Start, then Resume twice
    assert compare.agreement([result])["same_outcome"] == 1


def test_the_report_puts_the_versions_side_by_side(tmp_path):
    packets = [run(tmp_path, "gp-003"), run(tmp_path, "gp-018")]
    result = {
        "run_id": "T",
        "date": "2026-10-26",
        "packets": packets,
        "agreement": compare.agreement(packets),
        "langgraph": compare.summarize(packets, "langgraph"),
        "step_functions": compare.summarize(packets, "step_functions"),
    }
    lg = result["langgraph"]
    assert lg["scores"]["route_accuracy"] == 1.0 and lg["scores"]["wrong_automatic_declines"] == 0
    assert "extract+read_proposal+research" in lg["stage_seconds_median"]  # sorted, as Step Functions' Read maps
    assert "human_review" not in lg["stage_seconds_median"]  # the reviewer's time isn't the system's
    text = compare.report(result)
    assert "| Finished | 2 | 2 |" in text and "same outcome on 2 of the 2" in text


def test_a_disagreement_is_reported_by_packet():
    entry = lambda route, flags: {  # noqa: E731
        "status": "done",
        "decision": None,
        "row": {"route": route, "failed_rules": [], "flags": flags},
    }
    packets = [
        {
            "packet_id": "gp-002",
            "langgraph": entry("review", ["staff_mismatch"]),
            "step_functions": entry("review", []),
        },
        {"packet_id": "gp-003", "langgraph": entry("review", []), "step_functions": {"status": "failed"}},
    ]
    assert compare.agreement(packets) == {
        "both_finished": 1,
        "same_outcome": 0,
        "differences": {"gp-002": {"flags": [["staff_mismatch"], []]}},
    }


def test_the_decisions_are_the_same_for_every_version_and_valid():
    now = dt.datetime(2026, 10, 26, tzinfo=dt.UTC)
    assert [d.decision for d in compare.decisions_for("gp-027", now)] == [None, "decline"]
    assert compare.decisions_for("gp-003", now)[0].decision == "approve"
    assert {d.reviewer for d in compare.decisions_for("gp-027", now)} == {"compare-script"}


def test_a_version_that_never_stops_is_failed_after_the_timeout():
    stuck = type("Stuck", (), {"name": "stuck", "status": lambda self, application_id: "running"})()
    clock = itertools.count(0, 60).__next__
    seen = compare.wait_for([stuck], "gp-003-x", sent=0, clock=clock, sleep=lambda s: None)
    assert seen == {"stuck": {"status": "failed", "seconds": None, "timed_out": True}}


class RecordingS3:
    def __init__(self):
        self.keys = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.keys.append(Key)

    def upload_file(self, filename, bucket, key):
        self.keys.append(key)


def test_form_json_is_uploaded_last_since_its_event_starts_the_langgraph_version():
    s3 = RecordingS3()
    compare.upload(s3, "intake", "gp-003", "gp-003-cmpT")
    assert s3.keys[-1] == "gp-003-cmpT/form.json"
    assert sorted(s3.keys) == [f"gp-003-cmpT/{f}" for f in ("form.json", "narrative.md", "return.pdf")]


def test_only_dev_packets_can_be_run(monkeypatch):
    monkeypatch.setattr("sys.argv", ["compare.py", "--packets", "gp-011"])  # a test packet
    with pytest.raises(SystemExit):
        compare.main()


T0 = dt.datetime(2026, 10, 26, 12, tzinfo=dt.UTC)


def event(id_, type_, seconds, name=None, previous=None):
    e = {"id": id_, "type": type_, "timestamp": T0 + dt.timedelta(seconds=seconds), "previousEventId": previous or 0}
    if type_.endswith("StateEntered"):
        e["stateEnteredEventDetails"] = {"name": name}
    if type_.endswith("StateExited"):
        e["stateExitedEventDetails"] = {"name": name}
    return e


def test_history_gives_stage_times_transitions_invocations_and_retries():
    events = [
        event(1, "ExecutionStarted", 0),
        event(2, "TaskStateEntered", 0, "Intake", 1),
        event(3, "TaskScheduled", 0.1, previous=2),
        event(4, "TaskStateExited", 1.5, "Intake", 3),
        event(5, "ParallelStateEntered", 1.5, "Read", 4),
        event(6, "TaskStateEntered", 1.6, "Extract", 5),
        event(7, "TaskStateEntered", 1.6, "Research", 5),
        event(8, "TaskScheduled", 1.7, previous=6),
        event(9, "TaskScheduled", 1.7, previous=7),
        event(10, "TaskFailed", 3.0, previous=8),  # Extract failed once, then its Retry ran it again
        event(11, "TaskScheduled", 33.0, previous=10),
        event(12, "TaskStateExited", 2.0, "Research", 9),
        event(13, "TaskStateExited", 40.0, "Extract", 11),
        event(14, "ParallelStateExited", 40.1, "Read", 13),
    ]
    m = compare.history_measures(events)
    assert m["stages"] == [
        {"stage": "intake", "seconds": 1.5},
        {"stage": "extract+read_proposal+research", "seconds": 38.6},
    ]
    assert m["branches"] == [{"stage": "Research", "seconds": 0.4}, {"stage": "Extract", "seconds": 38.4}]
    assert (m["state_transitions"], m["lambda_invocations"]) == (4, 4)
    assert m["task_failures"] == {"Extract": 1}
