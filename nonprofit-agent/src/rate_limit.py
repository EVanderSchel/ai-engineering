"""Per-caller request limits for /ask: a leaked or misused key can't ask questions in bulk.

Each caller gets NONPROFIT_RATE_LIMIT_PER_MINUTE questions per rolling minute (default 5; a question costs
about $0.005-0.15; 0 turns the limit off). Over the limit, the request is rejected with 429 and a
Retry-After header before Claude is called.

Who counts as a caller: when NONPROFIT_API_KEY is set there is exactly one valid key, and every request has
already been checked against it, so all authenticated requests share that key's limit. When it isn't set
(local development), a client could send any made-up key, so callers are counted by IP address.

Counts are kept in this process's memory, which is right for a single replica. (The same design as the rag
and irs-990-extraction projects' rate_limit.py; each project keeps its own copy.)
"""

import os
import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 60.0

_lock = threading.Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)


def limit_per_minute() -> int:
    return int(os.environ.get("NONPROFIT_RATE_LIMIT_PER_MINUTE", "5"))


def caller_id(authenticated: bool, client_ip: str | None) -> str:
    """Who to count requests against (see the module docstring)."""
    if authenticated:
        return "api-key"
    return f"ip:{client_ip or 'unknown'}"


def check(caller: str, now: float | None = None) -> float | None:
    """Record a request from caller. None if it's allowed, otherwise the seconds until the caller's
    oldest counted request leaves the window (for the Retry-After header)."""
    limit = limit_per_minute()
    if limit <= 0:
        return None
    now = time.monotonic() if now is None else now
    with _lock:
        timestamps = _requests[caller]
        while timestamps and now - timestamps[0] >= WINDOW_SECONDS:
            timestamps.popleft()
        if len(timestamps) >= limit:
            return WINDOW_SECONDS - (now - timestamps[0])
        timestamps.append(now)
        return None


def reset() -> None:
    """Forget all counts (for tests)."""
    with _lock:
        _requests.clear()
