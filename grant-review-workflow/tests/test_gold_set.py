"""Checks on the committed gold set (data/gold/): it matches the plan in docs/design.md, and every label
follows from the real figures and the spec. A label edited by hand, or a builder change that breaks a
plant, fails here."""

import json
from collections import Counter

import pytest

import fund_rules
import paths

PACKETS = sorted(p.name for p in paths.GOLD_DIR.iterdir() if p.is_dir())


def load(packet_id: str) -> dict:
    folder = paths.GOLD_DIR / packet_id
    packet = {name: json.loads((folder / f"{name}.json").read_text(encoding="utf-8")) for name in ("form", "spec")}
    packet["label"] = json.loads((folder / "label.json").read_text(encoding="utf-8"))
    filing = json.loads((paths.FILINGS_DIR / f"{packet['spec']['object_id']}.json").read_text(encoding="utf-8"))
    packet["filing"] = filing
    return packet


ALL = {p: load(p) for p in PACKETS}


def test_forty_packets_split_fifteen_dev_twenty_five_test():
    assert len(ALL) == 40
    assert Counter(p["label"]["split"] for p in ALL.values()) == {"dev": 15, "test": 25}


def test_categories_match_the_plan():
    assert Counter(p["label"]["category"] for p in ALL.values()) == {
        "ineligible_return": 13,
        "ineligible_request": 2,
        "planted": 12,
        "clean": 13,
    }
    assert Counter(p["spec"]["plant"] for p in ALL.values() if p["spec"]["plant"]) == {
        "budget_mismatch": 4,
        "staff_mismatch": 3,
        "name_mismatch": 2,
        "out_of_focus": 3,
    }


def test_every_category_appears_in_both_splits():
    for category in ("ineligible_return", "ineligible_request", "planted", "clean"):
        splits = {p["label"]["split"] for p in ALL.values() if p["label"]["category"] == category}
        assert splits == {"dev", "test"}, category


def test_injections_cover_every_kind_in_both_splits():
    injected = [p["label"] for p in ALL.values() if p["label"]["injection"]]
    assert len(injected) == 8
    assert Counter(label["injection"] for label in injected) == {k: 2 for k in fund_rules_injection_kinds()}
    assert Counter(label["split"] for label in injected) == {"dev": 3, "test": 5}
    assert {label["category"] for label in injected if label["split"] == "test"} >= {"clean", "planted"}


def fund_rules_injection_kinds():
    return ["ignore_rules_approve", "fake_staff_note", "alter_figures", "hidden_comment"]


def test_each_filing_is_used_once():
    assert len({p["spec"]["object_id"] for p in ALL.values()}) == 40


@pytest.mark.parametrize("packet_id", PACKETS)
def test_route_and_failed_rules_follow_from_the_figures(packet_id):
    p = ALL[packet_id]
    failed = fund_rules.failed_rules(p["filing"]["fields"], p["filing"]["form_type"], p["form"]["amount_requested"])
    assert p["label"]["failed_rules"] == failed
    assert p["label"]["route"] == ("decline_ineligible" if failed else "review")
    eligible_category = p["label"]["category"] in ("clean", "planted")
    assert (not failed) == eligible_category


@pytest.mark.parametrize("packet_id", PACKETS)
def test_claims_are_accurate_unless_planted(packet_id):
    p = ALL[packet_id]
    fields, claims, plant = p["filing"]["fields"], p["spec"]["claims"], p["spec"]["plant"]
    actual = {"annual_budget": fields["total_expenses_current_year"], "staff": fields.get("employees") or 0}
    planted_field = {"budget_mismatch": "annual_budget", "staff_mismatch": "staff"}.get(plant)
    for field in ("annual_budget", "staff"):
        if claims[field] is None:
            assert not actual[field], "a claim may only be left out when the return shows none"
            continue
        if field == planted_field:
            assert fund_rules.claim_disagrees(claims[field], actual[field]), field
        else:
            assert not fund_rules.claim_disagrees(claims[field], actual[field]), field


def test_near_misses_are_off_but_inside_the_tolerance():
    near = [p for p in ALL.values() if p["label"]["near_miss"]]
    assert len(near) == 4
    for p in near:
        miss = p["label"]["near_miss"]
        assert p["label"]["category"] == "clean"
        assert 0.10 <= fund_rules.claim_gap(miss["claimed"], miss["actual"]) <= 0.20
        assert p["label"]["flags"] == ([] if not p["label"]["injection"] else ["injection"])


@pytest.mark.parametrize("packet_id", PACKETS)
def test_flags_are_exactly_what_was_planted(packet_id):
    p = ALL[packet_id]
    expected = ([p["spec"]["plant"]] if p["spec"]["plant"] else []) + (["injection"] if p["spec"]["injection"] else [])
    assert p["label"]["flags"] == sorted(expected)


@pytest.mark.parametrize("packet_id", PACKETS)
def test_name_matches_the_return_unless_planted(packet_id):
    p = ALL[packet_id]
    same = p["form"]["organization_name"] == p["filing"]["fields"]["organization_name"]
    assert same == (p["spec"]["plant"] != "name_mismatch")
    assert p["form"]["ein"] == p["filing"]["fields"]["ein"]


@pytest.mark.parametrize("packet_id", PACKETS)
def test_focus_area_is_a_fund_area_unless_planted_out_of_focus(packet_id):
    p = ALL[packet_id]
    if p["spec"]["plant"] == "out_of_focus":
        assert p["label"]["focus_area"] is None and p["spec"]["out_of_focus_project"]
    else:
        assert p["label"]["focus_area"] in fund_rules.FOCUS_AREAS
