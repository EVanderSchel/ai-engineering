"""The drift monitor's free checks: each answer's format and finish reason, in the log line and the metric, and the
generated alert rules, which must be current with the canary's baselines. No network, no model."""

import json

import pytest
from conftest import AUTH, TEST_KEY, no_wait_policy, routes
from fastapi.testclient import TestClient
from test_dashboard import METRIC, exported
from test_prompts import write_prompt

import app as gateway
import drift
import keys
import prompts
from stub_provider import StubProvider


def prompt_with(answer_format):
    return prompts.Prompt("p", "v1", "Answer.", prompts.fingerprint("Answer."), answer_format)


@pytest.mark.parametrize(
    ("text", "finish", "answer_format", "expected"),
    [
        ("B, O, N", "stop", r"[A-Z](\s*,\s*[A-Z]){0,2}", "ok"),
        ("  B,O  \n", "stop", r"[A-Z](\s*,\s*[A-Z]){0,2}", "ok"),  # surrounding whitespace doesn't count
        ("It's a school, so B.\n\nB, O", "stop", r"[A-Z](\s*,\s*[A-Z]){0,2}", "bad"),  # explained first
        ("", "length", r"[A-Z](\s*,\s*[A-Z]){0,2}", "bad"),  # thought until cut off
        ("[withheld]", "content_filter", r"[A-Z]", None),  # the guardrails' notice isn't the model's answer
        ("anything", "stop", None, None),  # the prompt declares no format
    ],
)
def test_an_answer_is_checked_against_its_prompts_format(text, finish, answer_format, expected):
    signals = drift.answer_signals(text, finish, prompt_with(answer_format))
    assert signals == {"finish": finish, "answer_format": expected}


def test_without_a_prompt_only_the_finish_reason_is_recorded():
    assert drift.answer_signals("hi", "stop", None) == {"finish": "stop", "answer_format": None}


def test_the_registry_reads_answer_formats_and_refuses_broken_ones(tmp_path):
    write_prompt(tmp_path, "letters", {"v1": "Answer with a letter."})
    manifest = tmp_path / "letters" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["versions"]["v1"]["answer_format"] = "[A-Z]"
    manifest.write_text(json.dumps(data), "utf-8")
    assert prompts.Registry(tmp_path).resolve("letters").well_formed("B") is True
    data["versions"]["v1"]["answer_format"] = "[A-Z"
    manifest.write_text(json.dumps(data), "utf-8")
    with pytest.raises(RuntimeError, match="answer_format isn't a valid pattern"):
        prompts.Registry(tmp_path)


def test_ntee_classify_v1_declares_the_format_the_canary_parses():
    v1 = prompts.Registry().resolve("ntee-classify@v1")
    assert v1.well_formed("B, O, N") and v1.well_formed("X") and not v1.well_formed("Probably B.\nB")


# --- through the gateway ---


@pytest.fixture
def client(tmp_path):
    # The stub answers "Stub reply to: <the message>", so a one-word message gives a well-formed answer here.
    write_prompt(tmp_path, "echo", {"v1": "Echo."})
    manifest = tmp_path / "echo" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["versions"]["v1"]["answer_format"] = r"Stub reply to: \w+"
    manifest.write_text(json.dumps(data), "utf-8")
    app = gateway.create_app(
        routes(), {"stub": StubProvider(), "anthropic": StubProvider("anthropic")}, {keys.key_hash(TEST_KEY): "test"},
        policy=no_wait_policy(), prompt_registry=prompts.Registry(tmp_path),
    )  # fmt: skip
    return TestClient(app)


def ask(client, text, stream=False, **options):
    body = {"model": "stub", "max_tokens": 20, "stream": stream, "messages": [{"role": "user", "content": text}]}
    if options:
        body["gateway"] = options
    with client.stream("POST", "/v1/chat/completions", json=body, headers=AUTH) as response:
        assert response.status_code == 200
        list(response.iter_lines())


def last_log_line(capsys) -> dict:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"message":"request"' in line]
    return lines[-1]


@pytest.mark.parametrize("stream", [False, True])
def test_each_answer_is_checked_logged_and_counted(client, capsys, stream):
    ask(client, "hello", stream=stream, prompt="echo")
    line = last_log_line(capsys)
    assert (line["finish"], line["answer_format"]) == ("stop", "ok")
    assert "Stub reply" not in json.dumps(line)  # the answer itself is never logged
    ask(client, "two words", stream=stream, prompt="echo")
    assert last_log_line(capsys)["answer_format"] == "bad"
    ask(client, "no prompt", stream=stream)
    assert "answer_format" not in last_log_line(capsys)  # a log line leaves out empty fields
    rendered = client.app.state.metrics.render().decode()
    assert 'gateway_answers_total{finish="stop",format="ok",prompt="echo@v1",route="stub"} 1.0' in rendered
    assert 'gateway_answers_total{finish="stop",format="bad",prompt="echo@v1",route="stub"} 1.0' in rendered
    assert 'gateway_answers_total{finish="stop",format="",prompt="",route="stub"} 1.0' in rendered


def test_a_cut_off_answer_is_counted_as_cut_off(client, capsys):
    body = {"model": "stub", "max_tokens": 2, "messages": [{"role": "user", "content": "hello"}],
            "gateway": {"prompt": "echo"}}  # fmt: skip
    assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code == 200
    line = last_log_line(capsys)
    assert (line["finish"], line["answer_format"]) == ("length", "bad")


def test_refused_requests_have_no_answer_to_count(client, capsys):
    body = {"model": "stub", "max_tokens": 20, "messages": [{"role": "user", "content": "hi"}],
            "gateway": {"prompt": "nope"}}  # fmt: skip
    assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code == 404
    assert "finish" not in last_log_line(capsys)
    assert "gateway_answers_total{" not in client.app.state.metrics.render().decode()


# --- the generated rules ---


def test_the_rules_are_current_with_the_canary_baselines():
    committed = drift.RULES_FILE.read_text("utf-8").replace("\r\n", "\n")
    assert committed == drift.render(drift.references()), "regenerate: uv run python src/drift.py"


def test_the_references_are_what_the_baselines_measured():
    refs = {r["check"]: r for r in drift.references()}
    assert set(refs) == {"ntee-fast", "ntee-smart"}  # arc-local has no prompt, so no format to check
    assert refs["ntee-fast"]["bad_format"] == 0.0  # Haiku answered in the format every time
    assert refs["ntee-smart"]["bad_format"] == 0.05  # Sonnet explained itself first on 3 of 60
    assert all(r["prompt"] == "ntee-classify@v1" and r["answers"] == 60 for r in refs.values())
    # The judge's grades of the same answers (drift_judge.py, run once, $0.034).
    assert (refs["ntee-fast"]["judge_mean"], refs["ntee-smart"]["judge_mean"]) == (4.267, 4.3)


def test_the_drift_rules_ask_for_metrics_the_gateway_exports():
    import yaml

    rules = yaml.safe_load(drift.RULES_FILE.read_text("utf-8"))["groups"][0]["rules"]
    names = {m for r in rules for m in METRIC.findall(r["expr"])}
    assert "gateway_answers_total" in names and names <= exported(), names - exported()
    assert {r["alert"] for r in rules if "alert" in r} >= {"GatewayAnswerFormatDrift", "GatewayAnswersCutOff"}
