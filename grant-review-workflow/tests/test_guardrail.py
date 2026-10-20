"""The guardrail, without AWS: botocore's Stubber checks every request against Bedrock's API definition (so the
guardrail's config is validated too) and answers with canned responses."""

import datetime as dt

import boto3
import pymupdf
import pytest
from botocore.stub import ANY, Stubber

import guardrail
import red_team
from intake import gold_packet, intake

NOW = dt.datetime(2026, 10, 20, tzinfo=dt.UTC)
ID = "abc123xyz9"
ARN = f"arn:aws:bedrock:us-east-2:111122223333:guardrail/{ID}"


def stubbed(service):
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-2")
    client = session.client(service)
    return client, Stubber(client)


@pytest.fixture
def control():
    client, stubber = stubbed("bedrock")
    with stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


@pytest.fixture
def runtime():
    client, stubber = stubbed("bedrock-runtime")
    with stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def summary(version="DRAFT", status="READY"):
    return {
        "id": ID,
        "arn": ARN,
        "status": status,
        "name": guardrail.NAME,
        "version": version,
        "createdAt": NOW,
        "updatedAt": NOW,
    }


def got(version, status="READY"):
    return {
        "name": guardrail.NAME,
        "guardrailId": ID,
        "guardrailArn": ARN,
        "version": version,
        "status": status,
        "createdAt": NOW,
        "updatedAt": NOW,
        "blockedInputMessaging": "x",
        "blockedOutputsMessaging": "x",
    }


