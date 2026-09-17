"""Retrieve relevant chunks for a question and optionally synthesize an answer with Claude."""

import argparse
import os
import pathlib

import chromadb
from chromadb.utils import embedding_functions

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "chroma_db"
COLLECTION_NAME = "rag_docs"

ANSWER_PROMPT = """Answer the question using only the context below. \
If the context doesn't contain the answer, say so.

Context:
{context}

Question: {question}"""


def retrieve(question: str, k: int):
    client = chromadb.PersistentClient(path=str(DB_DIR))
    embedding_fn = embedding_functions.DefaultEmbeddingFunction()
    collection = client.get_collection(COLLECTION_NAME, embedding_function=embedding_fn)

    results = collection.query(query_texts=[question], n_results=k)
    hits = []
    for doc, meta, distance in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        hits.append({"text": doc, "source": meta["source"], "distance": distance})
    return hits


def synthesize_answer(question: str, hits: list[dict]) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    import anthropic

    context = "\n\n".join(f"[{h['source']}] {h['text']}" for h in hits)
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=500,
        messages=[{"role": "user", "content": ANSWER_PROMPT.format(context=context, question=question)}],
    )
    return response.content[0].text


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="Question to ask the corpus")
    parser.add_argument("-k", type=int, default=3, help="Number of chunks to retrieve")
    args = parser.parse_args()

    hits = retrieve(args.question, args.k)

    print(f"\nTop {len(hits)} retrieved chunks:\n")
    for i, hit in enumerate(hits, 1):
        print(f"{i}. [{hit['source']}] (distance={hit['distance']:.4f})")
        print(f"   {hit['text'][:200]}{'...' if len(hit['text']) > 200 else ''}\n")

    answer = synthesize_answer(args.question, hits)
    if answer:
        print("Answer:\n")
        print(answer)
    else:
        print("Set ANTHROPIC_API_KEY to also generate a synthesized answer from these chunks.")
