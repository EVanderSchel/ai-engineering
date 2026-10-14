"""Build the dataset: nonprofit mission statements labeled with their NTEE major group.

    python src/build_dataset.py                                   # default batch, 12,000 examples
    python src/build_dataset.py --batch 2025_TEOS_XML_06A --batch 2025_TEOS_XML_03A

Three stages, each cached in data/raw/ (gitignored) so a rerun skips finished work:

1. Labels: stream the IRS Exempt Organizations Business Master File (four regional CSVs, about 1.9 million
   organizations) and keep only EIN -> NTEE code, in data/raw/bmf_ntee.csv.
2. Missions: download a monthly e-file XML batch (a ZIP of about 45,000 returns), take each Form 990 or
   990-EZ's mission statement, keep them in data/raw/missions_<batch>.csv, and delete the ZIP. One large,
   polite download per batch instead of tens of thousands of small requests.
3. Build: join on EIN, clean, deduplicate, sample and split, writing data/dataset.csv and data/stats.json
   (both committed: the dataset is a few MB and is versioned with the code that made it).

Design (approved by the user before building):
- Text: Form 990 Part I line 1 (ActivityOrMissionDesc), falling back to Part III (MissionDesc); Form 990-EZ
  PrimaryExemptPurposeTxt. Private foundations (990-PF) have no mission field and are left out.
- Label: the NTEE major group (first letter, ntee.GROUPS); group Z ("Unknown, Unclassified") is excluded.
- Cleaning: whitespace normalized; missions under 15 characters and placeholders ("N/A", "SEE SCHEDULE O")
  dropped.
- Deduplication: one record per organization (its latest return); identical mission texts (ignoring case
  and punctuation) collapsed to one, and dropped entirely when they carry different labels, so near-copies
  can't sit in both train and test.
- Sampling: about 12,000 examples in the natural distribution, topped up to at least 100 per group where the
  data has them.
- Split: 70% train / 15% dev / 15% test, stratified by group, fixed seed. The test split is held out.
"""

import argparse
import csv
import html
import json
import random
import re
import statistics
import zipfile
from collections import Counter, defaultdict

import requests

import ntee
from paths import DATA_DIR, RAW_DIR

HEADERS = {"User-Agent": "ntee-classification (learning project; github.com/EVanderSchel/ai-engineering)"}
BMF_URL = "https://www.irs.gov/pub/irs-soi/{region}.csv"
BMF_REGIONS = ("eo1", "eo2", "eo3", "eo4")
BATCH_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch}.zip"
DEFAULT_BATCHES = ("2025_TEOS_XML_06A",)
DATASET_PATH = DATA_DIR / "dataset.csv"
STATS_PATH = DATA_DIR / "stats.json"

MIN_CHARS = 15
SIZE, MIN_PER_GROUP, SEED = 12_000, 100, 2026
SPLITS = {"train": 0.70, "dev": 0.15, "test": 0.15}
FIELDS = ["ein", "form", "tax_period", "mission", "ntee_code", "group", "broad", "split"]


# --- Stage 1: labels ---------------------------------------------------------------------------------


def download_labels() -> dict[str, str]:
    """EIN -> NTEE code for every organization in the master file that has one."""
    path = RAW_DIR / "bmf_ntee.csv"
    if not path.exists():
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        rows = []
        for region in BMF_REGIONS:
            print(f"  streaming master file {region}.csv")
            with requests.get(BMF_URL.format(region=region), headers=HEADERS, stream=True, timeout=120) as r:
                r.raise_for_status()
                lines = (line.decode("latin-1") for line in r.iter_lines())
                rows += [
                    (row["EIN"], row["NTEE_CD"].strip()) for row in csv.DictReader(lines) if row["NTEE_CD"].strip()
                ]
        partial = path.with_suffix(".partial")
        with partial.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows([("ein", "ntee_code"), *rows])
        partial.replace(path)
    with path.open(encoding="utf-8", newline="") as f:
        return {row["ein"]: row["ntee_code"] for row in csv.DictReader(f)}


