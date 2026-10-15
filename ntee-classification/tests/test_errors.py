"""Error analysis: the grading sheet's sampling and parsing, and the report built from saved dev runs."""

import data
import errors


def test_answers_are_read_from_a_graded_sheet():
    sheet = (
        "# Error check\n\n---\n\n## 1. EIN 111 (990)\n\n> A MUSEUM\n\n**Your call:** yes\n\n"
        "---\n\n## 2. EIN 222 (990)\n\n> SOCCER\n\n**Your call:** \nno, N\n\n"
        "---\n\n## 3. EIN 333 (990)\n\n> CHARITY\n\n**Your call:** Unsure, could be T\n\n"
        "---\n\n## 4. EIN 444 (990)\n\n> BLANK\n\n**Your call:** \n"
    )
    assert errors.read_answers(sheet) == {"111": "yes", "222": "no", "333": "unsure"}


def test_graded_counts_by_kind():
    key = [{"ein": "1", "kind": "hard"}, {"ein": "2", "kind": "hard"}, {"ein": "3", "kind": "control"}]
    result = errors.graded(key, {"1": "no", "2": "yes", "3": "yes"})
    assert result["hard"] == {"graded": 2, "yes": 1, "unsure": 0, "no": 1}
    assert result["control"] == {"graded": 1, "yes": 1, "unsure": 0, "no": 0}


def test_the_sheet_mixes_hard_and_control_missions():
    rows, preds = data.load("dev"), errors.load_predictions()
    sample = errors.sheet_sample(rows, preds)
    kinds = [r["kind"] for r in sample]
    assert kinds.count("hard") == errors.HARD and kinds.count("control") == errors.CONTROL
    assert len({r["ein"] for r in sample}) == len(sample)
    for r in sample:
        all_wrong = all(preds[a][r["ein"]] != r["group"] for a in errors.APPROACHES)
        assert all_wrong == (r["kind"] == "hard")
    assert errors.sheet_sample(rows, preds) == sample  # same seed, same sheet


def test_agreement_shares_sum_to_one_and_length_buckets_cover_dev():
    rows, preds = data.load("dev"), errors.load_predictions()
    assert abs(sum(errors.agreement(rows, preds).values()) - 1) < 1e-9
    assert sum(b["n"] for b in errors.by_length(rows, preds)) == len(rows)


def test_picks_are_read_after_a_no_or_unsure():
    sheet = (
        "# x\n\n---\n\n## 1. EIN 111 (990)\n\n> A\n\n**Your call:** no, F or P\n\n"
        "---\n\n## 2. EIN 222 (990)\n\n> B\n\n**Your call:** yes\n\n"
        "---\n\n## 3. EIN 333 (990)\n\n> C\n\n**Your call:** \nunsure, maybe T\n"
    )
    assert errors.read_picks(sheet) == {"111": {"F", "P"}, "333": {"T"}}


def test_picks_vs_models_counts_matches_on_disputed_hard_missions():
    key = [{"ein": "1", "kind": "hard"}, {"ein": "2", "kind": "hard"}, {"ein": "3", "kind": "control"}]
    preds = {a: {"1": "F", "2": "B", "3": "C"} for a in errors.APPROACHES}
    answers, picks = {"1": "no", "2": "no", "3": "no"}, {"1": {"F", "P"}, "2": {"X"}, "3": {"C"}}
    result = errors.picks_vs_models(key, answers, picks, preds)
    assert result["n"] == 2 and result["Sonnet"] == 1
