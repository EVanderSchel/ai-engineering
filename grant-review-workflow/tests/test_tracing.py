"""The workflow's spans, recorded in memory with the OpenTelemetry SDK (a dev dependency; in Lambda the ADOT layer
supplies it): a span per step, the reviewer pause isn't an error, a failure records its type only, and no span
carries applicant text."""

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from test_workflow import application, right_answers

import tracing
import workflow

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)  # once per process: OpenTelemetry allows a single global provider


@pytest.fixture(autouse=True)
def spans():
    EXPORTER.clear()
    yield EXPORTER
    EXPORTER.clear()


def by_step(spans):
    return {s.attributes["grant_review.step"]: s for s in spans.get_finished_spans()}


def test_each_step_of_a_run_is_a_span(spans, tmp_path):
    app = workflow.build(right_answers(), checkpointer=workflow.memory_checkpointer())
    workflow.start(app, application("gp-027", tmp_path))
    steps = by_step(spans)
    assert set(steps) == {"extract", "research", "read_proposal", "cross_check", "eligibility", "route", "human_review"}
    assert all(s.attributes["grant_review.application_id"].startswith("gp-027") for s in steps.values())


def test_the_reviewer_pause_is_marked_paused_not_failed(spans, tmp_path):
    app = workflow.build(right_answers(), checkpointer=workflow.memory_checkpointer())
    workflow.start(app, application("gp-027", tmp_path))
    pause = by_step(spans)["human_review"]
    assert pause.attributes["grant_review.paused"] and pause.status.status_code != StatusCode.ERROR


def test_a_failure_records_its_type_and_never_its_message(spans):
    class Leaky(ValueError):
        pass

    def fails(state):
        raise Leaky("the applicant wrote: our secret narrative")

    class State:
        application_id = "gp-027-x"

    with pytest.raises(Leaky):
        tracing.traced("extract", fails)(State())
    [span] = spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR and span.attributes["error.type"] == "Leaky"
    assert "secret narrative" not in json.dumps(span.to_json()) and not span.events


def test_model_calls_and_cost_are_attached_to_their_step(spans, tmp_path):
    app = workflow.build(right_answers(), checkpointer=workflow.memory_checkpointer())
    workflow.start(app, application("gp-027", tmp_path))
    extract = by_step(spans)["extract"]
    assert extract.attributes["grant_review.model_calls"] == 1 and "grant_review.cost_usd" in extract.attributes


def test_no_span_carries_the_narrative(spans, tmp_path):
    app = workflow.build(right_answers(), checkpointer=workflow.memory_checkpointer())
    application_ = application("gp-027", tmp_path)
    workflow.start(app, application_)
    exported = json.dumps([s.to_json() for s in spans.get_finished_spans()])
    assert application_.narrative[200:260] not in exported


def test_a_log_line_can_name_its_trace(spans):
    assert tracing.current_trace_id() is None  # outside any span
    with trace.get_tracer("t").start_as_current_span("x"):
        trace_id = tracing.current_trace_id()
    assert trace_id.startswith("1-") and len(trace_id) == 35
