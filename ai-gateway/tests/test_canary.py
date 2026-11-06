"""The eval canary: parsing, scoring, the baseline comparison, and whole checks through the in-memory gateway with
the scripted model. Nothing here reaches a network or a real model."""

import asyncio
import copy
import sys

import pytest

import canary
import config
import prompts
from stub_provider import StubProvider

SETTINGS = canary.load_checks()


def run_sync(coro):
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


# --- the checks file and the data ---


def test_every_check_names_a_real_route_and_prompt():
    routes, registry = config.load_routes(), prompts.Registry()
    for check in SETTINGS.checks.values():
        assert check.route in routes
        if check.prompt:
            registry.resolve(check.prompt)  # raises if it isn't registered


def test_temperature_is_only_set_where_every_candidate_takes_it():
    routes = config.load_routes()
    for check in SETTINGS.checks.values():
        if check.temperature is not None:
            assert all(c.provider in ("local", "stub") for c in routes[check.route].candidates)


def test_items_are_the_first_rows_every_time():
    first, again = canary.load_items("ntee", 5), canary.load_items("ntee", 10)
    assert again[:5] == first
    assert first[0].user.startswith("<mission>") and first[0].user.endswith("</mission>")
    assert len(first[0].labels) == 25
    arc = canary.load_items("arc", 3)
    assert arc[0].labels == "ABCD" and "Answer with the letter" in arc[0].user


def test_asking_for_more_rows_than_a_suite_has_fails():
    with pytest.raises(ValueError, match="only 400 rows"):
        canary.load_items("arc", 401)


# --- parsing ---


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("B, O, N", ["B", "O", "N"]),
        ("B,O,N", ["B", "O", "N"]),
        ("B", ["B"]),
        ("B, B, O", ["B", "O"]),  # a repeat counts once
        ("**P**, L, K, E", ["P", "L", "K"]),  # at most three
        ("Answer: X, Q", ["X", "Q"]),  # the word "Answer" isn't a letter
        ("Z, B", ["B"]),  # Z isn't an NTEE major group
        ("I'm not sure.", ["I"]),  # a lone capital I reads as group I: kept, as Project 4 would score it
        ("", []),
    ],
)
def test_ntee_answers_are_parsed_most_likely_first(answer, expected):
    labels = canary.load_items("ntee", 1)[0].labels
    assert canary.parse("ntee", answer, labels) == expected


@pytest.mark.parametrize(("answer", "expected"), [("B", ["B"]), ("C. organs", ["C"]), ("The answer is B", [])])
def test_arc_answers_are_parsed_like_step_6(answer, expected):
    assert canary.parse("arc", answer, "ABCD") == expected


# --- scoring and comparing ---


def result(id_, correct, error=None, top3=None):
    if error:
        return {"id": id_, "gold": "B", "error": error}
    return {
        "id": id_, "gold": "B", "answer": "x", "predicted": ["B"] if correct else ["C"], "correct": correct,
        "top3": correct if top3 is None else top3, "model": "m", "input_tokens": 10, "output_tokens": 2,
    }  # fmt: skip


def run_of(results, subject=None):
    return {"items": results, "summary": canary.summarize(results, "ntee"), "subject": subject or {"route": "r"}}


def test_summary_leaves_failed_requests_out_of_accuracy():
    s = canary.summarize([result("a", True), result("b", False, top3=True), result("c", None, error="timeout")], "ntee")
    assert (s["rows"], s["answered"], s["errors"]) == (3, 2, 1)
    assert s["accuracy"] == 0.5 and s["top3"] == 1.0
    assert s["input_tokens"] == 20 and s["models"] == {"m": 2}


def test_a_run_within_the_margin_passes():
    baseline = run_of([result(str(i), i < 16) for i in range(20)])  # 80%
    run = run_of([result(str(i), i < 15) for i in range(20)])  # 75%: exactly the margin
    verdict = canary.compare(run, baseline, margin=0.05)
    assert verdict["verdict"] == "pass" and verdict["change"] == -0.05


