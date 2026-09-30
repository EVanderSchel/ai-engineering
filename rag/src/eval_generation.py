"""Measure generation quality: is Claude's synthesized answer faithful to the retrieved
context, does it address the question, does it cite its sources, and does it admit when the
context doesn't contain the answer?

Retrieval eval (eval.py) only checks whether the right chunk was found. It's blind to
generation - a system can retrieve perfectly and still answer badly, by ignoring the
context and answering from memory, or by mixing in unsupported claims. This script closes
that gap with an LLM-as-judge: a second, independent Claude call scores each (question,
context, answer) triple rather than trusting the generating call's own self-report.

Eval files can mix two kinds of case. Answerable cases (the default) check faithfulness,
relevance, citations, and that the model didn't wrongly refuse. Cases marked
"answerable": false ask about things the corpus doesn't cover, and check that the model says
so instead of guessing.

Each full run is appended to data/generation_eval_history.jsonl along with the exact prompt
version (and its fingerprint) that produced the answers, so versions can be compared:

    python src/eval_generation.py --prompt-version v2 \\
        --eval-file data/eval_set_current_events.json data/eval_set_unanswerable.json
    python src/eval_generation.py --history
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import time

import prompts
import telemetry
from embeddings import MODELS
from query import ANSWER_MODEL, answer_text, generate
from retrieval import retrieve

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "generation_eval_history.jsonl"

# A small, cheap model is deliberately used for judging - grading a claim against
# context is a much easier task than generating the answer in the first place, and
# using a different model than the one being judged avoids the judge favoring its
# own generation style.
JUDGE_MODEL = "claude-haiku-4-5-20251001"
# Bump when JUDGE_PROMPT changes: scores from different judge versions aren't comparable.
JUDGE_VERSION = "judge/v3"

JUDGE_PROMPT = """You are evaluating a RAG system's generated answer against the context it was given.

Question: {question}

Retrieved context:
{context}

Generated answer:
{answer}

Score the answer on four criteria:
1. faithful: true if every factual claim in the answer is directly supported by the retrieved \
context (no invented facts, no outside knowledge), false otherwise. An answer that correctly \
says the context doesn't contain the answer is faithful.
2. relevant: true if the answer actually addresses the question asked, false otherwise.
3. declined: true if the answer states that the context does not contain the answer (or not \
enough information to answer it) instead of answering, false if it gives an answer.
4. misled: {misled_criterion}

