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


def test_the_bearer_key_is_read_from_the_header():
    key = keys.new_key()
    assert keys.bearer_key(f"Bearer {key}") == key
    assert keys.bearer_key(f"bearer  {key} ") == key
    assert keys.bearer_key(key) is None  # no scheme
    assert keys.bearer_key("Bearer sk-not-a-gateway-key") is None
    assert keys.bearer_key(None) is None


@pytest.mark.anyio
async def test_new_keys_are_random_and_only_their_hash_identifies_a_team():
    a, b = keys.new_key(), keys.new_key()
    assert a != b and a.startswith("gw_") and len(a) > 40
    table = keys.parse_keys(f"team-a:{keys.key_hash(a)}, team-b:{keys.key_hash(b)}")
    store = keys.MemoryKeyStore(table)
    assert (await store.team_for(a)).name == "team-a"
    assert (await store.team_for(b)).name == "team-b"
    assert await store.team_for(a + "x") is None
    assert a not in str(table)


def test_a_team_may_use_its_routes():
    assert keys.Team(1, "t").may_use("anything")
    limited = keys.Team(1, "t", ("fast",))
    assert limited.may_use("fast") and not limited.may_use("smart")


@pytest.mark.parametrize("value", ["team-a", "team-a:abc", ":" + "0" * 64, "team:" + "G" * 64])
def test_malformed_key_settings_are_refused(value):
    with pytest.raises(ValueError):
        keys.parse_keys(value)


def test_no_keys_configured_means_nobody_gets_in():
    assert keys.parse_keys("") == {}
