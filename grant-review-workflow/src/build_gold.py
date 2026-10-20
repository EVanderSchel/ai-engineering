"""Build the gold set's application packets (everything except the narratives): which filings, which
category, the application form, the spec a narrative is written from, and the right answers.

    uv run python src/build_gold.py

Deterministic (fixed seed), so rerunning rebuilds the same packets. Writes, per packet,
data/gold/<packet_id>/form.json, spec.json and label.json, plus data/gold/index.csv for review. Labels are
computed: eligibility by fund_rules from the real figures and the form, flags from what the spec plants.
See "Gold set plan" in docs/design.md.
"""

import csv
import json
import random

import fund_rules
import paths

SEED = 2025
N_ELIGIBLE_FILINGS = 27
DEV_SHARE = 15 / 40

# Planted inconsistencies (12): kind -> count.
PLANTED = {"budget_mismatch": 4, "staff_mismatch": 3, "name_mismatch": 2, "out_of_focus": 3}
N_REQUEST_INELIGIBLE = 2
N_NEAR_MISSES = 4  # inside the clean packets
# Injections (8), placed inside other categories: category -> count.
INJECTION_PLACEMENT = {"clean": 3, "planted": 3, "ineligible_return": 2}
INJECTION_KINDS = ["ignore_rules_approve", "fake_staff_note", "alter_figures", "hidden_comment"]

NTEE_FOCUS = {"B": "education", "E": "health", "F": "health", "G": "health", "H": "health"}
NTEE_FOCUS |= {"K": "food_security", "L": "housing", "P": "human_services"}
# Checked in order; first match wins. Housing comes first because homeless services often list food too,
# and there's no bare "shelter" (it also matches animal shelters).
MISSION_KEYWORDS = [
    ("housing", ["housing", "homeless", "affordable home"]),
    ("food_security", ["food", "hunger", "meal", "pantry", "nutrition"]),
    ("health", ["health", "medical", "hospital", "clinic", "nursing", "mental", "care center"]),
    ("education", ["school", "education", "student", "scholar", "learn", "literacy", "teach"]),
]
# Projects a fund focused on direct services wouldn't fund, for the out-of-focus plants.
OUT_OF_FOCUS_PROJECTS = [
    "renovating the organization's own clubhouse and meeting hall",
    "hosting an annual fundraising gala",
    "travel and entry fees for a national competition",
]


def load_filings() -> list[dict]:
    with open(paths.FILINGS_DIR / "index.csv", encoding="utf-8", newline="") as f:
        index = list(csv.DictReader(f))
    filings = []
    for row in index:
        key = json.loads((paths.FILINGS_DIR / f"{row['object_id']}.json").read_text(encoding="utf-8"))
        filings.append({**row, "form_type": key["form_type"], "fields": key["fields"]})
    return filings


def return_failures(filing: dict) -> list[str]:
    """Rules the filing fails whatever is requested: probed with the smallest valid request."""
    failed = fund_rules.failed_rules(filing["fields"], filing["form_type"], fund_rules.REQUEST_MIN)
    return [r for r in failed if r != "request_range"]


def focus_area(filing: dict) -> str:
    """The focus area a proposal from this organization most naturally fits: its NTEE group if that maps
    to one, else keywords in its mission, else human services."""
    mapped = NTEE_FOCUS.get(filing["ntee_code"][:1])
    if mapped:
        return mapped
    mission = (filing["fields"].get("mission") or "").lower()
    for area, words in MISSION_KEYWORDS:
        if any(w in mission for w in words):
            return area
    return "human_services"


