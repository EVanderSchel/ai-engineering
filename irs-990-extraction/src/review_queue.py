"""Step 6.4: a review queue. The fields a review rule flags, on a page a person can work through.

python src/review_queue.py data/runs/<run> --second data/runs/<another run of the same pages>
    writes data/review/<run>.html. At the top, a list of the filings to review with a progress count;
    then one section per filing, with page 1 on the left (click to zoom) and the fields to check on
    the right, each with its form line, the extracted value, the second run's value when they disagree,
    and Claude's doubt. Filings with a few flagged fields come first, whole-document reviews last.
    A field counts as reviewed only once the reviewer edits it or clicks "Looks right"; "Blank" means
    the line is empty on the form, which is different from 0. Entries are kept in the browser while
    they work. "Download corrections" saves the reviewed fields, and lists the ones nobody checked.

python src/review_queue.py --apply corrections.json
    writes data/runs/<run>_reviewed/: the same run with the reviewed values in place (unreviewed fields
    keep the extracted value and are listed as unreviewed), scored by evaluate.py like any other run,
    so the history shows the accuracy after review.

python src/review_queue.py data/runs/<run> --second ... --simulate
    writes corrections.json as a perfect reviewer would (from the answer keys), to test the plumbing.
"""

import argparse
import base64
import html
import io
import json
import pathlib
import shutil

from PIL import Image

import evaluate
import extract
import review
import scans
from fields import ALL_FIELDS
from paths import DATA_DIR, ROOT

REVIEW_DIR = DATA_DIR / "review"
DEFAULT_RULE = "checks + long thinking (> 2,000) + second run"
RULES = {rule.name: rule for rule in review.RULES}


# --- Building the queue ----------------------------------------------------------------------------


def queue(records: list[dict], rule: review.Rule, seconds: dict[str, dict] | None = None) -> list[dict]:
    """The filings with anything to review, each with its flagged fields: filings with a few flagged
    fields first (in run order), whole-document reviews last, so a long one can't hide the short ones."""
    items = []
    for record in records:
        second = (seconds or {}).get(record["object_id"])
        flags = review.flagged_fields(record, rule, second)
        if not flags:
            continue
        answer = record["answer"] or {}
        other = (second or {}).get("answer") or {}
        fields = []
        for f in ALL_FIELDS:
            if f.name not in flags:
                continue
            disagrees = second is not None and evaluate.normalize(f.name, answer.get(f.name)) != evaluate.normalize(
                f.name, other.get(f.name)
            )
            fields.append(
                {
                    "name": f.name,
                    "kind": f.kind,
                    "label": f"{f.description} ({f.line})",
                    "value": answer.get(f.name),
                    "second": other.get(f.name) if disagrees else None,
                    "disagrees": disagrees,
                    "doubted": f.name in (record.get("unsure") or []),
                }
            )
        whole = len(flags) == len(ALL_FIELDS)
        reason = _reason(record, rule) if whole else "some fields"
        items.append(
            {
                "object_id": record["object_id"],
                "scan": record.get("scan"),
                "reason": reason,
                "whole": whole,
                "fields": fields,
            }
        )
    return sorted(items, key=lambda item: item["whole"])  # stable: run order within each group


def _reason(record: dict, rule: review.Rule) -> str:
    if not record["answer"]:
        return "no answer: transcribe every field"
    if rule.checks and (record["problems"] or len(record["attempts"]) > 1):
        return "the form's arithmetic didn't add up: check every field"
    return f"a hard page (Claude wrote {record['usage']['output_tokens']:,} tokens): check every field"


def _page_jpeg(object_id: str, scan: str | None) -> str:
    """Page 1 as a base64 JPEG (smaller than PNG; it's for a person's eyes, not Claude's)."""
    png = extract.page_png(scans.source_pdf(object_id, scan), long_edge=1568)
    buffer = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buffer, "JPEG", quality=80)
    return base64.standard_b64encode(buffer.getvalue()).decode()


def _value_text(value) -> str:
    return "" if value is None else str(value)


