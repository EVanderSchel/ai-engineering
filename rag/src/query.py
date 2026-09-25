"""Retrieve relevant chunks for a question and optionally synthesize an answer with Claude."""

import argparse
import os

from embeddings import MODELS
from retrieval import retrieve

ANSWER_PROMPT = """Answer the question using only the context below. \
If the context doesn't contain the answer, say so.

Context:
{context}

Question: {question}"""


ANSWER_MODEL = "claude-sonnet-5"
ANSWER_MAX_TOKENS = 500


def build_messages(question: str, hits: list[dict]) -> list[dict]:
    context = "\n\n".join(f"[{h['source']}] {h['text']}" for h in hits)
    return [{"role": "user", "content": ANSWER_PROMPT.format(context=context, question=question)}]


def synthesize_answer(question: str, hits: list[dict], client=None) -> str | None:
    """Return Claude's answer, or None if no client was given and ANTHROPIC_API_KEY isn't set."""
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None

        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=ANSWER_MODEL,
        max_tokens=ANSWER_MAX_TOKENS,
        messages=build_messages(question, hits),
    )
    return response.content[0].text


def stream_answer(client, question: str, hits: list[dict]):
    """Yield the answer piece by piece as Claude generates it, then yield the final Message.

    The final item is the complete Message object (with stop_reason and token usage), so the
    caller can tell text chunks (str) apart from the end-of-stream summary.
    """
    with client.messages.stream(
        model=ANSWER_MODEL,
        max_tokens=ANSWER_MAX_TOKENS,
        messages=build_messages(question, hits),
    ) as stream:
        for text in stream.text_stream:
            yield text
        yield stream.get_final_message()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="Question to ask the corpus")
    parser.add_argument("-k", type=int, default=3, help="Number of chunks to retrieve")
    parser.add_argument("--embedding-model", choices=list(MODELS), default="default")
    parser.add_argument("--hybrid", action="store_true", help="Combine vector search with BM25 keyword search")
    parser.add_argument("--rerank", action="store_true", help="Rerank candidates with a cross-encoder")
    parser.add_argument(
        "--rerank-candidates", type=int, default=10, help="How many candidates to rerank (only with --rerank)"
    )
    args = parser.parse_args()

    hits = retrieve(
        args.question,
        k=args.k,
        embedding_model=args.embedding_model,
        hybrid=args.hybrid,
        use_reranker=args.rerank,
        rerank_candidates=args.rerank_candidates,
    )

    print(f"\nTop {len(hits)} retrieved chunks:\n")
    for i, hit in enumerate(hits, 1):
        print(f"{i}. [{hit['source']}]")
        print(f"   {hit['text'][:200]}{'...' if len(hit['text']) > 200 else ''}\n")

    answer = synthesize_answer(args.question, hits)
    if answer:
        print("Answer:\n")
        print(answer)
    else:
        print("Set ANTHROPIC_API_KEY to also generate a synthesized answer from these chunks.")
