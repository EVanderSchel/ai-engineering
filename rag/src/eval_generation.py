"""Measure generation quality: is Claude's synthesized answer faithful to the retrieved
context, and does it actually address the question?

Retrieval eval (eval.py) only checks whether the right chunk was found. It's blind to
generation - a system can retrieve perfectly and still answer badly, by ignoring the
context and answering from memory, or by mixing in unsupported claims. This script closes
that gap with an LLM-as-judge: a second, independent Claude call scores each (question,
context, answer) triple rather than trusting the generating call's own self-report.

Each run is appended to data/generation_eval_history.jsonl along with the exact prompt version
(and its fingerprint) that produced the answers, so prompt versions can be compared over time:

    python src/eval_generation.py --prompt-version v2 --eval-file data/eval_set_current_events.json
    python src/eval_generation.py --history
"""

import argparse
import datetime
import json
import os
import pathlib

import prompts
import telemetry
from embeddings import MODELS
from query import ANSWER_MODEL, generate
from retrieval import retrieve

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "generation_eval_history.jsonl"

# A small, cheap model is deliberately used for judging - grading a claim against
# context is a much easier task than generating the answer in the first place, and
# using a different model than the one being judged avoids the judge favoring its
# own generation style.
JUDGE_MODEL = "claude-haiku-4-5-20251001"

JUDGE_PROMPT = """You are evaluating a RAG system's generated answer against the context it was given.

Question: {question}

Retrieved context:
{context}

Generated answer:
{answer}

Score the answer on two criteria:
1. faithful: true if every factual claim in the answer is directly supported by the retrieved \
context (no invented facts, no outside knowledge), false otherwise. An answer that correctly \
says the context doesn't contain the answer is faithful.
2. relevant: true if the answer actually addresses the question asked, false otherwise.

Respond with ONLY a JSON object, no other text, in this exact format:
{{"faithful": true, "relevant": true, "reasoning": "one sentence explanation"}}"""


def judge_answer(client, question: str, hits: list[dict], answer: str) -> dict:
    context = "\n\n".join(f"[{h['source']}] {h['text']}" for h in hits)
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(context=context, question=question, answer=answer)}],
    )
    raw = response.content[0].text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def run_eval(
    eval_path: pathlib.Path, k: int, limit: int | None, prompt: prompts.Prompt, **retrieve_kwargs
) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set - generation eval needs it to both generate and judge answers.")
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    cases = json.loads(eval_path.read_text(encoding="utf-8"))
    if limit:
        cases = cases[:limit]
    print(f"Prompt {prompt.id} (sha256 {prompt.sha256[:12]}), {len(cases)} cases\n")

    faithful_count = 0
    relevant_count = 0
    output_tokens = 0
    cost = 0.0

    for case in cases:
        question = case["question"]
        hits = retrieve(question, k=k, **retrieve_kwargs)
        message = generate(client, question, hits, prompt)
        answer = message.content[0].text
        output_tokens += message.usage.output_tokens
        cost += telemetry.cost_usd(ANSWER_MODEL, message.usage.input_tokens, message.usage.output_tokens) or 0
        verdict = judge_answer(client, question, hits, answer)

        faithful_count += verdict["faithful"]
        relevant_count += verdict["relevant"]

        tags = "".join(
            [
                "[faithful]" if verdict["faithful"] else "[UNFAITHFUL]",
                "[relevant]" if verdict["relevant"] else "[IRRELEVANT]",
            ]
        )
        print(f"{tags} q={question!r}")
        print(f"    answer: {answer[:150]}{'...' if len(answer) > 150 else ''}")
        print(f"    judge:  {verdict['reasoning']}\n")

    n = len(cases)
    print(f"Faithfulness: {faithful_count}/{n} ({faithful_count / n:.0%})")
    print(f"Relevance:    {relevant_count}/{n} ({relevant_count / n:.0%})")
    print(f"Avg output tokens: {output_tokens / n:.0f}   Generation cost: ${cost:.4f}")
    return {
        "faithfulness": round(faithful_count / n, 4),
        "relevance": round(relevant_count / n, 4),
        "avg_output_tokens": round(output_tokens / n, 1),
        "generation_cost_usd": round(cost, 6),
        "n": n,
    }


def record(result: dict, prompt: prompts.Prompt, eval_path: pathlib.Path, settings: dict) -> None:
    """Append one run to the history file, tagged with exactly what produced it."""
    entry = {
        "date": datetime.date.today().isoformat(),
        "prompt": prompt.id,
        "prompt_sha256": prompt.sha256[:12],
        "model": ANSWER_MODEL,
        "judge_model": JUDGE_MODEL,
        "eval_file": eval_path.resolve().relative_to(ROOT).as_posix(),
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
    header = f"{'date':<11} {'prompt':<11} {'eval file':<36} {'n':>3} {'faithful':>9} {'relevant':>9} {'out tok':>8} {'cost $':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['date']:<11} {r['prompt']:<11} {r['eval_file'].removeprefix('data/'):<36} {r['n']:>3}"
            f" {r['faithfulness']:>9.0%} {r['relevance']:>9.0%} {r['avg_output_tokens']:>8.0f} {r['generation_cost_usd']:>8.4f}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eval-file",
        type=pathlib.Path,
        default=ROOT / "data" / "eval_set.json",
        help="JSON file of {question, expected_source} pairs (default: data/eval_set.json)",
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
    parser.add_argument("--no-record", action="store_true", help="Don't append this run to the history file")
    parser.add_argument("--history", action="store_true", help="Print all recorded runs and exit")
    args = parser.parse_args()

    if args.history:
        print_history()
        raise SystemExit

    prompt = prompts.load("answer", args.prompt_version)
    settings = {
        "k": args.k,
        "embedding_model": args.embedding_model,
        "hybrid": args.hybrid,
        "rerank": args.rerank,
    }
    result = run_eval(
        args.eval_file,
        args.k,
        args.limit,
        prompt,
        embedding_model=args.embedding_model,
        hybrid=args.hybrid,
        use_reranker=args.rerank,
        rerank_candidates=args.rerank_candidates,
    )
    # Partial runs (--limit) aren't comparable with full ones, so they're never recorded.
    if result and not args.no_record and not args.limit:
        record(result, prompt, args.eval_file, settings)
