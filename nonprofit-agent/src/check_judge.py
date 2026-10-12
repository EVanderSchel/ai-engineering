"""Check the judge before trusting it: answers that must FAIL their rubric, plus answers that must PASS,
each judged twice to see whether verdicts are consistent. About $0.15 (Opus 5).

    python src/check_judge.py

The tool results each answer is judged against are real: the tools run on the eval's frozen snapshot.
"""

import anthropic

import agent
import grade
import propublica
import run_eval

# (case id, tool calls the agent would have made, answer, verdict the judge must give)
CHECKS = [
    (
        "q15",
        [
            ("search_organizations", {"query": "Iowa Public Radio"}),
            ("get_financials", {"ein": "20-4227123", "tax_year": 2024}),
        ],
        "I don't know.",
        False,
    ),
    (
        "q15",
        [
            ("search_organizations", {"query": "Iowa Public Radio"}),
            ("get_financials", {"ein": "20-4227123", "tax_year": 2024}),
        ],
        "Iowa Public Radio's 2024 revenue was $13,420,551 (Form 990).",
        False,
    ),
    (
        "q15",
        [
            ("search_organizations", {"query": "Iowa Public Radio"}),
            ("get_financials", {"ein": "20-4227123", "tax_year": 2024}),
        ],
        "Iowa Public Radio is a public radio network based in Des Moines that operates stations across Iowa.",
        False,
    ),
    (
        "q18",
        [
            ("search_organizations", {"query": "Sehgal Foundation"}),
            ("get_financials", {"ein": "42-1477858", "tax_year": 2023}),
        ],
        "The Sehgal Foundation paid its officers $0 in 2023 (Form 990-PF).",
        False,
    ),
    (
        "q20",
        [
            ("search_organizations", {"query": "Habitat for Humanity", "state": "IA"}),
            ("get_financials", {"ein": "42-1520979", "tax_year": 2023}),
        ],
        "Habitat for Humanity's revenue in Iowa in 2023 was $2,480,061 (Form 990, fiscal year ended June 2023).",
        False,
    ),
    (
        "q16",
        [
            ("search_organizations", {"query": "Habitat for Humanity", "state": "IA"}),
            ("get_financials", {"ein": "42-1520979"}),
        ],
        "The Altoona Habitat for Humanity affiliate (EIN 42-1520979) had revenue of $1,710,261 in 2022.",
        False,
    ),
    # Must pass: correct answers, one terse and one with a clearly labeled other year.
    (
        "q15",
        [
            ("search_organizations", {"query": "Iowa Public Radio"}),
            ("get_financials", {"ein": "20-4227123", "tax_year": 2024}),
        ],
        "No 2024 figure is available: the 2024 return was filed, but this data has no extracted figures for it yet.",
        True,
    ),
    (
        "q16",
        [
            ("search_organizations", {"query": "Habitat for Humanity", "state": "IA"}),
            ("get_financials", {"ein": "42-1520979"}),
        ],
        "There's no 2022 financial data for the Altoona affiliate (EIN 42-1520979). For reference, its revenue "
        "was $1,710,261 in tax year 2021 and $2,480,061 in tax year 2023.",
        True,
    ),
]


def trajectory(calls) -> list[dict]:
    events = []
    for name, tool_input in calls:
        output, is_error = agent.run_tool(name, tool_input)
        events.append({"type": "tool", "name": name, "input": tool_input, "output": output, "is_error": is_error})
    return events


def main() -> None:
    propublica.CACHE_DIR = run_eval.SNAPSHOT_DIR
    cases = {c["id"]: c for c in run_eval.load_cases()}
    client = anthropic.Anthropic(timeout=180, max_retries=4)
    usage = agent.Usage()
    wrong = inconsistent = 0
    for case_id, calls, answer, must_pass in CHECKS:
        case = cases[case_id]
        events = trajectory(calls)
        verdicts = []
        for _ in range(2):
            verdict, call_usage, _model = grade.judge(client, case["question"], case["rubric"], events, answer)
            usage.add(call_usage)
            verdicts.append(verdict)
        ok = all(v.passed == must_pass for v in verdicts)
        wrong += not ok
        inconsistent += verdicts[0].passed != verdicts[1].passed
        print(
            f"{'OK   ' if ok else 'WRONG'} {case_id} must {'pass' if must_pass else 'fail'}: "
            f"{['pass' if v.passed else 'fail' for v in verdicts]}  {answer[:70]!r}"
        )
        if not ok:
            print(f"      judge: {verdicts[0].reason}")
    print(
        f"\n{len(CHECKS) - wrong}/{len(CHECKS)} judged as they must be; {inconsistent} inconsistent between the two "
        f"tries; cost ${usage.cost(grade.JUDGE_MODEL):.3f}"
    )


if __name__ == "__main__":
    main()
