"""System prompts for prompted classification (step 3). Three variants, each adding to the one before:

- names:   the task and the 25 group names only (what a model knows from the category titles alone).
- guided:  plus a short description of each group and the NTEE conventions the step 1 label check and the
           step 2 baseline exposed: classify by primary activity, not motivation; support and fundraising
           organizations take the group of what they support, not T.
- fewshot: plus 4 labeled training missions per group (100 examples), chosen with a fixed seed.

The variant's text is the experiment, so it lives here in code, versioned with the results that used it.
"""

import random

import data
import ntee

TASK = """\
You classify US nonprofit organizations into NTEE (National Taxonomy of Exempt Entities) major groups from
the mission statement they filed on IRS Form 990 or 990-EZ. Missions can be terse, all capitals,
misspelled, or generic; infer the most likely group from the text. Give the three most likely groups, most
likely first, all different."""

DESCRIPTIONS = {
    "A": "museums, performing arts, historical societies and preservation, public radio and TV, arts councils, humanities",
    "B": "schools from preschool to university, libraries, parent-teacher groups, scholarships and student aid, adult education",
    "C": "conservation, natural resources, pollution and recycling, botanical gardens, horticulture, environmental education",
    "D": "animal protection and welfare, humane societies and shelters, wildlife preservation, zoos, veterinary services",
    "E": "hospitals, clinics, community and public health, nursing homes, home health, ambulance and emergency medical services",
    "F": "mental health treatment, substance abuse prevention and treatment, counseling, hotlines and crisis intervention",
    "G": "associations for a specific disease, disorder or disability (cancer, heart disease, autism...): support and awareness",
    "H": "research into specific diseases, disorders or medical disciplines",
    "I": "crime prevention, legal aid and services, courts, offender rehabilitation, police and law-enforcement support",
    "J": "job training, employment services, vocational rehabilitation, labor unions",
    "K": "food banks and pantries, nutrition programs, agriculture and farming organizations",
    "L": "housing construction and development, low-income and affordable housing, homeless shelters, home repair",
    "M": "fire departments and volunteer fire companies, disaster preparedness and relief, rescue, safety education",
    "N": "sports leagues and clubs, amateur athletics, camps, parks and playgrounds, recreational and hunting/fishing clubs, fairs",
    "O": "youth centers and clubs (scouting, Boys & Girls Clubs, 4-H), mentoring, youth leadership programs",
    "P": "multipurpose human services, children and family services, senior services, services for people with disabilities, "
    "emergency assistance, YMCAs, community and neighborhood centers",
    "Q": "international development and relief abroad, exchange programs, international peace and human rights, national security",
    "R": "civil rights, advocacy for specific populations, social action, civil liberties, voter education",
    "S": "community and economic development, chambers of commerce and business associations, neighborhood associations, "
    "service clubs (Rotary, Lions), nonprofit capacity building",
    "T": "private and community foundations that make grants, federated giving (United Way), donor funds, promoting volunteering",
    "U": "physical, earth and life sciences (non-medical), engineering, technology, science promotion",
    "V": "social science research and institutes (economics, political science, sociology), area and ethnic studies",
    "W": "public policy research, government and public administration, military and veterans organizations, transportation, "
    "consumer protection, leadership development",
    "X": "congregations and religious organizations, religious media, interfaith groups, missionary activities",
    "Y": "mutual and membership benefit: fraternal beneficiary societies, pension and retirement funds, cemeteries and burial",
}

CONVENTIONS = """\
NTEE conventions:
- Classify by the organization's primary activity, not its motivation or faith: a Christian school is B, a
  faith-based food pantry is K, a church-run hospital is E.
- An organization that raises funds for or supports one organization or cause takes that cause's group: a
  school foundation or athletic booster club is B, a hospital foundation is E, friends of a museum are A. T is
  only for grantmaking foundations, community foundations, federated giving and promoting volunteering.
- If a mission covers several activities, choose the main one and rank the others second and third."""

VARIANTS = ("names", "guided", "fewshot")
EXAMPLES_PER_GROUP, EXAMPLE_SEED = 4, 2026  # 4: crosses Haiku 4.5's 4,096-token caching minimum (3 left it at 3,639)


def _group_list(with_descriptions: bool) -> str:
    lines = []
    for letter, name in ntee.GROUPS.items():
        lines.append(f"{letter}: {name}" + (f" ({DESCRIPTIONS[letter]})" if with_descriptions else ""))
    return "NTEE major groups:\n" + "\n".join(lines)


def few_shot_examples(per_group: int = EXAMPLES_PER_GROUP, seed: int = EXAMPLE_SEED) -> list[dict]:
    """Labeled training missions, per_group for each group, of a readable length (40-250 characters).
    From the train split only, so none can appear in dev or test."""
    rng = random.Random(seed)
    by_group: dict[str, list[dict]] = {}
    for row in data.load("train"):
        if 40 <= len(row["mission"]) <= 250:
            by_group.setdefault(row["group"], []).append(row)
    chosen = []
    for group in ntee.GROUPS:
        pool = sorted(by_group.get(group, []), key=lambda r: r["ein"])
        chosen += rng.sample(pool, min(per_group, len(pool)))
    return chosen


def system_prompt(variant: str) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"unknown prompt variant {variant!r}; expected one of {VARIANTS}")
    parts = [TASK, _group_list(with_descriptions=variant != "names")]
    if variant in ("guided", "fewshot"):
        parts.append(CONVENTIONS)
    if variant == "fewshot":
        examples = "\n".join(f"- {r['mission']} -> {r['group']}" for r in few_shot_examples())
        parts.append("Examples of missions and their groups:\n" + examples)
    return "\n\n".join(parts)