# --- Stage 2: missions -------------------------------------------------------------------------------

_TAGS = {
    "990": ("ActivityOrMissionDesc", "MissionDesc"),
    "990EZ": ("PrimaryExemptPurposeTxt",),
}


def _tag(xml: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return html.unescape(match.group(1)) if match else None


def parse_return(xml: str) -> dict | None:
    """Form type, filer EIN, tax period end and mission of one e-filed return, or None when it isn't a Form
    990 or 990-EZ, or has no mission field."""
    form = _tag(xml, "ReturnTypeCd")
    if form not in _TAGS:
        return None
    filer = re.search(r"<Filer>.*?<EIN>(\d{9})</EIN>", xml, re.S)  # the filer's, not a preparer's
    mission = next((m for tag in _TAGS[form] if (m := _tag(xml, tag))), None)
    if not filer or not mission:
        return None
    return {"ein": filer.group(1), "form": form, "tax_period": _tag(xml, "TaxPeriodEndDt") or "", "mission": mission}


def download_missions(batch: str) -> list[dict]:
    path = RAW_DIR / f"missions_{batch}.csv"
    if not path.exists():
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = RAW_DIR / f"{batch}.zip"
        print(f"  downloading {batch}.zip")
        with requests.get(
            BATCH_URL.format(year=batch[:4], batch=batch), headers=HEADERS, stream=True, timeout=120
        ) as r:
            r.raise_for_status()
            with zip_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        records = []
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if name.endswith(".xml") and (record := parse_return(z.read(name).decode("utf-8", "replace"))):
                    records.append(record)
        zip_path.unlink()  # keep only what's needed: the ZIP is hundreds of MB
        partial = path.with_suffix(".partial")
        with partial.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["ein", "form", "tax_period", "mission"])
            writer.writeheader()
            writer.writerows(records)
        partial.replace(path)
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# --- Stage 3: build ----------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(
    r"^(n/?a|none|see schedule o|see sch\.? o|see attached|see attachment|see statement|see part iii|"
    r"refer to schedule o|same as above)\b",
    re.IGNORECASE,
)


def clean_mission(text: str) -> str | None:
    """The mission with whitespace normalized, or None if it's too short or a placeholder."""
    text = " ".join((text or "").split())
    if len(text) < MIN_CHARS or _PLACEHOLDER.match(text):
        return None
    if "schedule o" in text.lower() and len(text) < 40:  # "SEE SCHEDULE O FOR MISSION" and the like
        return None
    return text


