"""The Bedrock guardrail (step 6b): a second, independent screen of the text an applicant writes, used only to flag.

    uv run python src/guardrail.py              # report: does it exist, its status and versions (changes nothing)
    uv run python src/guardrail.py --create     # create it and publish the pinned version if missing (rerunnable)
    uv run python src/guardrail.py --compare    # screen the dev packets and the red team's attacks (paid, ~$0.05)

What it screens: the organization name, project title and narrative, each in its own ApplyGuardrail call (the
standalone mode: no model runs, so the guardrail's verdict can't change what a model does, and each finding names
its field). The workflow's models never see a guardrail; read_proposal's own injection check is unchanged.

What it looks for, in detect-only mode (every action NONE, so nothing is blocked or rewritten; the response says
what was detected):
- prompt attacks (the content filter's PROMPT_ATTACK category, input strength HIGH);
- personal data: identifiers that never belong in an application (SSNs, bank and card numbers, passwords...), and
  contact details about people (names, emails, phones, addresses, ages).

Classic tier, so screening stays in us-east-2 (version 1, pinned). Version 2 tried the Standard tier, which needs
a cross-Region guardrail profile (us.guardrail.v1:0, US Regions only): on dev it caught 7 of 25 injections to
Classic's 9, with the same zero false alarms (results/guardrail-dev-v1.md, -v2.md), so it isn't used. The
guardrail's DRAFT still holds version 2's settings; nothing reads the DRAFT.

ApplyGuardrail returns the matched text of each personal-data finding. screen() keeps only the types: matched
text is the applicant's personal data and is never stored or printed.

Versions are immutable snapshots; the workflow pins VERSION. After changing config(), bump VERSION and run
--create, which publishes it. To remove the guardrail (all versions):
    aws bedrock delete-guardrail --guardrail-identifier <id> --region us-east-2
"""

import argparse
import json
import math
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

import bedrock
import evaluate
import paths
import red_team
from bedrock import REGION
from intake import gold_packet, intake
from state import ApplicationState

NAME = "grant-review-screen"
VERSION = "1"
TAGS = [{"key": "project", "value": "grant-review-workflow"}, {"key": "purpose", "value": "screen-applicant-text"}]
# ApplyGuardrail prices per 1,000 text units (a text unit is up to 1,000 characters), us-east-2 public price list.
PRICE_PER_1K_UNITS = {"contentPolicyUnits": 0.15, "sensitiveInformationPolicyUnits": 0.10}

IDENTIFIERS = [
    "US_SOCIAL_SECURITY_NUMBER",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "US_PASSPORT_NUMBER",
    "DRIVER_ID",
    "US_BANK_ACCOUNT_NUMBER",
    "US_BANK_ROUTING_NUMBER",
    "CREDIT_DEBIT_CARD_NUMBER",
    "PASSWORD",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
]
CONTACT_DETAILS = ["NAME", "EMAIL", "PHONE", "ADDRESS", "AGE"]
FIELDS = ("organization_name", "project_title", "narrative")


def config() -> dict:
    """CreateGuardrail's arguments: detect only, on input; nothing here is ever blocked."""
    detect = {"inputAction": "NONE", "inputEnabled": True, "outputAction": "NONE", "outputEnabled": False}
    return {
        "name": NAME,
        "description": "Screens an applicant's organization name, project title and narrative. Flags only.",
        "contentPolicyConfig": {
            "filtersConfig": [
                {
                    "type": "PROMPT_ATTACK",
                    "inputStrength": "HIGH",
                    "outputStrength": "NONE",  # prompt attacks are an input-only filter
                    **detect,
                }
            ],
            "tierConfig": {"tierName": "CLASSIC"},
        },
        "sensitiveInformationPolicyConfig": {
            "piiEntitiesConfig": [{"type": t, "action": "NONE", **detect} for t in IDENTIFIERS + CONTACT_DETAILS]
        },
        # Required by the API, never shown: nothing is blocked.
        "blockedInputMessaging": "Blocked by the grant-review screen.",
        "blockedOutputsMessaging": "Blocked by the grant-review screen.",
        "tags": TAGS,
    }


def control_client():
    """The `bedrock` management client (create and describe guardrails)."""
    return boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=REGION).client("bedrock")


def find(control) -> dict | None:
    """The guardrail's summary (its DRAFT), or None if it doesn't exist."""
    for page in control.get_paginator("list_guardrails").paginate():
        for summary in page["guardrails"]:
            if summary["name"] == NAME:
                return summary
    return None


def versions(control, guardrail_id: str) -> list[str]:
    numbered = []
    for page in control.get_paginator("list_guardrails").paginate(guardrailIdentifier=guardrail_id):
        numbered += [g["version"] for g in page["guardrails"] if g["version"] != "DRAFT"]
    return sorted(numbered, key=int)


