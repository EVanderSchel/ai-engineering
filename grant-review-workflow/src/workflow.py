"""The workflow as a LangGraph graph: the step functions from step 2, joined (see "Diagram" in docs/design.md).

    START -> extract --------\
    START -> research -------+-> cross_check -> eligibility -> route -+-> draft_letter -> END  (declined by rules)
    START -> read_proposal --/                                         +-> END                  (waits for reviewer)

Each node calls one step and returns only the fields that step owns; LangGraph merges the returns into the
state. extract, research and read_proposal start together and run in parallel; cross_check waits for all three.
The two lists more than one step adds to (`flags`, `calls`) have a reducer in the state, so parallel additions
are concatenated rather than overwritten. Step 4 adds the pause for the reviewer and checkpoints.

    $env:AWS_PROFILE = "agent-toolkit"; uv run python src/workflow.py   # every dev packet, end to end
"""

import dataclasses
import datetime as dt
import json
import sys
import time
from collections.abc import Callable

from botocore.exceptions import ClientError
from langgraph.graph import END, START, StateGraph

import bedrock
import checks
import draft_letter
import extract
import paths
import read_proposal
import research
from intake import gold_packet, intake
from state import ApplicationState, Flag

Node = Callable[[ApplicationState], dict]


@dataclasses.dataclass(frozen=True)
class Steps:
    """The model-calling steps, as node functions. Passed in, so tests can run the graph with stand-ins."""

    extract: Node
    research: Node
    read_proposal: Node
    draft_letter: Node


def bedrock_steps(client) -> Steps:
    """The real steps, on Bedrock, with the models chosen in step 2 (design decision 7)."""

    def extract_node(state: ApplicationState) -> dict:
        extraction, call = extract.extract(client, state.return_pdf, model="sonnet")
        return {"extraction": extraction, "calls": [call]}

    def research_node(state: ApplicationState) -> dict:
        result, calls, _trajectory = research.research(client, state.form)
        return {"research": result, "calls": calls}

    def read_proposal_node(state: ApplicationState) -> dict:
        reading, problems, call = read_proposal.read_proposal(client, state.narrative, model="sonnet")
        # A reading that quotes or states something the narrative doesn't contain isn't trusted silently.
        flags = [Flag(step="read_proposal", kind="unverified_reading", evidence=p) for p in problems]
        return {"proposal": reading, "flags": flags, "calls": [call]}

    def draft_letter_node(state: ApplicationState) -> dict:
        letter, problems, call = draft_letter.draft_letter(client, state, model="haiku")
        return {"letter": letter, "letter_problems": problems, "calls": [call]}

    return Steps(extract_node, research_node, read_proposal_node, draft_letter_node)


def build(steps: Steps):
    graph = StateGraph(ApplicationState)
    graph.add_node("extract", steps.extract)
    graph.add_node("research", steps.research)
    graph.add_node("read_proposal", steps.read_proposal)
    graph.add_node("cross_check", lambda state: {"flags": checks.cross_check(state)})
    graph.add_node("eligibility", lambda state: {"eligibility": checks.eligibility(state)})
    graph.add_node("route", lambda state: {"route": checks.route(state)})
    graph.add_node("draft_letter", steps.draft_letter)

    for parallel in ("extract", "research", "read_proposal"):
        graph.add_edge(START, parallel)
    graph.add_edge(["extract", "research", "read_proposal"], "cross_check")  # waits for all three
    graph.add_edge("cross_check", "eligibility")
    graph.add_edge("eligibility", "route")
    # Only a decline by the rules gets a letter now; everything else waits for a reviewer (step 4).
    graph.add_conditional_edges(
        "route", lambda state: state.route, {"decline_ineligible": "draft_letter", "review": END}
    )
    graph.add_edge("draft_letter", END)
    return graph.compile()


def diagram() -> str:
    """The compiled graph drawn as Mermaid by LangGraph itself (the README's copy is checked against it)."""
    shape_only = Steps(*(lambda state: {},) * 4)  # the graph is drawn, never run
    return build(shape_only).get_graph().draw_mermaid().strip()


def run(app, application: ApplicationState) -> ApplicationState:
    """Run one application through the graph. LangGraph returns the final state as a dict; validating it back
    into ApplicationState re-checks every value."""
    return ApplicationState.model_validate(app.invoke(application))


# --- End to end on the dev packets -----------------------------------------------------------------------


def run_dev() -> dict:
    app = build(bedrock_steps(bedrock.make_client()))
    rows = []
    for folder in sorted(paths.GOLD_DIR.glob("gp-*")):
        label = json.loads((folder / "label.json").read_text(encoding="utf-8"))
        if label["split"] != "dev":
            continue
        form, narrative, pdf = gold_packet(folder.name)
        started = time.perf_counter()
        final = run(app, intake(form, narrative, pdf, application_id=folder.name))
        seconds = time.perf_counter() - started
        flags = sorted(f.kind for f in final.flags)
        row = {
            "packet_id": folder.name,
            "route": final.route,
            "label_route": label["route"],
            "failed_rules": final.eligibility.failed_rules,
            "label_failed_rules": label["failed_rules"],
            "flags": flags,
            "label_flags": label["flags"],
            "letter_problems": final.letter_problems,
            "cost_usd": round(final.cost_usd, 5),
            "model_calls": len(final.calls),
            "seconds": round(seconds, 1),
        }
        rows.append(row)
        notes = []
        if row["route"] != row["label_route"]:
            notes.append(f"ROUTE {row['route']} vs label {row['label_route']}")
        if row["failed_rules"] != row["label_failed_rules"]:
            notes.append(f"RULES {row['failed_rules']} vs {row['label_failed_rules']}")
        if row["flags"] != row["label_flags"]:
            notes.append(f"FLAGS {row['flags']} vs {row['label_flags']}")
        if row["letter_problems"]:
            notes.append(f"LETTER {row['letter_problems']}")
        print(f"{folder.name}: {'ok' if not notes else ' | '.join(notes)} (${final.cost_usd:.4f}, {seconds:.1f} s)")
    n = len(rows)
    return {
        "date": dt.date.today().isoformat(),
        "packets": n,
        "route_right": sum(r["route"] == r["label_route"] for r in rows),
        "wrong_declines": sum(r["route"] == "decline_ineligible" != r["label_route"] for r in rows),
        "rules_right": sum(r["failed_rules"] == r["label_failed_rules"] for r in rows),
        "flags_exact": sum(r["flags"] == r["label_flags"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "median_seconds": sorted(r["seconds"] for r in rows)[n // 2],
        "rows": rows,
    }


def main() -> int:
    try:
        summary = run_dev()
    except ClientError as e:
        print(f"Bedrock error: {e}", file=sys.stderr)
        return 1
    out = paths.ROOT / "results" / "workflow-dev.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    s = summary
    print(
        f"\nroute {s['route_right']}/{s['packets']} (wrong automatic declines: {s['wrong_declines']}), "
        f"rules {s['rules_right']}/{s['packets']}, flags exactly right {s['flags_exact']}/{s['packets']}, "
        f"${s['cost_usd']:.4f} total, median {s['median_seconds']} s per application. "
        f"Saved {out.relative_to(paths.ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
