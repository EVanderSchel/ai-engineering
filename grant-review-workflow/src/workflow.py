"""The workflow as a LangGraph graph: the step functions from step 2, joined (see "Diagram" in docs/design.md).

    START -> extract --------\
    START -> research -------+-> cross_check -> eligibility -> route -+-> draft_letter -> END  (declined by rules)
    START -> read_proposal --/                     ^                  +-> human_review -+-> draft_letter -> END
                                                   +-------- corrections only ---------+

Each node calls one step and returns only the fields that step owns; LangGraph merges the returns into the
state. extract, research and read_proposal start together and run in parallel; cross_check waits for all three.
The two lists more than one step adds to (`flags`, `calls`) have a reducer in the state, so parallel additions
are concatenated rather than overwritten.

human_review pauses the graph with interrupt(): the state is saved by the checkpointer and the run stops,
possibly for days. resume() continues it with the reviewer's decision. A decision goes to the letter;
corrections alone go back to eligibility, so the rules re-run on the corrected figures. Each application is a
thread (thread_id = application_id), so any process with the same checkpointer can resume it.

    $env:AWS_PROFILE = "agent-toolkit"; uv run python src/workflow.py   # every dev packet, end to end
"""

import argparse
import dataclasses
import datetime as dt
import json
import sys
import time
from collections.abc import Callable

from botocore.exceptions import ClientError
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langgraph_checkpoint_aws import DynamoDBSaver
from pydantic import BaseModel

import bedrock
import checkpoint_table
import checks
import draft_letter
import extract
import paths
import read_proposal
import research
import state as state_models
import tracing
from intake import gold_packet, intake
from state import ApplicationState, Flag, ReviewerDecision

Node = Callable[[ApplicationState], dict]


@dataclasses.dataclass(frozen=True)
class Steps:
    """The model-calling steps, as node functions. Passed in, so tests can run the graph with stand-ins."""

    extract: Node
    research: Node
    read_proposal: Node
    draft_letter: Node


# The model each step uses (design decision 7), in one place: the eval fingerprint covers it.
STEP_MODELS = {
    "extract": "sonnet",
    "research_judge": research.JUDGE_MODEL,
    "research_agent": research.AGENT_MODEL,
    "read_proposal": "sonnet",
    "draft_letter": "haiku",
}


def bedrock_steps(client) -> Steps:
    """The real steps, on Bedrock, with the models chosen in step 2 (design decision 7)."""

    def extract_node(state: ApplicationState) -> dict:
        extraction, call = extract.extract(client, state.return_pdf, model=STEP_MODELS["extract"])
        return {"extraction": extraction, "calls": [call]}

    def research_node(state: ApplicationState) -> dict:
        result, calls, _trajectory = research.research(client, state.form)
        return {"research": result, "calls": calls}

    def read_proposal_node(state: ApplicationState) -> dict:
        reading, problems, call = read_proposal.read_proposal(
            client,
            state.narrative,
            state.form.organization_name,
            state.form.project_title,
            model=STEP_MODELS["read_proposal"],
        )
        # A reading that quotes or states something the narrative doesn't contain isn't trusted silently.
        flags = [Flag(step="read_proposal", kind="unverified_reading", evidence=p) for p in problems]
        return {"proposal": reading, "flags": flags, "calls": [call]}

    def draft_letter_node(state: ApplicationState) -> dict:
        letter, problems, call = draft_letter.draft_letter(client, state, model=STEP_MODELS["draft_letter"])
        return {"letter": letter, "letter_problems": problems, "calls": [call]}

    return Steps(extract_node, research_node, read_proposal_node, draft_letter_node)


def checkpoint_classes() -> list[tuple[str, str]]:
    """The only classes a checkpoint may be rebuilt into: our state models. Rebuilding arbitrary classes from
    stored data is an attack route (whoever can write the checkpoint table could choose what gets
    instantiated), so LangGraph is moving to an allowlist. It's derived from the state module, so it can't go
    stale when a model is added."""
    models = [
        cls
        for cls in vars(state_models).values()
        if isinstance(cls, type) and issubclass(cls, BaseModel) and cls.__module__ == state_models.__name__
    ]
    return sorted({(cls.__module__, cls.__name__) for cls in [*models, bedrock.CallRecord]})


def serializer() -> JsonPlusSerializer:
    """The checkpoint serializer for every checkpointer this workflow uses: our classes only."""
    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_classes())


def memory_checkpointer() -> InMemorySaver:
    """Checkpoints in this process's memory: for tests and local runs (DynamoDB once deployed)."""
    return InMemorySaver(serde=serializer())


CHECKPOINT_TTL_SECONDS = 30 * 24 * 3600  # 30 days (design decision 8)