def test_the_config_only_detects():
    config = guardrail.config()
    rules = (
        config["contentPolicyConfig"]["filtersConfig"] + config["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]
    )
    assert {r["inputAction"] for r in rules} == {"NONE"} and not any(r["outputEnabled"] for r in rules)
    assert {r["action"] for r in config["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]} == {"NONE"}


def test_screening_stays_in_the_region():
    config = guardrail.config()
    assert config["contentPolicyConfig"]["tierConfig"] == {"tierName": "CLASSIC"} and "crossRegionConfig" not in config


def test_create_makes_the_guardrail_and_publishes_the_pinned_version(control):
    client, stubber = control
    stubber.add_response("list_guardrails", {"guardrails": []}, {})
    stubber.add_response(
        "create_guardrail",
        {"guardrailId": ID, "guardrailArn": ARN, "version": "DRAFT", "createdAt": NOW},
        guardrail.config(),
    )
    stubber.add_response(
        "get_guardrail", got("DRAFT", "CREATING"), {"guardrailIdentifier": ID, "guardrailVersion": "DRAFT"}
    )
    stubber.add_response("get_guardrail", got("DRAFT"), {"guardrailIdentifier": ID, "guardrailVersion": "DRAFT"})
    stubber.add_response("list_guardrails", {"guardrails": [summary()]}, {"guardrailIdentifier": ID})
    stubber.add_response(
        "create_guardrail_version", {"guardrailId": ID, "version": "1"}, {"guardrailIdentifier": ID, "description": ANY}
    )
    stubber.add_response("get_guardrail", got("1"), {"guardrailIdentifier": ID, "guardrailVersion": "1"})
    assert guardrail.create(client, sleep=lambda s: None) == ID


def test_create_is_rerunnable(control):
    client, stubber = control
    stubber.add_response("list_guardrails", {"guardrails": [summary()]}, {})
    stubber.add_response("get_guardrail", got("DRAFT"), {"guardrailIdentifier": ID, "guardrailVersion": "DRAFT"})
    published = [summary(), summary("1"), summary("2")]
    stubber.add_response("list_guardrails", {"guardrails": published}, {"guardrailIdentifier": ID})
    assert guardrail.create(client, sleep=lambda s: None) == ID  # nothing created or published


def test_a_new_version_updates_the_draft_then_publishes_it(control, monkeypatch):
    monkeypatch.setattr(guardrail, "VERSION", "2")
    client, stubber = control
    draft = {"guardrailIdentifier": ID, "guardrailVersion": "DRAFT"}
    stubber.add_response("list_guardrails", {"guardrails": [summary()]}, {})
    stubber.add_response("get_guardrail", got("DRAFT"), draft)
    stubber.add_response("list_guardrails", {"guardrails": [summary(), summary("1")]}, {"guardrailIdentifier": ID})
    update = {k: v for k, v in guardrail.config().items() if k != "tags"}
    stubber.add_response(
        "update_guardrail",
        {"guardrailId": ID, "guardrailArn": ARN, "version": "DRAFT", "updatedAt": NOW},
        {"guardrailIdentifier": ID, **update},
    )
    stubber.add_response("get_guardrail", got("DRAFT", "UPDATING"), draft)
    stubber.add_response("get_guardrail", got("DRAFT"), draft)
    stubber.add_response(
        "create_guardrail_version", {"guardrailId": ID, "version": "2"}, {"guardrailIdentifier": ID, "description": ANY}
    )
    stubber.add_response("get_guardrail", got("2"), {"guardrailIdentifier": ID, "guardrailVersion": "2"})
    assert guardrail.create(client, sleep=lambda s: None) == ID


def test_create_refuses_to_skip_a_version(control, monkeypatch):
    monkeypatch.setattr(guardrail, "VERSION", "3")
    client, stubber = control
    stubber.add_response("list_guardrails", {"guardrails": [summary()]}, {})
    stubber.add_response("get_guardrail", got("DRAFT"), {"guardrailIdentifier": ID, "guardrailVersion": "DRAFT"})
    stubber.add_response("list_guardrails", {"guardrails": [summary(), summary("1")]}, {"guardrailIdentifier": ID})
    with pytest.raises(RuntimeError, match="published versions"):
        guardrail.create(client, sleep=lambda s: None)


def test_report_says_when_the_guardrail_is_missing(control, capsys):
    client, stubber = control
    stubber.add_response("list_guardrails", {"guardrails": []}, {})
    assert guardrail.report(client) is None
    assert "doesn't exist" in capsys.readouterr().out


def answer(attack=None, pii=(), units=4):
    """An ApplyGuardrail response in detect-only mode: action NONE, detections reported."""
    filters = [
        {
            "type": "PROMPT_ATTACK",
            "confidence": attack or "NONE",
            "filterStrength": "HIGH",
            "action": "NONE",
            "detected": bool(attack),
        }
    ]
    entities = [{"match": match, "type": kind, "action": "NONE", "detected": True} for kind, match in pii]
    usage = {
        "topicPolicyUnits": 0,
        "contentPolicyUnits": units,
        "wordPolicyUnits": 0,
        "sensitiveInformationPolicyUnits": units,
        "sensitiveInformationPolicyFreeUnits": 0,
        "contextualGroundingPolicyUnits": 0,
    }
    assessment = {
        "contentPolicy": {"filters": filters},
        "sensitiveInformationPolicy": {"piiEntities": entities, "regexes": []},
        "invocationMetrics": {"guardrailProcessingLatency": 120},
    }
    return {"usage": usage, "action": "NONE", "outputs": [], "assessments": [assessment]}


def expect(stubber, text, response):
    params = {
        "guardrailIdentifier": ID,
        "guardrailVersion": guardrail.VERSION,
        "source": "INPUT",
        "content": [{"text": {"text": text}}],
        "outputScope": "FULL",
    }
    stubber.add_response("apply_guardrail", response, params)


def test_a_screening_keeps_types_never_the_personal_data(runtime):
    client, stubber = runtime
    expect(stubber, "text", answer("HIGH", [("US_SOCIAL_SECURITY_NUMBER", "123-45-6789"), ("EMAIL", "a@b.org")]))
    found = guardrail.screen_text(client, ID, "narrative", "text")
    assert found.prompt_attack == "HIGH"
    assert found.identifiers == ["US_SOCIAL_SECURITY_NUMBER"] and found.contact_details == ["EMAIL"]
    assert "123-45-6789" not in found.model_dump_json() and "a@b.org" not in found.model_dump_json()
    assert found.cost_usd == pytest.approx(4 * 0.25 / 1000) and found.latency_ms == 120


def test_a_clean_field_finds_nothing(runtime):
    client, stubber = runtime
    expect(stubber, "text", answer())
    found = guardrail.screen_text(client, ID, "narrative", "text")
    assert found.prompt_attack is None and found.identifiers == [] and found.contact_details == []


@pytest.fixture
def placeholder_pdf(tmp_path, monkeypatch):
    pdf = tmp_path / "r.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=612, height=792)
        doc.save(pdf)
    fake = lambda packet_id: (*gold_packet(packet_id)[:2], pdf)  # noqa: E731
    monkeypatch.setattr(red_team, "gold_packet", fake)
    monkeypatch.setattr(guardrail, "gold_packet", fake)
    monkeypatch.setattr(red_team, "ATTACK_DIR", tmp_path / "attacks")
    return pdf


def test_screen_sends_each_field_the_applicant_wrote(runtime, placeholder_pdf):
    client, stubber = runtime
    form, narrative, pdf = gold_packet("gp-027")[:2] + (placeholder_pdf,)
    application = intake(form, narrative, pdf, application_id="s1")
    expect(stubber, application.form.organization_name, answer(units=1))
    expect(stubber, application.form.project_title, answer(units=1))
    expect(stubber, application.narrative, answer("MEDIUM"))
    found = guardrail.screen(client, ID, application)
    assert [s.field for s in found] == list(guardrail.FIELDS)
    assert [s.prompt_attack for s in found] == [None, None, "MEDIUM"]
    assert guardrail.estimated_cost(application) == pytest.approx(
        (2 + -(-len(application.narrative) // 1000)) * 0.25 / 1000
    )


class KeywordRuntime:
    """A stand-in ApplyGuardrail that 'detects' an attack by keyword: enough to check compare's bookkeeping."""

    def apply_guardrail(self, content, **_):
        text = content[0]["text"]["text"]
        hit = any(w in text for w in ("AI reviewer", "SYSTEM", "system instructions"))
        return answer("HIGH" if hit else None, [("EMAIL", "x@y.org")] if "@" in text else [], units=1)


def test_compare_lines_the_guardrail_up_against_the_workflow(placeholder_pdf):
    result = guardrail.compare(KeywordRuntime(), ID)
    text_attacks = [a for a in red_team.attacks() if a.surface != "page_image"]
    assert result["cases"] == 15 + len(text_attacks)
    assert result["injections"] == result["guardrail_caught"] + sum(
        not r["guardrail"] for r in result["rows"] if r["injection_expected"]
    )
    by_case = {r["case"]: r for r in result["rows"]}
    assert by_case["gp-027-name-approve"]["guardrail_fields"] == ["organization_name (HIGH)"]
    assert by_case["gp-027-name-approve"]["read_proposal_rate"] == 0.0  # the red team's finding
    assert "gp-027-image-figures" not in by_case
    assert "Guardrail vs." in guardrail.comparison_report(result)
