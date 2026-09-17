"""Text chunking utilities."""


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping fixed-size chunks.

    Splits on paragraph boundaries where possible so chunks don't cut
    mid-sentence more than necessary, then falls back to raw character
    slicing with overlap for paragraphs longer than chunk_size.
    """
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 1 <= chunk_size:
            current = f"{current}\n{para}".strip()
            continue

        if current:
            chunks.append(current)

        if len(para) <= chunk_size:
            current = para
        else:
            start = 0
            while start < len(para):
                end = start + chunk_size
                chunks.append(para[start:end])
                start = end - overlap
            current = ""

    if current:
        chunks.append(current)

    return chunks