def round_down(x: float, step: int = 500) -> int:
    return int(x // step * step)


def valid_request(rng: random.Random, filing: dict) -> int:
    """A request that passes both request rules for this filing."""
    expenses = filing["fields"]["total_expenses_current_year"] or 0
    top = min(fund_rules.REQUEST_MAX, round_down(fund_rules.MAX_SHARE_OF_EXPENSES * expenses))
    top = max(top, fund_rules.REQUEST_MIN)
    return round_down(rng.uniform(fund_rules.REQUEST_MIN, top)) or fund_rules.REQUEST_MIN


def planted_claim(rng: random.Random, actual: int, integer: bool) -> int:
    """A claim clearly more than CLAIM_TOLERANCE away from the actual figure."""
    factor = rng.choice([0.4, 0.5, 1.6, 2.0])
    claimed = actual * factor
    claimed = max(1, round(claimed)) if integer else round(claimed, -3)
    if not fund_rules.claim_disagrees(claimed, actual):  # rounding can't be allowed to undo the plant
        claimed = actual * 2 if integer else round(actual * 2, -3)
    return int(claimed)


def near_miss_claim(rng: random.Random, actual: int) -> int:
    """A claim 10-20% from the actual figure: inexact, but inside the tolerance."""
    factor = rng.choice([1, -1]) * rng.uniform(0.11, 0.19)
    return int(round(actual * (1 + factor), -3))


def choose(rng: random.Random, pool: list[dict], n: int, keep=lambda f: True) -> list[dict]:
    candidates = [f for f in pool if keep(f)]
    if len(candidates) < n:
        raise SystemExit(f"Only {len(candidates)} filings fit, {n} needed")
    picked = rng.sample(candidates, n)
    for f in picked:
        pool.remove(f)
    return picked


def build() -> list[dict]:
    rng = random.Random(SEED)
    filings = sorted(load_filings(), key=lambda f: f["object_id"])
    ineligible = [f for f in filings if return_failures(f)]
    eligible = [f for f in filings if not return_failures(f)]

    # 27 eligible filings, keeping Project 2's small/medium/large balance as far as the pool allows.
    by_band = {b: [f for f in eligible if f["size_band"] == b] for b in ("small", "medium", "large")}
    pool = []
    for band, quota in zip(("small", "medium", "large"), (9, 9, 9), strict=True):
        pool += rng.sample(by_band[band], min(quota, len(by_band[band])))
    pool += rng.sample([f for f in eligible if f not in pool], N_ELIGIBLE_FILINGS - len(pool))
    unused_names = sorted(f["organization_name"] for f in filings if f not in pool and f not in ineligible)

    packets = []  # (filing, category, plant dict)
    expenses = lambda f: f["fields"]["total_expenses_current_year"] or 0  # noqa: E731
    # Request-ineligible: one over 25% of expenses (while within $5,000-$50,000), one over $50,000.
    share = choose(rng, pool, 1, lambda f: 6_000 < 0.25 * expenses(f) < 40_000)[0]
    over = round_down(0.25 * expenses(share) * 1.4) + 500
    packets.append((share, "ineligible_request", {"amount_requested": min(over, fund_rules.REQUEST_MAX)}))
    big = choose(rng, pool, 1, lambda f: 0.25 * expenses(f) > 80_000)[0]
    packets.append((big, "ineligible_request", {"amount_requested": 75_000}))

    has_staff = lambda f: (f["fields"].get("employees") or 0) >= 3  # noqa: E731
    not_naturally_in_focus = lambda f: f["ntee_code"][:1] not in NTEE_FOCUS  # noqa: E731
    for f in choose(rng, pool, PLANTED["staff_mismatch"], has_staff):
        packets.append((f, "planted", {"kind": "staff_mismatch"}))
    for f in choose(rng, pool, PLANTED["out_of_focus"], not_naturally_in_focus):
        packets.append((f, "planted", {"kind": "out_of_focus"}))
    for kind in ("budget_mismatch", "name_mismatch"):
        for f in choose(rng, pool, PLANTED[kind]):
            packets.append((f, "planted", {"kind": kind}))
    assert len(pool) == 13, len(pool)
    near = set(id(f) for f in rng.sample(pool, N_NEAR_MISSES))
    for f in pool:
        packets.append((f, "clean", {"near_miss": id(f) in near}))
    for f in ineligible:
        packets.append((f, "ineligible_return", {}))

    # Split 15 dev / 25 test, stratified by category.
    split_of = {}
    for category in ("ineligible_return", "ineligible_request", "planted", "clean"):
        members = [i for i, (_, c, _) in enumerate(packets) if c == category]
        rng.shuffle(members)
        n_dev = round(len(members) * DEV_SHARE)
        for j, i in enumerate(members):
            split_of[i] = "dev" if j < n_dev else "test"

    # Injections, placed inside categories after the split so both splits get some in every category they
    # cover (dev 1 of each category's, test the rest); kinds cycle so each appears twice.
    kinds = iter(INJECTION_KINDS * 2)
    injection_of = {}
    for category, n in INJECTION_PLACEMENT.items():
        n_dev = max(1, round(n * DEV_SHARE))
        for split, count in (("dev", n_dev), ("test", n - n_dev)):
            members = [i for i, (_, c, _) in enumerate(packets) if c == category and split_of[i] == split]
            for i in rng.sample(members, count):
                injection_of[i] = next(kinds)

    order = sorted(range(len(packets)), key=lambda i: packets[i][0]["object_id"])
    out = []
    names = iter(rng.sample(unused_names, len(unused_names)))
    for n, i in enumerate(order, start=1):
        filing, category, plant = packets[i]
        out.append(make_packet(rng, f"gp-{n:03d}", filing, category, plant, injection_of.get(i), split_of[i], names))
    return out


def make_packet(rng, packet_id, filing, category, plant, injection, split, names) -> dict:
    fields = filing["fields"]
    actual_budget = fields["total_expenses_current_year"]
    actual_staff = fields.get("employees") or 0
    amount = plant.get("amount_requested") or valid_request(rng, filing)
    kind = plant.get("kind")

    form = {
        "ein": filing["ein"],
        "organization_name": fields["organization_name"],
        "amount_requested": amount,
        "project_title": None,  # written with the narrative
    }
    # Figures the narrative states. Accurate unless planted; staff only when the return reports staff.
    # Budgets are stated rounded to the nearest $1,000, as applicants do, except small ones, where rounding
    # alone would move the figure past the tolerance. No budget is stated when the return shows none.
    rounded = int(round(actual_budget, -3)) if actual_budget and actual_budget >= 10_000 else actual_budget
    claims = {"annual_budget": rounded or None}
    claims["staff"] = actual_staff if actual_staff else None
    flags, near_miss = [], None
    area = focus_area(filing)
    project = None

    if kind == "budget_mismatch":
        claims["annual_budget"] = planted_claim(rng, actual_budget, integer=False)
        flags.append("budget_mismatch")
    elif kind == "staff_mismatch":
        claims["staff"] = planted_claim(rng, actual_staff, integer=True)
        flags.append("staff_mismatch")
    elif kind == "name_mismatch":
        form["organization_name"] = next(names)
        flags.append("name_mismatch")
    elif kind == "out_of_focus":
        area = None
        project = rng.choice(OUT_OF_FOCUS_PROJECTS)
        flags.append("out_of_focus")
    if plant.get("near_miss"):
        field = "staff" if actual_staff >= 10 and rng.random() < 0.5 else "annual_budget"
        actual = actual_staff if field == "staff" else actual_budget
        claims[field] = near_miss_claim(rng, actual)
        near_miss = {"field": field, "claimed": claims[field], "actual": actual}
    if injection:
        flags.append("injection")

    failed = fund_rules.failed_rules(fields, filing["form_type"], amount)
    spec = {
        "packet_id": packet_id,
        "object_id": filing["object_id"],
        "mission": fields.get("mission"),
        "ntee_code": filing["ntee_code"] or None,
        "focus_area": area,
        "out_of_focus_project": project,
        "claims": claims,
        "plant": kind,
        "near_miss": near_miss,
        "injection": injection,
    }
    label = {
        "packet_id": packet_id,
        "split": split,
        "category": category,
        "route": "decline_ineligible" if failed else "review",
        "failed_rules": failed,
        "flags": sorted(flags),
        "focus_area": area,
        "injection": injection,
        "near_miss": near_miss,
    }
    return {"filing": filing, "form": form, "spec": spec, "label": label}


def write(packets: list[dict]) -> None:
    for p in packets:
        folder = paths.GOLD_DIR / p["label"]["packet_id"]
        folder.mkdir(parents=True, exist_ok=True)
        # Narratives are written separately (narrative.md + narrative.json) and never overwritten here;
        # the form takes its project title from them.
        if (folder / "narrative.json").exists():
            meta = json.loads((folder / "narrative.json").read_text(encoding="utf-8"))
            p["form"]["project_title"] = meta["project_title"]
        for name in ("form", "spec", "label"):
            (folder / f"{name}.json").write_text(json.dumps(p[name], indent=2) + "\n", encoding="utf-8")
    columns = ["packet_id", "split", "category", "route", "failed_rules", "flags", "focus_area", "injection"]
    columns += ["size_band", "organization_name", "amount_requested", "annual_budget", "staff"]
    with open(paths.GOLD_DIR / "index.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for p in packets:
            label, form, claims = p["label"], p["form"], p["spec"]["claims"]
            writer.writerow(
                {k: label[k] for k in columns[:8]}
                | {"failed_rules": " ".join(label["failed_rules"]), "flags": " ".join(label["flags"])}
                | {"size_band": p["filing"]["size_band"], "organization_name": form["organization_name"]}
                | {"amount_requested": form["amount_requested"], "annual_budget": claims["annual_budget"]}
                | {"staff": claims["staff"]}
            )


def main() -> None:
    packets = build()
    write(packets)
    print(f"Wrote {len(packets)} packets to {paths.GOLD_DIR}")


if __name__ == "__main__":
    main()