def render(run: str, rule: review.Rule, items: list[dict], names: dict[str, str]) -> str:
    contents, sections = [], []
    for item in items:
        object_id, name = item["object_id"], html.escape(names.get(item["object_id"], "") or item["object_id"])
        rows = []
        for f in item["fields"]:
            notes = []
            if f["disagrees"]:
                notes.append(f"second run: {html.escape(_value_text(f['second'])) or 'blank'}")
            if f["doubted"]:
                notes.append("Claude was unsure")
            rows.append(
                f'<tr data-field="{f["name"]}" data-kind="{f["kind"]}">'
                f'<td class="label">{html.escape(f["label"])}</td>'
                f'<td><input type="text" value="{html.escape(_value_text(f["value"]))}" '
                f'aria-label="{html.escape(f["name"])}"{" disabled" if f["value"] is None else ""}>'
                f'<label class="blank"><input type="checkbox"{" checked" if f["value"] is None else ""}> Blank</label>'
                f'<div class="note">extracted: {html.escape(_value_text(f["value"])) or "blank"}'
                f"{' · ' + ' · '.join(notes) if notes else ''}</div></td>"
                f'<td><button type="button" class="check" aria-pressed="false">Looks right</button></td></tr>'
            )
        image = _page_jpeg(object_id, item["scan"])
        contents.append(
            f'<li><a href="#f-{object_id}">{name}</a> · {len(item["fields"])} field(s)'
            f"{' (whole document)' if item['whole'] else ''} · "
            f'<span class="count" data-object="{object_id}">0</span> checked</li>'
        )
        sections.append(
            f'<section id="f-{object_id}" data-object="{object_id}"><h2>{name} '
            f'<span class="id">{object_id}{" · scan " + item["scan"] if item["scan"] else ""}</span></h2>'
            f'<p class="reason">Why: {html.escape(item["reason"])} · {len(item["fields"])} field(s)</p>'
            f'<div class="pair"><div class="page"><img src="data:image/jpeg;base64,{image}" '
            f'alt="Page 1 of return {object_id}" title="Click to zoom"></div>'
            f"<table>{''.join(rows)}</table></div></section>"
        )
    n_fields = sum(len(item["fields"]) for item in items)
    return PAGE.format(
        title=f"Review {run}",
        run=html.escape(run),
        rule=html.escape(rule.name),
        n_items=len(items),
        n_fields=n_fields,
        contents="\n".join(contents),
        sections="\n".join(sections),
        run_json=json.dumps(run),
        rule_json=json.dumps(rule.name),
    )


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg: #fbfbfa; --fg: #1d1d1b; --muted: #6b6b66; --line: #ddddd8; --accent: #2a5db0; --warn: #a5560b;
  --done: #2e7d4f; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #1b1b1a; --fg: #ececea; --muted: #a3a39d; --line: #3a3a37; --accent: #8db3f0; --warn: #f0b36d;
    --done: #7fd0a0; }}
}}
body {{ margin: 0; padding: 16px; background: var(--bg); color: var(--fg); font: 15px/1.4 system-ui, sans-serif; }}
header {{ position: sticky; top: 0; background: var(--bg); padding: 8px 0; z-index: 1;
  border-bottom: 1px solid var(--line); display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }}
h1 {{ font-size: 1.2rem; margin: 0; }}
h2 {{ font-size: 1.05rem; margin: 24px 0 4px; scroll-margin-top: 64px; }}
.id, .reason, .note, .intro {{ color: var(--muted); font-size: 0.85rem; }}
#progress {{ font-weight: 600; }}
nav ol {{ margin: 4px 0 0; padding-left: 20px; }}
nav li.complete a {{ color: var(--done); }}
.pair {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: start; }}
@media (max-width: 800px) {{ .pair {{ grid-template-columns: 1fr; }} }}
.page {{ position: sticky; top: 64px; max-height: calc(100vh - 80px); overflow: auto; border: 1px solid var(--line); }}
.page img {{ display: block; width: 100%; cursor: zoom-in; }}
.page img.zoom {{ width: 250%; cursor: zoom-out; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ border-top: 1px solid var(--line); padding: 6px 4px; vertical-align: top; }}
td.label {{ width: 40%; }}
input[type=text] {{ width: 60%; font: inherit; padding: 2px 4px; background: transparent; color: var(--fg);
  border: 1px solid var(--line); }}