class _AllowlistedDynamoDBSaver(DynamoDBSaver):
    """DynamoDBSaver takes no serializer argument; it builds its own from self.serde, which LangGraph's base class
    sets from this class attribute during __init__. So this subclass gives it ours before anything uses it."""

    serde = serializer()


def dynamodb_checkpointer(session=None) -> DynamoDBSaver:
    """Checkpoints in the DynamoDB table (src/checkpoint_table.py), kept 30 days."""
    return _AllowlistedDynamoDBSaver(
        table_name=checkpoint_table.TABLE,
        session=session,
        region_name=bedrock.REGION,
        ttl_seconds=CHECKPOINT_TTL_SECONDS,
    )


def review_request(state: ApplicationState) -> dict:
    """What the reviewer is shown when the graph pauses: the flags with their evidence, the rules' result, and
    the figures read from the return (which a reviewer may correct)."""
    return {
        "application_id": state.application_id,
        "organization_name": state.form.organization_name,
        "project_title": state.form.project_title,
        "amount_requested": state.form.amount_requested,
        "failed_rules": state.eligibility.failed_rules if state.eligibility else [],
        "flags": [{"kind": f.kind, "evidence": f.evidence} for f in state.flags],
        "figures": state.extraction.figures.model_dump(mode="json") if state.extraction else None,
        "focus_area": state.proposal.focus_area if state.proposal else None,
    }


def human_review(state: ApplicationState) -> dict:
    """Pause for the reviewer. interrupt() saves the state and stops the run here; resuming with
    Command(resume=decision) makes interrupt() return that decision, and this node finishes. (On resume the
    node runs again from its start, so nothing before interrupt() may have side effects.)"""
    decision = ReviewerDecision.model_validate(interrupt(review_request(state)))
    if state.review is not None:  # a later round keeps the corrections made in earlier ones
        decision = decision.model_copy(update={"corrections": {**state.review.corrections, **decision.corrections}})
    return {"review": decision}


def build(steps: Steps, checkpointer: BaseCheckpointSaver | None = None):
    graph = StateGraph(ApplicationState)
    nodes = {
        "extract": steps.extract,
        "research": steps.research,
        "read_proposal": steps.read_proposal,
        "cross_check": lambda state: {"flags": checks.cross_check(state)},
        "eligibility": lambda state: {"eligibility": checks.eligibility(state)},
        "route": lambda state: {"route": checks.route(state)},
        "human_review": human_review,
        "draft_letter": steps.draft_letter,
    }
    for name, node in nodes.items():
        graph.add_node(name, tracing.traced(name, node))  # a span per step (a no-op outside Lambda)

    for parallel in ("extract", "research", "read_proposal"):
        graph.add_edge(START, parallel)
    graph.add_edge(["extract", "research", "read_proposal"], "cross_check")  # waits for all three
    graph.add_edge("cross_check", "eligibility")
    graph.add_edge("eligibility", "route")
    graph.add_conditional_edges(
        "route", lambda state: state.route, {"decline_ineligible": "draft_letter", "review": "human_review"}
    )
    graph.add_conditional_edges(
        "human_review",
        lambda state: "decided" if state.review.decision else "corrected",
        {"decided": "draft_letter", "corrected": "eligibility"},
    )
    graph.add_edge("draft_letter", END)
    return graph.compile(checkpointer=checkpointer)


def diagram() -> str:
    """The compiled graph drawn as Mermaid by LangGraph itself (the README's copy is checked against it)."""
    shape_only = Steps(*(lambda state: {},) * 4)  # the graph is drawn, never run
    return build(shape_only).get_graph().draw_mermaid().strip()


@dataclasses.dataclass(frozen=True)
class Outcome:
    """Where an application stands after start() or resume()."""

    state: ApplicationState
    waiting_for_review: bool
    review_request: dict | None  # what the reviewer is shown, while waiting


def thread(application_id: str) -> dict:
    return {"configurable": {"thread_id": application_id}}


def outcome(app, application_id: str) -> Outcome:
    """The saved state of an application's thread, validated back into ApplicationState."""
    snapshot = app.get_state(thread(application_id))
    waiting = [i.value for task in snapshot.tasks for i in task.interrupts]
    return Outcome(ApplicationState.model_validate(snapshot.values), bool(waiting), waiting[0] if waiting else None)


def stage_timings(app, application_id: str) -> list[dict]:
    """How long each stage took, from the thread's checkpoint history: a checkpoint records what runs next and
    when it was saved, so the gap to the following checkpoint is that stage's duration. Parallel steps form one
    stage. A gap at human_review is the reviewer's time, not the system's."""
    history = list(reversed(list(app.get_state_history(thread(application_id)))))
    timings = []
    for before, after in zip(history, history[1:], strict=False):
        if before.next and before.next != ("__start__",):
            seconds = (
                dt.datetime.fromisoformat(after.created_at) - dt.datetime.fromisoformat(before.created_at)
            ).total_seconds()
            timings.append({"stage": "+".join(before.next), "seconds": round(seconds, 3)})
    return timings