def _wait_ready(control, guardrail_id: str, version: str, sleep=time.sleep) -> None:
    for _ in range(30):
        status = control.get_guardrail(guardrailIdentifier=guardrail_id, guardrailVersion=version)["status"]
        if status == "READY":
            return
        if status == "FAILED":
            raise RuntimeError(f"guardrail {guardrail_id} version {version} FAILED")
        sleep(2)
    raise TimeoutError(f"guardrail {guardrail_id} version {version} isn't READY after a minute")


def create(control, sleep=time.sleep) -> str:
    """Create the guardrail if missing and publish VERSION if it isn't published yet (updating the DRAFT to config()
    first, since a version is a snapshot of the DRAFT). Returns the guardrail ID."""
    summary = find(control)
    if summary is None:
        guardrail_id = control.create_guardrail(**config())["guardrailId"]
        print(f"Creating {NAME} in {REGION}...")
    else:
        guardrail_id = summary["id"]
    _wait_ready(control, guardrail_id, "DRAFT", sleep)
    published = versions(control, guardrail_id)
    if VERSION not in published:
        if len(published) + 1 != int(VERSION):
            raise RuntimeError(f"VERSION is {VERSION} but the published versions are {published}")
        if summary is not None:
            update = {k: v for k, v in config().items() if k != "tags"}  # tags aren't part of a version
            control.update_guardrail(guardrailIdentifier=guardrail_id, **update)
            _wait_ready(control, guardrail_id, "DRAFT", sleep)
        made = control.create_guardrail_version(guardrailIdentifier=guardrail_id, description=f"v{VERSION}")
        _wait_ready(control, guardrail_id, made["version"], sleep)
    return guardrail_id


def report(control) -> str | None:
    summary = find(control)
    if summary is None:
        print(f"{NAME} doesn't exist in {REGION}. Create it with --create.")
        return None
    published = versions(control, summary["id"])
    pinned = "published" if VERSION in published else "NOT published: run --create"
    print(f"{NAME} ({REGION}): id {summary['id']}, DRAFT {summary['status']}, versions {published or 'none'}")
    print(f"  the workflow pins version {VERSION} ({pinned})")
    return summary["id"]


class Screening(BaseModel, frozen=True):
    """What the guardrail found in one field. Personal-data findings are types only, never the matched text."""

    field: str
    prompt_attack: str | None = None  # the filter's confidence (LOW/MEDIUM/HIGH) when it detected one
    identifiers: list[str] = []
    contact_details: list[str] = []
    cost_usd: float = 0.0
    latency_ms: int = 0


def screen_text(runtime, guardrail_id: str, field: str, text: str) -> Screening:
    response = runtime.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=VERSION,
        source="INPUT",
        content=[{"text": {"text": text}}],
        outputScope="FULL",  # report detections even though no action was taken
    )
    attack, found, latency = None, set(), 0
    for assessment in response["assessments"]:
        for f in assessment.get("contentPolicy", {}).get("filters", []):
            if f["type"] == "PROMPT_ATTACK" and f.get("detected"):
                attack = f["confidence"]
        for entity in assessment.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            if entity.get("detected", True):
                found.add(entity["type"])  # entity["match"] (the personal data itself) is deliberately dropped
        latency += assessment.get("invocationMetrics", {}).get("guardrailProcessingLatency", 0)
    usage = response["usage"]
    cost = sum(usage.get(k, 0) * price / 1000 for k, price in PRICE_PER_1K_UNITS.items())
    return Screening(
        field=field,
        prompt_attack=attack,
        identifiers=sorted(t for t in found if t in IDENTIFIERS),
        contact_details=sorted(t for t in found if t in CONTACT_DETAILS),
        cost_usd=round(cost, 6),
        latency_ms=latency,
    )


def screen(runtime, guardrail_id: str, application: ApplicationState) -> list[Screening]:
    """One ApplyGuardrail call per field the applicant wrote."""
    texts = {
        "organization_name": application.form.organization_name,
        "project_title": application.form.project_title or "",
        "narrative": application.narrative,
    }
    return [screen_text(runtime, guardrail_id, field, texts[field]) for field in FIELDS if texts[field].strip()]


def estimated_cost(application: ApplicationState) -> float:
    """Before any call: each field is billed at least one text unit by each policy."""
    lengths = [
        len(application.form.organization_name),
        len(application.form.project_title or ""),
        len(application.narrative),
    ]
    units = sum(math.ceil(n / 1000) for n in lengths if n)
    return units * sum(PRICE_PER_1K_UNITS.values()) / 1000


