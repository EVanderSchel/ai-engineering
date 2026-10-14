"""The dataset builder's rules, on small synthetic inputs (no network)."""

import random
from collections import Counter

import pytest

import build_dataset as bd
import ntee


def xml_return(form: str, ein: str = "421177880", mission_tag: str | None = None, mission: str = "", extra: str = ""):
    tag = mission_tag or {"990": "ActivityOrMissionDesc", "990EZ": "PrimaryExemptPurposeTxt"}.get(form, "X")
    return (
        f"<Return><ReturnHeader><ReturnTypeCd>{form}</ReturnTypeCd><TaxPeriodEndDt>2024-06-30</TaxPeriodEndDt>"
        f"<Filer><EIN>{ein}</EIN></Filer>{extra}</ReturnHeader><ReturnData><{tag}>{mission}</{tag}></ReturnData></Return>"
    )


# --- Labels --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "group"), [("B25", "B"), ("x20 ", "X"), ("Z99", None), ("", None), ("123", None)])
def test_major_group(code, group):
    assert ntee.major_group(code) == group


def test_every_group_rolls_up_to_exactly_one_broad_category():
    assert {g for _, (_, groups) in ntee.BROAD.items() for g in groups} == set(ntee.GROUPS)
    assert ntee.broad_category("P") == "V" and ntee.broad_category("X") == "VIII"


# --- Parsing returns ------------------------------------------------------------------------------------


def test_form_990_mission_and_the_filers_ein():
    preparer = "<PreparerFirmGrp><PreparerFirmEIN>999999999</PreparerFirmEIN></PreparerFirmGrp>"
    record = bd.parse_return(xml_return("990", mission="Feed hungry families &amp; seniors", extra=preparer))
    assert record == {
        "ein": "421177880",
        "form": "990",
        "tax_period": "2024-06-30",
        "mission": "Feed hungry families & seniors",
    }


def test_form_990_falls_back_to_the_part_iii_mission():
    assert (
        bd.parse_return(xml_return("990", mission_tag="MissionDesc", mission="Run a museum"))["mission"]
        == "Run a museum"
    )


def test_form_990ez_purpose():
    assert bd.parse_return(xml_return("990EZ", mission="Youth soccer league"))["form"] == "990EZ"


@pytest.mark.parametrize("xml", [xml_return("990PF", mission="Grants"), xml_return("990", mission="")])
def test_foundations_and_missing_missions_are_skipped(xml):
    assert bd.parse_return(xml) is None


# --- Cleaning -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("  Provide   ambulance and fire\nprotection services  ", "Provide ambulance and fire protection services"),
        ("EDUCATION HUNTING WITH BOW", "EDUCATION HUNTING WITH BOW"),
        ("Youth soccer", None),  # under 15 characters
        ("N/A - see the attached statement", None),
        ("SEE SCHEDULE O FOR DETAILS", None),
        ("See Part III and Schedule O", None),
        ("None of the above applies here", None),
    ],
)
def test_clean_mission(text, kept):
    assert bd.clean_mission(text) == kept


def test_text_key_ignores_case_punctuation_and_spacing():
    assert bd.text_key("Feed the Hungry!") == bd.text_key("  feed THE hungry ")


# --- Deduplication, sampling, splitting -----------------------------------------------------------------


def row(ein, mission, group, tax_period="2024-12-31"):
    return {
        "ein": ein,
        "form": "990",
        "tax_period": tax_period,
        "mission": mission,
        "ntee_code": group + "20",
        "group": group,
    }


def test_one_record_per_organization_keeps_the_latest_return():
    counts = Counter()
    kept = bd.deduplicate(
        [row("1", "old mission text here", "B", "2023-12-31"), row("1", "new mission text", "B")], counts
    )
    assert [r["mission"] for r in kept] == ["new mission text"]


def test_identical_texts_collapse_and_conflicting_ones_are_dropped():
    counts = Counter()
    records = [
        row("1", "Support local schools", "B"),
        row("2", "support local schools.", "B"),  # same text, same group: keep one
        row("3", "Help people in need", "P"),
        row("4", "HELP PEOPLE IN NEED", "X"),  # same text, different groups: drop both
    ]
    kept = bd.deduplicate(records, counts)
    assert [r["ein"] for r in kept] == ["1"]
    assert counts["dropped_identical_duplicates"] == 1 and counts["dropped_conflicting_duplicates"] == 2


def pool(sizes: dict[str, int]):
    return [row(f"{g}{i:05d}", f"mission {g} {i}", g) for g, n in sizes.items() for i in range(n)]


def test_sampling_keeps_the_natural_mix_and_tops_up_small_groups():
    chosen = bd.sample(pool({"B": 9000, "P": 2850, "Q": 150}), size=1200, min_per_group=100, rng=random.Random(1))
    counts = Counter(r["group"] for r in chosen)
    assert counts["B"] == 900 and counts["P"] == 285  # proportional
    assert counts["Q"] == 100  # proportional would be 15


def test_a_group_with_fewer_than_the_minimum_keeps_all_it_has():
    chosen = bd.sample(pool({"B": 1000, "V": 40}), size=500, min_per_group=100, rng=random.Random(1))
    assert Counter(r["group"] for r in chosen)["V"] == 40


def test_split_is_stratified_and_reproducible():
    records = pool({"B": 700, "P": 300})
    first, second = bd.split(records, random.Random(7)), bd.split(records, random.Random(7))
    assert first == second
    counts = Counter((r["group"], r["split"]) for r in first)
    assert (counts["B", "train"], counts["B", "dev"], counts["B", "test"]) == (490, 105, 105)
    assert (counts["P", "train"], counts["P", "dev"], counts["P", "test"]) == (210, 45, 45)


def test_build_end_to_end_funnel():
    labels = {"000000001": "B25", "000000002": "Z99", "000000003": "P20"}
    missions = [
        {"ein": "000000001", "form": "990", "tax_period": "2024-12-31", "mission": "Operate a charter school"},
        {"ein": "000000002", "form": "990", "tax_period": "2024-12-31", "mission": "Unclassified organization"},
        {"ein": "000000003", "form": "990", "tax_period": "2024-12-31", "mission": "N/A"},
        {"ein": "000000004", "form": "990", "tax_period": "2024-12-31", "mission": "No label in the master file"},
    ]
    rows, stats = bd.build(labels, missions, size=10)
    assert [(r["ein"], r["group"], r["broad"]) for r in rows] == [("000000001", "B", "II")]
    funnel = stats["funnel"]
    assert (
        funnel["dropped_no_ntee_code"],
        funnel["dropped_unknown_group_z"],
        funnel["dropped_short_or_placeholder"],
    ) == (1, 1, 1)
