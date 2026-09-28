from eval_gate import compare

FLOOR = {"hit_at_1": 0.9, "hit_at_k": 1.0, "mrr": 0.95}


def result(**scores):
    return {"run": {"corpus": "c", "strategy": "vector", "min": FLOOR}, "scores": {**FLOOR, **scores}}


def test_identical_scores_pass():
    lines, regressed, improved = compare([result()])
    assert (regressed, improved) == (False, False)
    assert lines[-1].endswith("| pass |")


def test_any_single_metric_drop_is_a_regression():
    lines, regressed, _ = compare([result(), result(mrr=0.94)])
    assert regressed
    assert "REGRESSED" in lines[-1] and "**0.940** (was 0.950)" in lines[-1]


def test_improvement_passes_and_is_flagged():
    _, regressed, improved = compare([result(hit_at_1=1.0)])
    assert (regressed, improved) == (False, True)


def test_float_noise_is_not_a_regression():
    _, regressed, improved = compare([result(mrr=0.95 - 1e-12)])
    assert (regressed, improved) == (False, False)
