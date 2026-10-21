"""Checks on the held-out packets (gp-041 onward, split "holdout", step 6.5): the builder reproduces them from
files alone, every label follows from the figures and the plan, and each case is what it says it is."""

import json
from collections import Counter

import pytest

import build_holdout
import fund_rules
import narratives
import paths
from research import normalize_name

PACKETS = sorted(
    p.name
    for p in paths.GOLD_DIR.iterdir()
    if p.is_dir() and json.loads((p / "label.json").read_text(encoding="utf-8"))["split"] == "holdout"
)


def load(packet_id: str) -> dict:
    folder = paths.GOLD_DIR / packet_id
    packet = {
        name: json.loads((folder / f"{name}.json").read_text(encoding="utf-8")) for name in ("form", "spec", "label")
    }
    packet["fields"] = json.loads(
        (paths.FILINGS_DIR / f"{packet['spec']['object_id']}.json").read_text(encoding="utf-8")
    )["fields"]
    return packet


ALL = {p: load(p) for p in PACKETS}


def test_one_packet_for_each_filing_the_gold_set_left_unused():
    assert PACKETS == [f"gp-{n:03d}" for n in range(41, 61)]
    assert sorted(p["spec"]["object_id"] for p in ALL.values()) == [
        f["object_id"] for f in build_holdout.unused_filings()
    ]


def test_the_builder_reproduces_every_packet_from_files_alone():
    """ProPublica's names are read back from the specs, so no network is needed and no label is hand-edited."""
    registered = {p["spec"]["object_id"]: p["spec"]["registered_name"] for p in ALL.values()}
    for built in build_holdout.build(registered):
        packet = ALL[built["label"]["packet_id"]]
        assert built["spec"] == packet["spec"] and built["label"] == packet["label"]
        title = packet["form"]["project_title"]
        assert {**built["form"], "project_title": title} == packet["form"]


def test_the_cases_match_the_plan():
    cases = Counter(p["spec"]["holdout_case"] for p in ALL.values())
    natural = sum(p["spec"]["registered_name_differs"] for p in ALL.values())
    assert natural == cases["natural_name_difference"] + cases["natural_name_difference+name_mismatch"] == 5
    assert cases["natural_name_difference+name_mismatch"] == 1
    assert {k: cases[k] for k in build_holdout.MATCHING_PLAN} == build_holdout.MATCHING_PLAN
    assert cases["clean"] == 20 - 5 - sum(build_holdout.MATCHING_PLAN.values())


@pytest.mark.parametrize("packet_id", PACKETS)
def test_route_and_failed_rules_follow_from_the_figures(packet_id):
    p = ALL[packet_id]
    failed = fund_rules.failed_rules(p["fields"], "990", p["form"]["amount_requested"])
    assert p["label"]["failed_rules"] == failed and p["label"]["route"] == (
        "decline_ineligible" if failed else "review"
    )
    assert bool(failed) == (p["spec"]["holdout_case"] == "ineligible_request")


@pytest.mark.parametrize("packet_id", PACKETS)
def test_flags_are_exactly_what_was_planted(packet_id):
    p = ALL[packet_id]
    injected = p["spec"]["injection"] or p["spec"]["form_injection"]
    expected = ([p["spec"]["plant"]] if p["spec"]["plant"] else []) + (["injection"] if injected else [])
    assert p["label"]["flags"] == sorted(expected)


@pytest.mark.parametrize("packet_id", PACKETS)
def test_names_are_what_each_case_says(packet_id):
    p = ALL[packet_id]
    form, own, registered = (
        p["form"]["organization_name"],
        p["fields"]["organization_name"],
        p["spec"]["registered_name"],
    )
    case = p["spec"]["holdout_case"]
    if case.endswith("name_mismatch"):
        assert normalize_name(form) != normalize_name(own)  # another organization's name
    elif case == "name_injection":
        assert form == own + p["spec"]["form_injection"]["text"]
    else:
        assert form == own  # the form matches the applicant's own return...
    # ...and for the natural differences, only the public record's name differs: no name_mismatch is expected.
    assert p["spec"]["registered_name_differs"] == case.startswith("natural_name_difference")
    if case == "natural_name_difference":
        assert normalize_name(registered) != normalize_name(own) and "name_mismatch" not in p["label"]["flags"]


def test_form_field_injections_are_worded_unlike_the_red_teams():
    import red_team

    red = {a.text.strip(" -()") for a in red_team.attacks() if a.surface in ("org_name", "project_title")}
    ours = [p["spec"]["form_injection"]["text"] for p in ALL.values() if p["spec"]["form_injection"]]
    assert len(ours) == 4 and not any(r in o for r in red for o in ours)


def test_the_near_miss_is_off_but_inside_the_tolerance():
    [miss] = [p["label"]["near_miss"] for p in ALL.values() if p["label"]["near_miss"]]
    assert 0.10 <= fund_rules.claim_gap(miss["claimed"], miss["actual"]) <= 0.20


@pytest.mark.parametrize("packet_id", [p for p in PACKETS if p in narratives.packets_with_narratives()])
def test_a_title_injection_rides_on_the_narratives_title(packet_id):
    p = ALL[packet_id]
    meta = json.loads((paths.GOLD_DIR / packet_id / "narrative.json").read_text(encoding="utf-8"))
    injection = p["spec"]["form_injection"]
    suffix = injection["text"] if injection and injection["field"] == "project_title" else ""
    assert p["form"]["project_title"] == meta["project_title"] + suffix, "rerun src/build_holdout.py"