def test_a_run_below_the_margin_fails_and_says_what_moved():
    baseline = run_of([result(str(i), i < 16) for i in range(20)])  # 80%
    run = run_of([result(str(i), 2 <= i < 16) for i in range(20)])  # 70%: items 0 and 1 broke
    verdict = canary.compare(run, baseline, margin=0.05)
    assert verdict["verdict"] == "fail"
    assert (verdict["broken"], verdict["fixed"], verdict["items_compared"]) == (2, 0, 20)


def test_a_swap_keeps_the_score_but_shows_up():
    baseline = run_of([result("a", True), result("b", False)])
    run = run_of([result("a", False), result("b", True)])
    verdict = canary.compare(run, baseline, margin=0.0)
    assert verdict["verdict"] == "pass" and (verdict["broken"], verdict["fixed"]) == (1, 1)


def test_only_items_both_runs_answered_are_compared():
    baseline = run_of([result("a", True), result("b", True), result("c", None, error="timeout")])
    run = run_of([result("a", True), result("b", None, error="timeout"), result("c", False)] + [
        result(str(i), True) for i in range(40)
    ])  # fmt: skip
    verdict = canary.compare(run, baseline, margin=0.0)
    assert verdict["items_compared"] == 1 and verdict["verdict"] == "pass"


def test_too_many_failed_requests_make_a_run_incomplete():
    run = run_of([result(str(i), True) for i in range(18)] + [result("x", None, "timeout"), result("y", None, "x")])
    assert canary.compare(run, run_of([]), margin=0.05)["verdict"] == "incomplete"  # 2 of 20 > 5%


def test_without_a_baseline_the_verdict_says_so():
    assert canary.compare(run_of([result("a", True)]), None, 0.05) == {"verdict": "no_baseline"}


def test_the_subject_changes_with_the_model_and_the_prompt_version(tmp_path):
    routes, registry = config.load_routes(), prompts.Registry()
    check = SETTINGS.checks["ntee-fast"]
    before = canary.subject(check, routes, registry)
    assert before["prompt"]["ref"] == "ntee-classify@v1"
    swapped = copy.deepcopy(routes)
    swapped["fast"].candidates[0] = config.Candidate(provider="anthropic", model="claude-haiku-5-5")
    assert canary.subject(check, swapped, registry) != before
    pinned = check.model_copy(update={"prompt": "ntee-classify@v1"})
    assert canary.subject(pinned, routes, registry) == before  # the same version, named another way


# --- whole checks through the gateway, with the scripted model ---


def check_through_gateway(name, accuracy, baseline=None, seed=0):
    check = SETTINGS.checks[name]
    routes, registry = config.load_routes(), prompts.Registry()
    items = canary.load_items(check.suite, check.rows)
    scripted = canary.ScriptedProvider(items, accuracy=accuracy, seed=seed)

    async def go():
        async with canary.client_for(canary.make_app(routes, canary.scripted_providers(scripted), registry)) as c:
            return await canary.run_check(name, check, c, canary.subject(check, routes, registry), 0.05, baseline)

    return run_sync(go()), scripted


def test_an_ntee_check_sends_the_registered_prompt_and_scores_the_answers():
    run, scripted = check_through_gateway("ntee-fast", accuracy=0.8)
    request = scripted.requests[0]
    v1 = prompts.Registry().resolve("ntee-classify@v1")
    assert request.system == v1.template  # the whole system prompt, as registered
    assert request.turns[-1].text.startswith("<mission>")
    assert request.temperature is None  # the Claude routes take no temperature
    s = run["summary"]
    assert s["rows"] == s["answered"] == 60 and s["errors"] == 0 and s["unparseable"] == 0
    assert 0.65 <= s["accuracy"] <= 0.95  # about 80%: the hash decides which
    assert run["comparison"] == {"verdict": "no_baseline"}


def test_an_arc_check_uses_temperature_zero_and_no_prompt():
    run, scripted = check_through_gateway("arc-local", accuracy=1.0)
    assert scripted.requests[0].system is None and scripted.requests[0].temperature == 0
    assert run["summary"]["accuracy"] == 1.0