.note {{ margin-top: 2px; }}
tr.changed input[type=text] {{ border-color: var(--warn); }}
tr.done td {{ opacity: 0.55; }}
button {{ font: inherit; padding: 6px 12px; background: var(--accent); color: var(--bg); border: 0; cursor: pointer; }}
button.check {{ padding: 2px 8px; background: transparent; color: var(--accent); border: 1px solid var(--accent);
  white-space: nowrap; }}
button.check[aria-pressed="true"] {{ background: var(--done); border-color: var(--done); color: var(--bg); }}
</style></head>
<body>
<header><h1>Review</h1><span id="progress">0 of {n_fields} fields checked</span>
<button id="download">Download corrections</button></header>
<p class="intro">Run {run} · rule: {rule}. For each field, compare the value with page 1 and either correct it
or click "Looks right". Tick Blank when the line is empty on the form (blank is not 0). A field counts as
reviewed only once you've done one of those; the download lists any field nobody checked. Your entries are kept
in this browser until you download them.</p>
<nav><strong>{n_items} filing(s) to review</strong><ol>
{contents}
</ol></nav>
{sections}
<script>
const RUN = {run_json}, RULE = {rule_json}, KEY = "review:" + RUN;
const load = () => {{ try {{ return JSON.parse(localStorage.getItem(KEY)) || {{}}; }} catch (e) {{ return {{}}; }} }};
const save = (state) => {{ try {{ localStorage.setItem(KEY, JSON.stringify(state)); }} catch (e) {{}} }};
const rows = [...document.querySelectorAll("tr[data-field]")];
const objectOf = (row) => row.closest("section").dataset.object;
const keyOf = (row) => objectOf(row) + "/" + row.dataset.field;
function parse(row) {{
  const [text, blank] = row.querySelectorAll("input");
  if (blank.checked) return null;
  const raw = text.value.trim();
  if (row.dataset.kind !== "int") return raw;
  const negative = /^\\(.*\\)$/.test(raw) || raw.startsWith("-");
  const digits = raw.replace(/[^0-9]/g, "");
  return digits === "" ? null : (negative ? -1 : 1) * parseInt(digits, 10);
}}
function refresh() {{
  let done = 0;
  const perObject = {{}};
  for (const row of rows) {{
    const checked = row.classList.contains("done");
    done += checked;
    perObject[objectOf(row)] = perObject[objectOf(row)] || {{ done: 0, all: 0 }};
    perObject[objectOf(row)].all += 1;
    perObject[objectOf(row)].done += checked;
  }}
  document.getElementById("progress").textContent = done + " of " + rows.length + " fields checked";
  for (const span of document.querySelectorAll(".count")) {{
    const counts = perObject[span.dataset.object];
    span.textContent = counts.done + " of " + counts.all;
    span.closest("li").classList.toggle("complete", counts.done === counts.all);
  }}
}}
const state = load();
for (const row of rows) {{
  const [text, blank] = row.querySelectorAll("input");
  const button = row.querySelector("button.check");
  const original = text.value + "|" + blank.checked;
  const saved = state[keyOf(row)];
  let checked = false;
  if (saved) {{
    text.value = saved.text; blank.checked = saved.blank; text.disabled = saved.blank;
    // Entries saved before fields had a checked state count as checked if they were edited.
    checked = "checked" in saved ? saved.checked : text.value + "|" + blank.checked !== original;
  }}
  const paint = () => {{
    row.classList.toggle("changed", text.value + "|" + blank.checked !== original);
    row.classList.toggle("done", checked);
    button.setAttribute("aria-pressed", String(checked));
    button.textContent = checked ? "Checked" : "Looks right";
  }};
  const changed = () => {{
    paint();
    state[keyOf(row)] = {{ text: text.value, blank: blank.checked, checked }};
    save(state);
    refresh();
  }};
  const edited = () => {{ text.disabled = blank.checked; checked = true; changed(); }};
  text.addEventListener("input", edited);
  blank.addEventListener("change", edited);
  button.addEventListener("click", () => {{ checked = !checked; changed(); }});
  paint();
}}
refresh();
document.querySelectorAll("img").forEach((img) => img.addEventListener("click", () => img.classList.toggle("zoom")));
document.getElementById("download").addEventListener("click", () => {{
  const corrections = {{}}, unreviewed = {{}};
  for (const row of rows) {{
    const id = objectOf(row);
    if (row.classList.contains("done")) (corrections[id] ||= {{}})[row.dataset.field] = parse(row);
    else (unreviewed[id] ||= []).push(row.dataset.field);
  }}
  const reviewed_at = new Date().toISOString();
  const body = JSON.stringify({{ run: RUN, rule: RULE, reviewed_at, corrections, unreviewed }}, null, 2);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([body], {{ type: "application/json" }}));
  link.download = RUN + "_corrections.json";
  link.click();
}});
</script></body></html>
"""


# --- Applying the corrections ----------------------------------------------------------------------


def apply(corrections: dict, runs_dir: pathlib.Path = evaluate.RUNS_DIR) -> pathlib.Path:
    """Copy the run to <run>_reviewed with every reviewed field set to the reviewer's value. Fields the
    reviewer didn't check keep the extracted value and are recorded as unreviewed."""
    source = runs_dir / corrections["run"]
    target = runs_dir / f"{corrections['run']}_reviewed"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    unreviewed = corrections.get("unreviewed", {})  # older corrections files had no unreviewed list
    for object_id in sorted(set(corrections["corrections"]) | set(unreviewed)):
        values = corrections["corrections"].get(object_id, {})
        path = target / f"{object_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        answer = record["answer"] or dict.fromkeys(f.name for f in ALL_FIELDS)
        answer.update(values)
        record |= {
            "answer": answer,
            "reviewed_fields": sorted(values),
            "unreviewed_fields": sorted(unreviewed.get(object_id, [])),
            "review_rule": corrections["rule"],
        }
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target


