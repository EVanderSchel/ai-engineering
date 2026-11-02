"""The quantization measurement's own logic: summaries, prompts, answer parsing, scoring and cost. No server."""

import asyncio

import httpx2
import pytest

import measure_quantization as mq

BROAD = {"1": {"name": "Arts", "groups": "A"}, "5": {"name": "Human Services", "groups": "IJKLMNOP"}}


def test_a_speed_level_is_summarized_from_its_requests():
    requests = [{"ttft": 0.1, "total": 1.1, "tokens": 101}, {"ttft": 0.3, "total": 2.3, "tokens": 101}]
    s = mq.summarize_speed(requests, wall_seconds=2.5)
    assert s["ttft_p50_ms"] == 200 and s["ttft_p95_ms"] == 300
    assert s["tokens_per_s_per_request"] == 75.0  # 100 tokens after the first: 100/s and 50/s, median 75
    assert s["tokens_per_s_total"] == 80.8 and s["requests_per_s"] == 0.8


@pytest.mark.parametrize(
    "answer, labels, label",
    [("B", "ABCD", "B"), (" C.", "ABCD", "C"), ("**D**", "ABCD", "D"), ("A) Sunlight", "ABCD", "A"),
     ("E", "ABCD", None), ("The answer is B", "ABCD", None), ("", "ABCD", None),
     ("5", "15", "5"), ("5: Human Services", "15", "5"), ("12", "15", None)],
)  # fmt: skip
def test_answers_are_parsed_to_a_label_or_none(answer, labels, label):
    assert mq.parse_label(answer, labels) == label


def test_the_prompts_carry_every_choice_and_ask_for_a_label_only():
    quiz = mq.quiz_prompt({"question": "Which is a mammal?", "choices": ["Shark", "Whale", "Trout", "Eel"]})
    assert "B. Whale" in quiz and quiz.endswith("letter of the correct choice only.")
    broad = mq.broad_prompt("We feed families.", BROAD)
    assert '"We feed families."' in broad and "5: Human Services" in broad and broad.endswith("number only.")
    assert mq.broad_of("K", BROAD) == "5"


def test_scores_count_accuracy_unparseable_and_agreement_with_the_reference():
    gold = {"1": "A", "2": "B", "3": "C", "4": "D"}
    predictions = {"1": "A", "2": "B", "3": None, "4": "A"}
    reference = {"1": "A", "2": "C", "3": None, "4": "A"}
    assert mq.score(predictions, gold, reference) == {
        "rows": 4, "accuracy": 0.5, "unparseable": 1, "same_as_reference": 0.75,
    }  # fmt: skip
    assert "same_as_reference" not in mq.score(predictions, gold, None)


def test_fidelity_counts_identical_answers_and_words_before_the_first_difference():
    reference = ["the sky is blue because", "bees make honey"]
    texts = ["the sky is blue because", "bees store nectar"]
    assert mq.words_in_common(texts[1], reference[1]) == 1
    f = mq.fidelity(texts, reference, repeat=["the sky is blue because", "bees store pollen"])
    assert f == {"prompts": 2, "identical_to_reference": 1, "words_in_common_mean": 3.0,
                 "reference_words_mean": 4.0, "repeats_itself": 1}  # fmt: skip


def test_cost_per_thousand_requests_follows_throughput():
    assert mq.cost_per_1k(1.0, usd_per_hour=3.6) == 1.0  # 3,600 requests an hour for $3.60
    assert mq.cost_per_1k(10.0, usd_per_hour=3.6) == 0.1


def test_the_pool_runs_every_item_at_most_n_at_once():
    running, peak = 0, 0

    async def work(item):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return item * 2

    results = asyncio.run(mq.run_pool(list(range(10)), 3, work))
    assert sorted(results) == [i * 2 for i in range(10)] and peak == 3


def test_the_samples_are_400_rows_each_with_valid_answers():
    quiz, ntee = mq.load_data()
    assert len(quiz["rows"]) == 400 and all(r["answer"] in "ABCD" and len(r["choices"]) == 4 for r in quiz["rows"])
    assert len(ntee["rows"]) == 400 and len({r["ein"] for r in ntee["rows"]}) == 400
    assert all(mq.broad_of(r["group"], ntee["broad"]) for r in ntee["rows"])


def test_a_dropped_connection_is_retried_once_and_counted():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise httpx2.RemoteProtocolError("Server disconnected without sending a response.")
        return "answer"

    async def broken():
        raise httpx2.ConnectError("refused")

    mq.RETRIES.clear()
    assert asyncio.run(mq.with_retry(flaky)) == "answer" and len(calls) == 2
    with pytest.raises(httpx2.ConnectError):  # a second failure isn't hidden
        asyncio.run(mq.with_retry(broken))
    assert mq.RETRIES == ["RemoteProtocolError", "ConnectError"]