def test_the_same_scripted_model_passes_against_its_own_baseline():
    baseline, _ = check_through_gateway("ntee-smart", accuracy=0.8)
    again, _ = check_through_gateway("ntee-smart", accuracy=0.8, baseline=baseline)
    assert again["comparison"]["verdict"] == "pass" and again["comparison"]["change"] == 0
    assert again["comparison"]["same_subject"] is True


def test_a_worse_model_fails_the_canary():
    baseline, _ = check_through_gateway("ntee-fast", accuracy=0.8)
    worse, _ = check_through_gateway("ntee-fast", accuracy=0.6, baseline=baseline)
    assert worse["comparison"]["verdict"] == "fail"
    assert worse["comparison"]["broken"] > 0 and worse["comparison"]["fixed"] == 0


def test_a_provider_outage_makes_the_run_incomplete_not_failed():
    check = SETTINGS.checks["arc-local"]
    routes, registry = config.load_routes(), prompts.Registry()
    from providers import ProviderError

    down = StubProvider(name="local", fail=ProviderError("unavailable", "down", "local"))
    providers = {"local": down, "stub": down, "anthropic": down, "bedrock": down}

    async def go():
        async with canary.client_for(canary.make_app(routes, providers, registry)) as c:
            return await canary.run_check("arc-local", check, c, canary.subject(check, routes, registry), 0.05, None)

    run = run_sync(go())
    assert run["summary"]["errors"] == 100 and run["summary"]["accuracy"] is None
    assert run["comparison"]["verdict"] == "incomplete"


def test_every_check_has_a_stub_baseline_of_its_size():
    """`canary.py --stub` compares with these; regenerate them with --stub --save-baseline after adding a check or
    changing its rows. (Their subjects may lag routes.yaml: the scripted model ignores the route's models.)"""
    for name, check in SETTINGS.checks.items():
        baseline = canary.read_json(canary.OUT_DIR / "stub" / "baselines" / f"{name}.json")
        assert baseline is not None, name
        assert baseline["summary"]["rows"] == check.rows


def test_baselines_can_be_read_from_the_working_tree_or_a_git_ref():
    path = canary.OUT_DIR / "baselines" / "ntee-fast.json"
    assert canary.read_baseline(path, None)["check"] == "ntee-fast"
    missing = canary.OUT_DIR / "baselines" / "no-such-check.json"
    assert canary.read_baseline(missing, None) is None
    assert canary.read_baseline(missing, "HEAD") is None  # a check the pull request adds has no baseline on main


def test_the_live_baselines_match_the_current_checks():
    """Each committed baseline must test what its check tests now. A PR that changes a route or prompt commits the
    new baseline from the canary's CI run (its artifact), and CI still compares with main's."""
    routes, registry = config.load_routes(), prompts.Registry()
    for name, check in SETTINGS.checks.items():
        baseline = canary.read_json(canary.OUT_DIR / "baselines" / f"{name}.json")
        assert baseline is not None, name
        assert baseline["subject"] == canary.subject(check, routes, registry), name
        assert baseline["summary"]["rows"] == check.rows and baseline["comparison"]["verdict"] != "incomplete"


@pytest.mark.parametrize(
    ("baseline", "changed", "ref", "runs"),
    [
        (None, False, None, True),  # without --changed, every check runs
        (None, True, None, True),  # locally, a check with no baseline runs, to make one
        (None, True, "HEAD^1", False),  # in CI, nothing on main to compare with: not paid for
        ({"subject": {"route": "fast"}}, True, "HEAD^1", False),  # unchanged
        ({"subject": {"route": "smart"}}, True, "HEAD^1", True),  # changed: compared with main's
    ],
)
def test_changed_runs_only_checks_that_can_be_compared_and_have_changed(baseline, changed, ref, runs):
    assert (canary.skip_reason({"route": "fast"}, baseline, changed, ref) is None) == runs
