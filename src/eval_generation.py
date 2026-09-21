"""Measure generation quality: is Claude's synthesized answer faithful to the retrieved
context, and does it actually address the question?

Retrieval eval (eval.py) only checks whether the right chunk was found. It's blind to
generation - a system can retrieve perfectly and still answer badly, by ignoring the
context and answering from memory, or by mixing in unsupported claims. This script closes
that gap with an LLM-as-judge: a second, independent Claude call scores each (question,
context, answer) triple rather than trusting the generating call's own self-report.
"""

import argparse
import json
import os
import pathlib

from embeddings import MODELS
from query import synthesize_answer
from retrieval import retrieve

ROOT = pathlib.Path(__file__).resolve().parent.parent

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


def run_eval(eval_path: pathlib.Path, k: int, limit: int | None, **retrieve_kwargs):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set - generation eval needs it to both generate and judge answers.")
        return

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    cases = json.loads(eval_path.read_text(encoding="utf-8"))
    if limit:
        cases = cases[:limit]

    faithful_count = 0
    relevant_count = 0

    for case in cases:
        question = case["question"]
        hits = retrieve(question, k=k, **retrieve_kwargs)
        answer = synthesize_answer(question, hits)
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
    args = parser.parse_args()

    run_eval(
        args.eval_file,
        args.k,
        args.limit,
        embedding_model=args.embedding_model,
        hybrid=args.hybrid,
        use_reranker=args.rerank,
        rerank_candidates=args.rerank_candidates,
    )
