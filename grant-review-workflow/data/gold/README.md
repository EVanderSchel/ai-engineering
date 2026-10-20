# Gold set: grant application packets

40 application packets to the **Prairie Commons Fund**, a fictional fund, with known right answers. See "Gold set plan" in [docs/design.md](../../docs/design.md).

**What's real and what isn't.** Each packet is built on a real organization's real Form 990 (its figures come from the IRS e-file XML, via Project 2). **The organizations did not apply for anything: the application forms and narratives were written for this project**, by Claude from each packet's spec, and approved one by one. Where a narrative says something about the organization beyond its name, mission and the figures in its spec, treat it as invented. Some packets deliberately contain wrong figures, a wrong name, or text that tries to manipulate an automated reviewer (prompt injection); `label.json` says which.

This note lives here rather than in the narratives on purpose: the workflow's model reads `narrative.md`, and a note addressed to its reader would itself look like injected text.

| File | Written by | What it is |
|---|---|---|
| `form.json` | `src/build_gold.py` | The application form: EIN, organization name, amount requested, project title |
| `spec.json` | `src/build_gold.py` | What the narrative must say: focus area, the figures it claims, what's planted |
| `label.json` | `src/build_gold.py` | The right answers: route, failed rules, flags, focus area, injection |
| `narrative.md` | Claude, approved by a person | The proposal, as the workflow's model will read it |
| `narrative.json` | Claude | The project title, the exact injection text (if any), and whether the narrative is approved |
| `index.csv` | `src/build_gold.py` | One row per packet, for review |

Checks: `tests/test_gold_set.py` recomputes every label from the real figures and the spec; `tests/test_narratives.py` (and `uv run python src/narratives.py`) confirms each narrative states exactly its spec's figures.
