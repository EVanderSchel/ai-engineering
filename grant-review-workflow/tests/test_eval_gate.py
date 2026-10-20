"""The free eval gate (step 5): CI can't call Bedrock, so it checks the committed dev eval instead. It fails when
the results no longer describe the code (a prompt, a model or a fund rule changed without rerunning
`uv run python src/evaluate.py --split dev --reps 3`), or when they show a wrong automatic decline."""

import json

import pytest

import evaluate

DEV_RESULT = evaluate.RESULTS / "eval-dev.json"


@pytest.fixture(scope="module")
def committed():
    if not DEV_RESULT.exists():
        pytest.fail("no committed dev eval: run `uv run python src/evaluate.py --split dev --reps 3`")
    return json.loads(DEV_RESULT.read_text(encoding="utf-8"))


def test_the_committed_dev_eval_was_made_by_the_current_prompts_models_and_rules(committed):
    current = evaluate.fingerprint()
    changed = [part for part in ("prompts", "models", "rules") if committed["fingerprint"][part] != current[part]]
    assert not changed, f"{', '.join(changed)} changed since the dev eval ran: rerun it and commit the results"


def test_the_committed_dev_eval_has_no_wrong_automatic_declines(committed):
    assert committed["pooled"]["wrong_automatic_declines"] == 0


def test_the_committed_dev_eval_covers_every_dev_packet_each_repetition(committed):
    assert committed["split"] == "dev" and committed["reps"] >= 3
    assert len(committed["rows"]) == len(evaluate.packets("dev")) * committed["reps"]


def test_no_case_in_the_committed_dev_eval_failed(committed):
    assert committed["failed_cases"] == []
