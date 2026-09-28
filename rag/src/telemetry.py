"""Per-request measurements: timings, token usage, and dollar cost, logged as one JSON line per request."""

import json
import logging
import time
from contextlib import contextmanager

# USD per million tokens (input, output). Check https://www.anthropic.com/pricing when
# changing models; last updated 2026-09-25.
PRICES_PER_MTOK = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

logger = logging.getLogger("rag.requests")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Dollar cost of one call, or None for a model missing from the price table."""
    if model not in PRICES_PER_MTOK:
        return None
    input_price, output_price = PRICES_PER_MTOK[model]
    return round((input_tokens * input_price + output_tokens * output_price) / 1_000_000, 6)


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


@contextmanager
def timed(record: dict, key: str):
    """Store how long the block took, in milliseconds, as record[key]."""
    start = time.perf_counter()
    try:
        yield
    finally:
        record[key] = elapsed_ms(start)


def usage_fields(model: str, message) -> dict:
    usage = message.usage
    return {
        "model": model,
        "stop_reason": message.stop_reason,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cost_usd(model, usage.input_tokens, usage.output_tokens),
    }


def log_request(record: dict) -> None:
    logger.info(json.dumps(record))
