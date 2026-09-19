"""Measure retrieval quality against a labeled set of (question, expected_source) pairs."""

import argparse
import json
import pathlib

from embeddings import MODELS
from retrieval import retrieve

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_eval(eval_path: pathlib.Path, k: int, **retrieve_kwargs):
    cases = json.loads(eval_path.read_text(encoding="utf-8"))

    hits_at_1 = 0
    hits_at_k = 0
    reciprocal_ranks = []

    for case in cases:
        question = case["question"]
        expected = case["expected_source"]
        sources = [hit["source"] for hit in retrieve(question, k=k, **retrieve_kwargs)]

        rank = sources.index(expected) + 1 if expected in sources else None
        reciprocal_ranks.append(1 / rank if rank else 0)
        if rank == 1:
            hits_at_1 += 1
        if rank is not None:
            hits_at_k += 1

        status = "OK" if rank else "MISS"
        print(f"[{status}] rank={rank or '-'} expected={expected!r} q={question!r}")

    n = len(cases)
    mrr = sum(reciprocal_ranks) / n
    print(f"\nHit@1: {hits_at_1}/{n} ({hits_at_1 / n:.0%})")
    print(f"Hit@{k}: {hits_at_k}/{n} ({hits_at_k / n:.0%})")
    print(f"MRR: {mrr:.2f}")


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
    args = parser.parse_args()

    run_eval(
        args.eval_file,
        args.k,
        embedding_model=args.embedding_model,
        hybrid=args.hybrid,
        use_reranker=args.rerank,
        rerank_candidates=args.rerank_candidates,
    )
