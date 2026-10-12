"""The eval's code grader: finding figures in answers, matching them, and the grounded check. The judge
itself is a model call; here it's replaced by a fake that returns a fixed verdict."""

import json
from types import SimpleNamespace

import pytest

import grade


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("revenue was $12,797,138", "usd", 12797138),
        ("about $12.8 million", "usd", 12_800_000),
        ("$47.2M in revenue", "usd", 47_200_000),
        ("$3.2B", "usd", 3_200_000_000),
        ("rose by $88.4K", "usd", 88_400),
        ("1,457,953 dollars", "usd", 1457953),
        ("up 67.64%", "pct", 67.64),
        ("fell 3.6 percent", "pct", 3.6),
    ],
)
def test_figures_are_found(text, kind, value):
    [figure] = grade.figures(text)
    assert figure.kind == kind
    assert figure.value == pytest.approx(value)


def test_years_eins_and_counts_are_not_figures():
    text = "In tax year 2023 (Form 990), EIN 42-1404937 was one of 27 affiliates; 501(c)(3) since 1986."
    assert grade.figures(text) == []


@pytest.mark.parametrize(
    ("written", "true_value", "ok"),
    [
        ("$12,797,138", 12797138, True),
        ("$12.8 million", 12797138, True),  # within 1%
        ("$13 million", 12797138, True),  # true value rounded to the precision written
        ("$14 million", 12797138, False),
        ("67.6%", 67.64, True),
        ("68%", 67.64, True),
        ("67.0%", 67.64, False),
        ("-$34,955", -34955, True),  # sign is a rubric matter
        ("3.9%", 3.73, False),  # the q12 distinction must survive rounding
    ],
)
def test_matching(written, true_value, ok):
    [figure] = grade.figures(written)
    assert grade.matches(true_value, figure) is ok


def tool_event(output: dict, is_error=False):
    return {"type": "tool", "name": "compute_ratios", "input": {}, "output": json.dumps(output), "is_error": is_error}


TRAJECTORY = [tool_event({"total_revenue_change": 5163462, "total_revenue_change_pct": 0.6764, "tax_year": 2023})]
CASE = {
    "question": "How did revenue change?",
    "expected": [{"kind": "usd", "value": 5163462}, {"kind": "pct", "value": 67.64}],
    "rubric": None,
}


def test_oracle_answer_passes_both_metrics():
    result = grade.grade(CASE, "Revenue rose by $5,163,462 (67.64%) from 2020 to 2023.", TRAJECTORY)
    assert result["grade"] == {"correct": 1, "grounded": 1}


@pytest.mark.parametrize("answer", [None, "", "I don't know."])
def test_null_answers_fail(answer):
    assert grade.grade(CASE, answer, TRAJECTORY)["grade"]["correct"] == 0


def test_a_missing_figure_fails_correct():
    result = grade.grade(CASE, "Revenue rose by $5,163,462.", TRAJECTORY)
    assert result["grade"]["correct"] == 0
    assert "Missing expected pct 67.64" in result["explanation"]["correct"]


def test_self_computed_or_invented_figures_fail_grounded():
    answer = "Revenue rose by $5,163,462 (67.64%), and the two years combined came to $20,431,000."
    result = grade.grade(CASE, answer, TRAJECTORY)
    assert result["grade"] == {"correct": 1, "grounded": 0}
    assert "$20,431,000" in result["explanation"]["grounded"]


def test_error_results_do_not_ground_figures():
    trajectory = [tool_event({"total_revenue": 5163462}, is_error=True)]
    assert grade.ungrounded("$5,163,462", trajectory)


class FakeJudge:
    def __init__(self, passed):
        self.passed = passed
        self.prompts = []
        self.messages = SimpleNamespace(parse=self.parse)

    def parse(self, **request):
        self.prompts.append(request["messages"][0]["content"])
        verdict = grade.Verdict(reason="because", passed=self.passed)
        return SimpleNamespace(
            parsed_output=verdict, usage={"input_tokens": 1}, model="claude-opus-5", stop_reason="end_turn"
        )


@pytest.mark.parametrize("passed", [True, False])
def test_rubric_verdict_decides_correct_when_figures_are_present(passed):
    case = CASE | {"rubric": "Says revenue rose."}
    judge = FakeJudge(passed)
    result = grade.grade(case, "Revenue rose by $5,163,462 (67.64%).", TRAJECTORY, judge)
    assert result["grade"]["correct"] == int(passed)
    assert "<criteria>\nSays revenue rose.\n</criteria>" in judge.prompts[0]
    assert "compute_ratios" in judge.prompts[0]  # the judge sees the tool results


def test_judge_with_no_verdict_is_an_error_not_a_fail():
    judge = FakeJudge(True)
    judge.messages = SimpleNamespace(
        parse=lambda **r: SimpleNamespace(parsed_output=None, stop_reason="refusal", usage={}, model="x")
    )
    with pytest.raises(RuntimeError, match="no verdict"):
        grade.grade(CASE | {"rubric": "x"}, "answer", TRAJECTORY, judge)