def simulate(run: str, items: list[dict], rule: review.Rule) -> dict:
    """The corrections a perfect reviewer would make: every flagged field set to the answer key's value."""
    keys = evaluate.gold()
    corrections = {
        item["object_id"]: {f["name"]: keys[item["object_id"]]["fields"][f["name"]] for f in item["fields"]}
        for item in items
    }
    return {"run": run, "rule": rule.name, "reviewed_at": "simulated", "corrections": corrections, "unreviewed": {}}


# --- Command line ----------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", nargs="?", type=pathlib.Path, help="a run directory to queue for review")
    parser.add_argument("--second", type=pathlib.Path, help="another run of the same pages")
    parser.add_argument("--rule", default=DEFAULT_RULE, choices=sorted(RULES))
    parser.add_argument("--apply", type=pathlib.Path, help="a downloaded corrections file to apply")
    parser.add_argument("--simulate", action="store_true", help="write a perfect reviewer's corrections instead")
    args = parser.parse_args()

    if args.apply:
        corrections = json.loads(args.apply.read_text(encoding="utf-8"))
        target = apply(corrections)
        score = evaluate.score_run(target)
        evaluate.record(score)
        reviewed = sum(len(v) for v in corrections["corrections"].values())
        unreviewed = sum(len(v) for v in corrections.get("unreviewed", {}).values())
        print(f"Applied {args.apply.name} -> {target}")
        print(f"{reviewed} field(s) reviewed; {unreviewed} flagged field(s) not reviewed (left as extracted)\n")
        print(evaluate.report(score))
        return
    if args.run is None:
        parser.error("give a run directory, or --apply a corrections file")
    rule = RULES[args.rule]
    if rule.second_run and not args.second:
        parser.error(f"rule {rule.name!r} needs --second")

    records = review.load(args.run)
    seconds = {r["object_id"]: r for r in review.load(args.second)} if args.second else None
    items = queue(records, rule, seconds)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    if args.simulate:
        out = REVIEW_DIR / f"{args.run.name}_corrections_simulated.json"
        out.write_text(json.dumps(simulate(args.run.name, items, rule), indent=2) + "\n", encoding="utf-8")
        print(f"Wrote a perfect reviewer's corrections to {out}")
        return
    names = {r["object_id"]: (r["answer"] or {}).get("organization_name") or "" for r in records}
    out = REVIEW_DIR / f"{args.run.name}.html"
    out.write_text(render(args.run.name, rule, items, names), encoding="utf-8")
    n_fields = sum(len(item["fields"]) for item in items)
    print(f"{len(items)} filings, {n_fields} fields to review: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
