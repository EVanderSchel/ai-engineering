"""Prompted classification with a fake Claude client: request shape, parsing, refusals, cost, resume.
No API calls."""

from types import SimpleNamespace

import pytest

import prompted
import prompts


class FakeClient:
    """Answers every request with the same ranking (or a refusal), recording the requests."""

    def __init__(self, ranking=("B", "O", "B"), stop_reason="end_turn", model=None):
        self.requests = []
        self.ranking, self.stop_reason, self.model = ranking, stop_reason, model
        self.messages = SimpleNamespace(parse=self.parse)
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, **request):
        self.requests.append(request)
        first, second, third = self.ranking
        parsed = request["output_format"](first=first, second=second, third=third)
        usage = SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=900
        )
        return SimpleNamespace(
            parsed_output=parsed, stop_reason=self.stop_reason, usage=usage, model=self.model or request["model"]
        )


def test_prompt_variants_build_on_each_other():
    names, guided, fewshot = (prompts.system_prompt(v) for v in prompts.VARIANTS)
    assert "Education" in names and "parent-teacher" not in names and "NTEE conventions" not in names
    assert "parent-teacher" in guided and "NTEE conventions" in guided and "Examples" not in guided
    assert fewshot.startswith(guided) and "Examples of missions and their groups" in fewshot


def test_few_shot_examples_come_from_train_only():
    examples = prompts.few_shot_examples()
    assert all(e["split"] == "train" for e in examples)
    assert len({e["ein"] for e in examples}) == len(examples)
    assert examples == prompts.few_shot_examples()  # fixed seed: the same prompt every time


def test_sonnet_requests_turn_thinking_off_and_enable_the_fallback():
    body = prompted.request("sonnet", "system text", "Feed families")
    assert body["model"] == "claude-sonnet-5-5"
    assert body["thinking"] == {"type": "between_tools"}  # "disabled" is a 400 on Sonnet 5.5
    assert body["fallbacks"] == "default" and body["betas"] == [prompted.FALLBACK_BETA]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_haiku_requests_have_no_sonnet_only_fields():
    body = prompted.request("haiku", "system text", "Feed families")
    assert body["model"] == "claude-haiku-4-5"
    assert not {"thinking", "fallbacks", "betas"} & body.keys()


def test_classify_returns_a_deduplicated_ranking_with_cost():
    answer = prompted.classify(FakeClient(("B", "O", "B")), "haiku", "system", "Run a charter school")
    assert answer["ranked"] == ["B", "O"]  # a repeated guess counts once
    assert answer["cost_usd"] == pytest.approx((100 * 1.0 + 900 * 0.1 + 20 * 5.0) / 1e6)


def test_a_refusal_is_an_empty_prediction():
    answer = prompted.classify(FakeClient(stop_reason="refusal"), "sonnet", "system", "mission")
    assert answer["ranked"] == [] and answer["stop_reason"] == "refusal"


def test_the_answering_model_is_recorded_and_priced():
    answer = prompted.classify(FakeClient(model="claude-sonnet-5"), "sonnet", "system", "mission")
    assert answer["model"] == "claude-sonnet-5"  # served by the fallback


def test_the_group_schema_only_allows_the_25_letters():
    with pytest.raises(ValueError):
        prompted.Ranking(first="Z", second="B", third="O")


def test_run_saves_answers_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(prompted, "CACHE_DIR", tmp_path)
    rows = [{"ein": str(i), "mission": f"mission number {i}"} for i in range(5)]
    first = FakeClient()
    prompted.run(first, "haiku", "names", "dev", rows[:3], workers=2)
    second = FakeClient()
    answers = prompted.run(second, "haiku", "names", "dev", rows, workers=2)
    assert len(first.requests) == 3 and len(second.requests) == 2  # the saved three weren't asked again
    assert set(answers) == {r["ein"] for r in rows}


def test_pilot_subset_is_fixed_and_a_subset():
    rows = [{"ein": f"{i:09d}"} for i in range(100)]
    pilot = prompted.subset(rows, 30)
    assert len(pilot) == 30 and pilot == prompted.subset(rows, 30)
    assert prompted.subset(rows, None) == rows