def _row(name: str, expected: bool, model_rate: float, screenings: list[Screening]) -> dict:
    return {
        "case": name,
        "injection_expected": expected,
        "read_proposal_rate": round(model_rate, 3),  # share of the workflow's runs that raised an injection flag
        "guardrail": any(s.prompt_attack for s in screenings),
        "guardrail_fields": [f"{s.field} ({s.prompt_attack})" for s in screenings if s.prompt_attack],
        "identifiers": sorted({t for s in screenings for t in s.identifiers}),
        "contact_details": sorted({t for s in screenings for t in s.contact_details}),
        "cost_usd": round(sum(s.cost_usd for s in screenings), 6),
    }


def compare(runtime, guardrail_id: str) -> dict:
    """The guardrail against the workflow's own injection check, on what the workflow was already run on: the 15
    dev packets (3 runs each, results/eval-dev.json) and the red team's attacks on text (results/red-team-dev.json;
    page-image attacks aren't text the applicant typed, and extract already ignored them)."""
    rows = []
    eval_rows = json.loads((paths.ROOT / "results" / "eval-dev.json").read_text(encoding="utf-8"))["rows"]
    for packet in evaluate.packets("dev"):
        label = json.loads((paths.GOLD_DIR / packet / "label.json").read_text(encoding="utf-8"))
        runs = [r for r in eval_rows if r["packet_id"] == packet]
        rate = sum("injection" in r["flags"] for r in runs) / len(runs)
        form, narrative, pdf = gold_packet(packet)
        application = intake(form, narrative, pdf, application_id=f"{packet}-screen")
        rows.append(_row(packet, "injection" in label["flags"], rate, screen(runtime, guardrail_id, application)))
    attacked = {
        r["attack"]: r
        for r in json.loads((paths.ROOT / "results" / "red-team-dev.json").read_text(encoding="utf-8"))["rows"]
    }
    for attack in red_team.attacks():
        if attack.surface == "page_image":
            continue
        application = red_team.attacked_application(attack, f"{attack.id}-screen")
        rate = float("injection" in attacked[attack.id]["flags"])
        rows.append(_row(attack.id, True, rate, screen(runtime, guardrail_id, application)))
    expected = [r for r in rows if r["injection_expected"]]
    clean = [r for r in rows if not r["injection_expected"]]
    return {
        "guardrail": f"{NAME} v{VERSION}",
        "cases": len(rows),
        "injections": len(expected),
        "guardrail_caught": sum(r["guardrail"] for r in expected),
        "read_proposal_caught": sum(r["read_proposal_rate"] == 1.0 for r in expected),
        "either_caught": sum(r["guardrail"] or r["read_proposal_rate"] > 0 for r in expected),
        "clean": len(clean),
        "guardrail_false_alarms": sum(r["guardrail"] for r in clean),
        "read_proposal_false_alarms": sum(r["read_proposal_rate"] > 0 for r in clean),
        "with_identifiers": sum(bool(r["identifiers"]) for r in rows),
        "with_contact_details": sum(bool(r["contact_details"]) for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "rows": rows,
    }


def comparison_report(result: dict) -> str:
    lines = [
        f"# Guardrail vs. the workflow's injection check, dev ({result['guardrail']})",
        "",
        f"{result['injections']} cases with an injection: guardrail caught {result['guardrail_caught']}, "
        f"read_proposal/research caught {result['read_proposal_caught']} (in every run), either "
        f"{result['either_caught']}. {result['clean']} clean packets: false alarms guardrail "
        f"{result['guardrail_false_alarms']}, read_proposal {result['read_proposal_false_alarms']}. "
        f"Personal data: identifiers in {result['with_identifiers']} cases, contact details in "
        f"{result['with_contact_details']}. ${result['cost_usd']:.4f}.",
        "",
        "| Case | Injection | Workflow flagged | Guardrail | Where | Identifiers | Contact details |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        lines.append(
            f"| {r['case']} | {'yes' if r['injection_expected'] else '-'} | {r['read_proposal_rate']:.0%} | "
            f"{'yes' if r['guardrail'] else '-'} | {', '.join(r['guardrail_fields']) or '-'} | "
            f"{', '.join(r['identifiers']) or '-'} | {', '.join(r['contact_details']) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="The guardrail: report, --create, or --compare (paid).")
    parser.add_argument("--create", action="store_true", help="create it and publish the pinned version")
    parser.add_argument("--compare", action="store_true", help="screen the dev packets and attacks (paid)")
    args = parser.parse_args()
    control = control_client()
    try:
        if args.create:
            create(control)
        guardrail_id = report(control)
        if args.compare and guardrail_id:
            result = compare(bedrock.make_client(), guardrail_id)
            out = paths.ROOT / "results" / f"guardrail-dev-v{VERSION}.json"
            out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            out.with_suffix(".md").write_text(comparison_report(result), encoding="utf-8")
            print()
            print(comparison_report(result))
    except ClientError as e:
        print(f"AWS error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
