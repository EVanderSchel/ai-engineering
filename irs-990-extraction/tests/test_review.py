import copy

import pytest

import evaluate
import review
from fields import FIELD_NAMES


@pytest.fixture
def record():
    """A filing extracted perfectly, easy (little thinking), checks passed first time."""
    object_id, key = next(iter(evaluate.gold().items()))
    return {
        "object_id": object_id,
        "answer": dict(key["fields"]),
        "problems": [],
        "attempts": [{}],
        "usage": {"output_tokens": 900},
        "unsure": [],
    }


def test_a_filing_without_an_answer_is_always_reviewed_whole(record):
    record["answer"] = None
    assert review.flagged_fields(record, review.Rule("none")) == set(FIELD_NAMES)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"problems": ["Line 12 ..."]}, review.Rule("checks", checks=True)),
        ({"attempts": [{}, {}]}, review.Rule("checks", checks=True)),  # passed, but only on the retry
        ({"usage": {"output_tokens": 5000}}, review.Rule("thinking", max_output_tokens=2000)),
    ],
)
def test_whole_document_signals(record, change, rule):
    assert review.flagged_fields(record | change, rule) == set(FIELD_NAMES)
    assert review.flagged_fields(record | change, review.Rule("none")) == set()  # only when the rule uses them


def test_field_signals_doubts_and_disagreement(record):
    record["unsure"] = ["volunteers"]
    second = copy.deepcopy(record)
    second["answer"]["employees"] = (record["answer"]["employees"] or 0) + 1
    assert review.flagged_fields(record, review.Rule("doubts", doubts=True)) == {"volunteers"}
    assert review.flagged_fields(record, review.Rule("second", second_run=True), second) == {"employees"}
    both = review.Rule("both", doubts=True, second_run=True)
    assert review.flagged_fields(record, both, second) == {"volunteers", "employees"}


def test_disagreement_ignores_differences_the_scorer_ignores(record):
    second = copy.deepcopy(record)
    second["answer"]["organization_name"] = second["answer"]["organization_name"].lower()
    assert review.flagged_fields(record, review.Rule("second", second_run=True), second) == set()


def test_a_rule_is_scored_by_errors_caught_and_fields_reviewed(record):
    wrong = copy.deepcopy(record)
    wrong["answer"]["employees"] = (record["answer"]["employees"] or 0) + 1
    wrong["answer"]["volunteers"] = 999
    wrong["unsure"] = ["employees", "mission"]  # one real error, one false alarm; misses volunteers
    s = review.score_rule([wrong], review.Rule("doubts", doubts=True))
    assert (s["errors"], s["caught"], s["fields_reviewed"]) == (2, 1, 2)
    assert s["accuracy_before"] == pytest.approx(1 - 2 / 42) and s["accuracy_after"] == pytest.approx(1 - 1 / 42)
    assert s["review_share"] == pytest.approx(2 / 42) and s["documents_reviewed_whole"] == 0
