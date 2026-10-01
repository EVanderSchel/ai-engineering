"""Per-caller request limits for the /ask endpoints: a leaked or misused key can't run up unlimited cost.

Each caller (its API key, or its IP address when no key is configured) gets RAG_RATE_LIMIT_PER_MINUTE
requests per rolling minute (default 30; 0 turns the limit off). Over the limit, the request is
rejected with 429 and a Retry-After header before any search or Claude call happens.

Counts are kept in this process's memory. That's correct while the app runs as a single replica
(max-replicas 1); with several replicas each would count separately, and a shared store such as
Redis would be needed instead.
"""

import hashlib
import os
import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 60.0

_lock = threading.Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)


def limit_per_minute() -> int:
    return int(os.environ.get("RAG_RATE_LIMIT_PER_MINUTE", "30"))


def caller_id(api_key: str | None, client_ip: str | None) -> str:
    """Who to count requests against. Keys are hashed so the raw key is never held as a dict key."""
    if api_key:
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return f"ip:{client_ip or 'unknown'}"


def check(caller: str, now: float | None = None) -> float | None:
    """Record a request from caller. Returns None if it's allowed, otherwise the number of seconds
    until the caller's oldest counted request leaves the window (for the Retry-After header)."""
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