Respond with ONLY a JSON object, no other text, in this exact format:
{{"faithful": true, "relevant": true, "declined": false, "misled": false, "reasoning": "one sentence explanation"}}"""

# Trap cases: the context holds a related fact that looks like the answer but isn't. A faithfulness
# check can't catch presenting it as the answer, since the fact itself is in the context.
TRAP_CRITERION = (
    "This question is a trap: {trap} Set misled to true if the answer presents that related "
    "information as the answer to the question. Set it to false if the answer makes clear that the "
    "context doesn't answer the question, even if it also mentions the related information."
)
NO_TRAP_CRITERION = "Always false for this question."

CITATION = re.compile(r"\[([^\[\]]+?\.txt)\]")


def judge_answer(client, question: str, hits: list[dict], answer: str, trap: str | None = None) -> dict:
    context = "\n\n".join(f"[{h['source']}] {h['text']}" for h in hits)
    criterion = TRAP_CRITERION.format(trap=trap) if trap else NO_TRAP_CRITERION
    prompt = JUDGE_PROMPT.format(context=context, question=question, answer=answer, misled_criterion=criterion)
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = answer_text(response).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def check_citations(answer: str, hits: list[dict]) -> dict:
    """Checked with code, not the judge: which [file.txt] citations appear, and are they real?"""
    cited = set(CITATION.findall(answer))
    retrieved = {h["source"] for h in hits}
    return {"cited": bool(cited & retrieved), "invalid_citations": sorted(cited - retrieved)}


def summarize(results: list[dict]) -> dict:
    """Turn per-case results into the run's metrics (fractions 0-1, or None if no cases apply).

    - faithfulness: every case, answerable or not
    - relevance, false_refusal_rate: answerable cases
    - citation_rate: answerable cases the model actually answered, citing at least one retrieved
      source and no source that wasn't retrieved
    - correct_refusal_rate: unanswerable cases (not traps) where the model said the context lacks the answer
    - trap_resistance: trap cases where the model did NOT present the look-alike fact as the answer
    """

    def rate(cases, predicate):
        return round(sum(map(predicate, cases)) / len(cases), 4) if cases else None

    answerable = [r for r in results if r["answerable"]]
    unanswerable = [r for r in results if not r["answerable"] and not r.get("trap")]
    traps = [r for r in results if r.get("trap")]
    answered = [r for r in answerable if not r["declined"]]
    return {
        "faithfulness": rate(results, lambda r: r["faithful"]),
        "relevance": rate(answerable, lambda r: r["relevant"]),
        "citation_rate": rate(answered, lambda r: r["cited"] and not r["invalid_citations"]),
        "false_refusal_rate": rate(answerable, lambda r: r["declined"]),
        "correct_refusal_rate": rate(unanswerable, lambda r: r["declined"]),
        "trap_resistance": rate(traps, lambda r: not r["misled"]),
        "avg_output_tokens": round(sum(r["output_tokens"] for r in results) / len(results), 1),
        "generation_cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        # Median, not mean: one slow API call shouldn't dominate the comparison.
        "median_generation_ms": round(sorted(r.get("generation_ms", 0) for r in results)[len(results) // 2]),
        "n": len(results),
        "n_unanswerable": len(unanswerable),
        "n_traps": len(traps),
    }


def run_eval(
    cases: list[dict], k: int, prompt: prompts.Prompt, answer_model: str = ANSWER_MODEL, **retrieve_kwargs
) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set - generation eval needs it to both generate and judge answers.")
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    print(f"Prompt {prompt.id} (sha256 {prompt.sha256[:12]}), answer model {answer_model}, {len(cases)} cases\n")

    results = []
    for case in cases:
        question = case["question"]
        answerable = case.get("answerable", True)
        hits = retrieve(question, k=k, **retrieve_kwargs)
        start = time.perf_counter()
        message = generate(client, question, hits, prompt, model=answer_model)
        generation_ms = (time.perf_counter() - start) * 1000
        answer = answer_text(message)
        verdict = judge_answer(client, question, hits, answer, case.get("trap"))
        result = {
            "answerable": answerable,
            "trap": bool(case.get("trap")),
            "misled": bool(verdict.get("misled", False)),
            "faithful": bool(verdict["faithful"]),
            "relevant": bool(verdict["relevant"]),
            "declined": bool(verdict["declined"]),
            **check_citations(answer, hits),
            "output_tokens": message.usage.output_tokens,
            "cost_usd": telemetry.cost_usd(answer_model, message.usage.input_tokens, message.usage.output_tokens) or 0,
            "generation_ms": generation_ms,
        }
        results.append(result)

        if answerable:
            tags = [
                "faithful" if result["faithful"] else "UNFAITHFUL",
                "relevant" if result["relevant"] else "IRRELEVANT",
                "WRONGLY-DECLINED" if result["declined"] else ("cited" if result["cited"] else "no-citation"),
            ]
        elif result["trap"]:
            tags = [
                "faithful" if result["faithful"] else "UNFAITHFUL",
                "MISLED-BY-TRAP" if result["misled"] else "resisted-trap",
            ]
        else:
            tags = [
                "faithful" if result["faithful"] else "UNFAITHFUL",
                "declined-correctly" if result["declined"] else "ANSWERED-UNANSWERABLE",
            ]
        if result["invalid_citations"]:
            tags.append(f"INVALID-CITATION {result['invalid_citations']}")
        print(f"[{']['.join(tags)}] q={question!r}")
        print(f"    answer: {answer[:150]}{'...' if len(answer) > 150 else ''}")
        print(f"    judge:  {verdict['reasoning']}\n")

    metrics = summarize(results)

    def pct(value):
        return "n/a" if value is None else f"{value:.0%}"

    print(f"Faithfulness:          {pct(metrics['faithfulness'])}")
    print(f"Relevance:             {pct(metrics['relevance'])}")
    print(f"Citation rate:         {pct(metrics['citation_rate'])}")
    print(f"False refusals:        {pct(metrics['false_refusal_rate'])}  (lower is better)")
    print(f"Correct refusals:      {pct(metrics['correct_refusal_rate'])}")
    print(f"Trap resistance:       {pct(metrics['trap_resistance'])}")
    print(f"Avg output tokens:     {metrics['avg_output_tokens']:.0f}")
    print(f"Generation cost:       ${metrics['generation_cost_usd']:.4f}")
    print(f"Median generation:     {metrics['median_generation_ms']} ms")
    return metrics


def record(
    result: dict, prompt: prompts.Prompt, eval_paths: list[pathlib.Path], settings: dict, answer_model: str = ANSWER_MODEL
) -> None:
    """Append one run to the history file, tagged with exactly what produced it."""
    entry = {
        "date": datetime.date.today().isoformat(),
        "prompt": prompt.id,
        "prompt_sha256": prompt.sha256[:12],
        "model": answer_model,
        "judge_model": JUDGE_MODEL,
        "judge": JUDGE_VERSION,
        "eval_file": " + ".join(p.resolve().relative_to(ROOT).as_posix() for p in eval_paths),
        **settings,
        **result,
    }
    with HISTORY_FILE.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"\nRecorded in {HISTORY_FILE.relative_to(ROOT).as_posix()}")


def print_history() -> None:
    if not HISTORY_FILE.exists():
        print("No recorded runs yet.")
        return
    rows = [json.loads(line) for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]

    def pct(row, key):
        value = row.get(key)
        return "-" if value is None else f"{value:.0%}"

    columns = ["date", "prompt", "model", "judge", "n", "faithful", "relevant", "cited", "false ref", "correct ref",
               "trap ok", "out tok", "cost $", "p50 ms"]
    widths = [10, 10, 16, 8, 3, 8, 8, 6, 9, 11, 7, 7, 7, 6]
    text_columns = 4
    print(" ".join(c.ljust(w) if i < text_columns else c.rjust(w) for i, (c, w) in enumerate(zip(columns, widths))))
    print("-" * (sum(widths) + len(widths) - 1))
    for r in rows:
        cells = [
            r["date"], r["prompt"], r.get("model", "claude-sonnet-5"), r.get("judge", "judge/v1"), str(r["n"]),
            pct(r, "faithfulness"), pct(r, "relevance"), pct(r, "citation_rate"),
            pct(r, "false_refusal_rate"), pct(r, "correct_refusal_rate"), pct(r, "trap_resistance"),
            f"{r['avg_output_tokens']:.0f}", f"{r['generation_cost_usd']:.4f}", str(r.get("median_generation_ms", "-")),
        ]
        print(" ".join(c.ljust(w) if i < text_columns else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths))))
        print(f"{'':11}eval: {r['eval_file']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--eval-file",
        type=pathlib.Path,
        nargs="+",
        default=[ROOT / "data" / "eval_set.json"],
        help="One or more JSON files of cases (default: data/eval_set.json)",
    )
    parser.add_argument("-k", type=int, default=3, help="Number of chunks to retrieve per question")
    parser.add_argument("--embedding-model", choices=list(MODELS), default="default")
    parser.add_argument("--hybrid", action="store_true", help="Combine vector search with BM25 keyword search")
    parser.add_argument("--rerank", action="store_true", help="Rerank candidates with a cross-encoder")
    parser.add_argument(
        "--rerank-candidates", type=int, default=10, help="How many candidates to rerank (only with --rerank)"
    )
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N cases (each case costs 2 API calls)")
    parser.add_argument(
        "--prompt-version", default=None, help="Answer prompt version to test, e.g. v2 (default: the manifest's active one)"
    )
    parser.add_argument(
        "--answer-model", default=ANSWER_MODEL, help=f"Model that generates the answers (default: {ANSWER_MODEL})"
    )
    parser.add_argument("--no-record", action="store_true", help="Don't append this run to the history file")
    parser.add_argument("--history", action="store_true", help="Print all recorded runs and exit")
    args = parser.parse_args()

    if args.history:
        print_history()
        raise SystemExit

    cases = [case for path in args.eval_file for case in json.loads(path.read_text(encoding="utf-8"))]
    if args.limit:
        cases = cases[: args.limit]
    prompt = prompts.load("answer", args.prompt_version)
    settings = {
        "k": args.k,
        "embedding_model": args.embedding_model,
        "hybrid": args.hybrid,
        "rerank": args.rerank,
    }
    result = run_eval(
        cases,
        args.k,
        prompt,
        answer_model=args.answer_model,
        embedding_model=args.embedding_model,
        hybrid=args.hybrid,
        use_reranker=args.rerank,
        rerank_candidates=args.rerank_candidates,
    )
    # Partial runs (--limit) aren't comparable with full ones, so they're never recorded.
    if result and not args.no_record and not args.limit:
        record(result, prompt, args.eval_file, settings, args.answer_model)
