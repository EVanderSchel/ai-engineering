"""The tests never talk to Langfuse: they swap its observations for FakeObservation (conftest.py), and
tracing.py uses _NoOp when tracing is off. Those stand-ins only prove something if they behave like the
real SDK, so these tests check them against the installed langfuse package. Langfuse SDK v5 removed
`set_trace_io`, which both stand-ins still had: every test passed while real tracing crashed /ask."""

import inspect

import pytest
from conftest import FakeObservation
from langfuse import Langfuse, LangfuseGeneration, LangfuseRetriever, LangfuseSpan

import tracing

# The observation types the API creates: the root span, the retrieval step and the generation step.
REAL_OBSERVATIONS = [LangfuseSpan, LangfuseRetriever, LangfuseGeneration]


def _public_methods(cls) -> set[str]:
    return {name for name, _ in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")}


@pytest.mark.parametrize("stand_in", [FakeObservation, tracing._NoOp])
@pytest.mark.parametrize("real", REAL_OBSERVATIONS, ids=lambda cls: cls.__name__)
def test_stand_ins_only_offer_methods_the_real_sdk_has(stand_in, real):
    missing = _public_methods(stand_in) - _public_methods(real)
    assert not missing, f"{stand_in.__name__} has {sorted(missing)}, which langfuse's {real.__name__} doesn't"


def test_start_trace_passes_only_arguments_the_client_accepts():
    accepted = inspect.signature(Langfuse.start_observation).parameters
    for argument in ("trace_context", "name", "input", "metadata"):
        assert argument in accepted
