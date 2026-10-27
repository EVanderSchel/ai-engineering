import pytest
from pydantic import ValidationError

import config
import keys


def test_the_committed_routes_file_is_valid():
    routes = config.load_routes()
    assert {"smart", "fast", "stub"} <= routes.keys()
    assert routes["smart"].candidates[0].refusal_fallback


@pytest.mark.parametrize(
    "text",
    [
        "smart: {candidates: [], max_tokens_ceiling: 10}",  # no candidates
        "smart: {candidates: [{provider: openai, model: x}], max_tokens_ceiling: 10}",  # unknown provider
        "smart: {candidates: [{provider: stub, model: x, temprature: 1}], max_tokens_ceiling: 10}",  # typo
        "smart: {candidates: [{provider: stub, model: x}]}",  # no ceiling
        "[]",
    ],
)
def test_a_bad_routes_file_fails_at_load(tmp_path, text):
    path = tmp_path / "routes.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises((ValidationError, ValueError)):
        config.load_routes(path)


def test_new_keys_are_random_and_only_their_hash_identifies_a_team():
    a, b = keys.new_key(), keys.new_key()
    assert a != b and a.startswith("gw_") and len(a) > 40
    table = keys.parse_keys(f"team-a:{keys.key_hash(a)}, team-b:{keys.key_hash(b)}")
    assert keys.team_for(f"Bearer {a}", table) == "team-a"
    assert keys.team_for(f"bearer {b}", table) == "team-b"
    assert keys.team_for(a, table) is None  # no scheme
    assert keys.team_for(f"Bearer {a}x", table) is None
    assert keys.team_for(None, table) is None
    assert a not in str(table)


@pytest.mark.parametrize("value", ["team-a", "team-a:abc", ":" + "0" * 64, "team:" + "G" * 64])
def test_malformed_key_settings_are_refused(value):
    with pytest.raises(ValueError):
        keys.parse_keys(value)


def test_no_keys_configured_means_nobody_gets_in():
    assert keys.parse_keys("") == {}
