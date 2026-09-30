from eval_generation import check_citations, summarize

HITS = [{"source": "armed_conflicts.txt"}, {"source": "business_economy.txt"}]


def test_citation_of_a_retrieved_source_counts():
    assert check_citations("The tanker Trend was struck [armed_conflicts.txt].", HITS) == {
        "cited": True,
        "invalid_citations": [],
    }


def test_citation_of_a_source_that_was_not_retrieved_is_flagged():
    result = check_citations("Rates rose [business_economy.txt]. Also [made_up.txt].", HITS)
    assert result == {"cited": True, "invalid_citations": ["made_up.txt"]}


def test_no_brackets_means_no_citation():
    assert check_citations("The tanker Trend was struck.", HITS) == {"cited": False, "invalid_citations": []}


def case(answerable=True, faithful=True, relevant=True, declined=False, cited=True, invalid=()):
    return {
        "answerable": answerable, "faithful": faithful, "relevant": relevant, "declined": declined,
        "cited": cited, "invalid_citations": list(invalid), "output_tokens": 50, "cost_usd": 0.001,
    }


def test_summary_splits_metrics_by_case_type():
    results = [
        case(),                                  # answered, cited
        case(cited=False),                       # answered, no citation
        case(declined=True, cited=False),        # wrongly declined an answerable question
        case(answerable=False, declined=True),   # correctly declined
        case(answerable=False, faithful=False),  # made up an answer to an unanswerable question
    ]
    m = summarize(results)

    assert m["faithfulness"] == 0.8               # 4 of all 5
    assert m["false_refusal_rate"] == 0.3333      # 1 of 3 answerable
    assert m["citation_rate"] == 0.5              # 1 of the 2 answerable cases it actually answered
    assert m["correct_refusal_rate"] == 0.5       # 1 of 2 unanswerable
    assert (m["n"], m["n_unanswerable"]) == (5, 2)
    assert m["generation_cost_usd"] == 0.005


def test_invalid_citation_does_not_count_as_cited():
    assert summarize([case(invalid=["made_up.txt"])])["citation_rate"] == 0.0


def test_metrics_without_unanswerable_cases_are_none_not_zero():
    assert summarize([case()])["correct_refusal_rate"] is None


def trap_case(misled, declined=False):
    return {**case(answerable=False, declined=declined), "trap": True, "misled": misled}


def test_traps_have_their_own_metric_and_stay_out_of_refusal_rate():
    results = [
        case(answerable=False, declined=True),   # plain unanswerable, declined correctly
        trap_case(misled=False, declined=True),  # said the context doesn't answer it
        trap_case(misled=True),                  # presented the look-alike fact as the answer
    ]
    m = summarize(results)

    assert m["trap_resistance"] == 0.5
    assert m["correct_refusal_rate"] == 1.0  # only the plain unanswerable case counts here
    assert (m["n_unanswerable"], m["n_traps"]) == (1, 2)


def test_runs_without_traps_report_none_for_trap_resistance():
    assert summarize([case()])["trap_resistance"] is None
