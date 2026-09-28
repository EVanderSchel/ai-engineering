from telemetry import cost_usd


def test_cost_uses_per_million_token_prices():
    # Sonnet 5: $2 per million input tokens, $10 per million output tokens.
    assert cost_usd("claude-sonnet-5", 1_000_000, 0) == 2.0
    assert cost_usd("claude-sonnet-5", 0, 1_000_000) == 10.0
    assert cost_usd("claude-sonnet-5", 484, 49) == 0.001458


def test_unknown_model_has_no_cost_rather_than_a_wrong_one():
    assert cost_usd("some-future-model", 100, 100) is None