def text_key(mission: str) -> str:
    """Mission text ignoring case, punctuation and spacing, to find duplicates."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", mission.lower()).split())


def deduplicate(records: list[dict], counts: Counter) -> list[dict]:
    """One record per organization (latest tax period, then longest mission); then one per distinct mission
    text, dropping every copy of a text whose copies carry different groups."""
    latest: dict[str, dict] = {}
    for r in records:
        best = latest.get(r["ein"])
        if best is None or (r["tax_period"], len(r["mission"])) > (best["tax_period"], len(best["mission"])):
            latest[r["ein"]] = r
    counts["one_per_organization"] = len(latest)
    by_text: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(latest.values(), key=lambda r: r["ein"]):
        by_text[text_key(r["mission"])].append(r)
    kept = []
    for copies in by_text.values():
        if len({r["group"] for r in copies}) > 1:
            counts["dropped_conflicting_duplicates"] += len(copies)
        else:
            kept.append(copies[0])
            counts["dropped_identical_duplicates"] += len(copies) - 1
    return kept


def sample(records: list[dict], size: int, min_per_group: int, rng: random.Random) -> list[dict]:
    """About `size` records in the natural group distribution, topped up to min_per_group where possible."""
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_group[r["group"]].append(r)
    total, chosen = len(records), []
    for _group, pool in sorted(by_group.items()):
        pool = sorted(pool, key=lambda r: r["ein"])
        rng.shuffle(pool)
        want = max(round(len(pool) / total * size), min(min_per_group, len(pool)))
        chosen += pool[: min(want, len(pool))]
    return chosen


def split(records: list[dict], rng: random.Random) -> list[dict]:
    """Assign train/dev/test within each group (stratified), so every split has the same group mix."""
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_group[r["group"]].append(r)
    out = []
    for _group, rows in sorted(by_group.items()):
        rows = sorted(rows, key=lambda r: r["ein"])
        rng.shuffle(rows)
        n_test, n_dev = round(len(rows) * SPLITS["test"]), round(len(rows) * SPLITS["dev"])
        for i, r in enumerate(rows):
            out.append(r | {"split": "test" if i < n_test else "dev" if i < n_test + n_dev else "train"})
    return sorted(out, key=lambda r: (r["split"], r["group"], r["ein"]))


def build(labels: dict[str, str], missions: list[dict], size: int = SIZE, seed: int = SEED) -> tuple[list, dict]:
    counts: Counter = Counter(returns_with_mission=len(missions))
    joined = []
    for m in missions:
        code = labels.get(m["ein"])
        if code is None:
            counts["dropped_no_ntee_code"] += 1
            continue
        if code.strip().upper().startswith(ntee.UNKNOWN_GROUP):
            counts["dropped_unknown_group_z"] += 1
            continue
        group = ntee.major_group(code)
        if group is None:
            counts["dropped_malformed_code"] += 1
            continue
        mission = clean_mission(m["mission"])
        if mission is None:
            counts["dropped_short_or_placeholder"] += 1
            continue
        joined.append(m | {"mission": mission, "ntee_code": code.strip().upper(), "group": group})
    counts["labeled_and_clean"] = len(joined)
    unique = deduplicate(joined, counts)
    counts["unique"] = len(unique)
    rng = random.Random(seed)
    rows = [
        r | {"broad": ntee.broad_category(r["group"])} for r in split(sample(unique, size, MIN_PER_GROUP, rng), rng)
    ]
    stats = {
        "funnel": dict(counts),
        "rows": len(rows),
        "per_split": dict(Counter(r["split"] for r in rows)),
        "per_group": {g: dict(Counter(r["split"] for r in rows if r["group"] == g)) for g in ntee.GROUPS},
        "available_per_group": dict(sorted(Counter(r["group"] for r in unique).items())),
        "groups_under_min": sorted(g for g in ntee.GROUPS if sum(r["group"] == g for r in rows) < MIN_PER_GROUP),
        "mission_chars": {
            "median": statistics.median(len(r["mission"]) for r in rows),
            "p10": sorted(len(r["mission"]) for r in rows)[len(rows) // 10],
            "p90": sorted(len(r["mission"]) for r in rows)[len(rows) * 9 // 10],
        },
        "forms": dict(Counter(r["form"] for r in rows)),
    }
    return rows, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", action="append", help=f"IRS XML batch(es) (default {', '.join(DEFAULT_BATCHES)})")
    parser.add_argument("--size", type=int, default=SIZE)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    print("Labels:")
    labels = download_labels()
    print(f"  {len(labels):,} organizations with an NTEE code")
    missions = []
    for batch in args.batch or DEFAULT_BATCHES:
        print(f"Missions from {batch}:")
        missions += download_missions(batch)
    print(f"  {len(missions):,} Form 990/990-EZ returns with a mission")

    rows, stats = build(labels, missions, args.size, args.seed)
    with DATASET_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({k: r[k] for k in FIELDS} for r in rows)
    STATS_PATH.write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(json.dumps(stats["funnel"], indent=1))
    print(
        f"wrote {len(rows):,} rows to {DATASET_PATH} ({stats['per_split']}); groups under {MIN_PER_GROUP}: "
        f"{stats['groups_under_min'] or 'none'}"
    )


if __name__ == "__main__":
    main()