class ApplicationExists(ValueError):
    """The application ID already has a saved thread: starting it again would feed new input into the old run,
    and the reducers would append to its flags and calls."""


def start(app, application: ApplicationState) -> Outcome:
    """Run a new application until it finishes or pauses for its reviewer."""
    if app.get_state(thread(application.application_id)).values:
        raise ApplicationExists(f"application {application.application_id} already has a saved thread")
    app.invoke(application, thread(application.application_id))
    return outcome(app, application.application_id)


def carry_on(app, application_id: str) -> Outcome:
    """Continue a run that stopped on an error (an outage, a crash) from its last checkpoint. Steps that had
    finished keep their results, so only what failed runs again (step 4's crash test)."""
    app.invoke(None, thread(application_id))
    return outcome(app, application_id)


def resume(app, application_id: str, decision: ReviewerDecision) -> Outcome:
    """Continue a paused application with the reviewer's decision (or corrections).

    The decision is a ReviewerDecision, so it's valid before the graph sees it. That matters: LangGraph saves a
    resume value before the paused node checks it, and replays it on every later resume, so an invalid value
    sent to the graph would leave the thread stuck."""
    app.invoke(Command(resume=decision.model_dump(mode="json")), thread(application_id))
    return outcome(app, application_id)


# --- End to end on the dev packets -----------------------------------------------------------------------


def run_dev(app, run_id: str) -> dict:
    rows = []
    for folder in sorted(paths.GOLD_DIR.glob("gp-*")):
        label = json.loads((folder / "label.json").read_text(encoding="utf-8"))
        if label["split"] != "dev":
            continue
        form, narrative, pdf = gold_packet(folder.name)
        started = time.perf_counter()
        application_id = f"{folder.name}-{run_id}"  # unique per run: a thread can't be started twice
        final = start(app, intake(form, narrative, pdf, application_id=application_id)).state
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
            "application_id": application_id,
            "stages": stage_timings(app, application_id),
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
        "run_id": run_id,
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
    parser = argparse.ArgumentParser(description="Run the dev packets end to end, or resume a paused application.")
    parser.add_argument("--dynamodb", action="store_true", help="checkpoints in DynamoDB (default: in memory)")
    parser.add_argument("--resume", metavar="APPLICATION_ID", help="resume a paused application (needs --dynamodb)")
    parser.add_argument("--decision", choices=["approve", "decline"], help="the reviewer's decision, with --resume")
    parser.add_argument("--reason", help="the reviewer's reason, quoted in the letter, with --resume")
    args = parser.parse_args()
    checkpointer = dynamodb_checkpointer() if args.dynamodb else memory_checkpointer()
    app = build(bedrock_steps(bedrock.make_client()), checkpointer=checkpointer)
    try:
        if args.resume:
            if not (args.dynamodb and args.decision and args.reason):
                parser.error("--resume needs --dynamodb, --decision and --reason")
            return resume_one(app, args.resume, args.decision, args.reason)
        summary = run_dev(app, run_id=dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S"))
    except ClientError as e:
        print(f"AWS error: {e}", file=sys.stderr)
        return 1
    name = "workflow-dev-dynamodb.json" if args.dynamodb else "workflow-dev.json"
    out = paths.ROOT / "results" / name
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    s = summary
    print()
    print(
        f"route {s['route_right']}/{s['packets']} (wrong automatic declines: {s['wrong_declines']}), "
        f"rules {s['rules_right']}/{s['packets']}, flags exactly right {s['flags_exact']}/{s['packets']}, "
        f"${s['cost_usd']:.4f} total, median {s['median_seconds']} s per application. "
        f"Saved {out.relative_to(paths.ROOT)}"
    )
    return 0


def resume_one(app, application_id: str, decision: str, reason: str) -> int:
    """Resume one paused application, in this process, from its checkpoints alone."""
    paused = outcome(app, application_id)
    if not paused.waiting_for_review:
        print(f"{application_id} isn't waiting for a review (route: {paused.state.route})", file=sys.stderr)
        return 1
    review = ReviewerDecision(decision=decision, reason=reason, reviewer="dev", decided_at=dt.datetime.now(dt.UTC))
    done = resume(app, application_id, review)
    print(f"{application_id}: {done.state.review.decision}d, letter problems: {done.state.letter_problems or 'none'}")
    print(f"cost so far ${done.state.cost_usd:.4f} over {len(done.state.calls)} model calls")
    print()
    print(done.state.letter)
    print()
    print("Stages:")
    for stage in stage_timings(app, application_id):
        print(f"  {stage['stage']}: {stage['seconds']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
