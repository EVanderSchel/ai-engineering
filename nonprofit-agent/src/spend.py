"""Daily spend cap for the public API: once a UTC day's Claude spending reaches the cap, /ask stops answering
until midnight UTC.

A question's cost is only known when it ends (about $0.005-0.15, capped at agent.MAX_COST per question), so a
question may start only if today's spend plus that worst case still fits under the cap. The cap is therefore
never exceeded; the price is that the last MAX_COST of each day goes unused.

Kept in this process's memory, which is right for one replica (the deployment runs at most one). A restart
starts the day's count again, so the hard backstop is the spend limit on the dedicated Anthropic workspace
whose key the deployment uses: a limit our code can't get wrong.
"""

import datetime
import os
import threading

_lock = threading.Lock()
_day: datetime.date | None = None
_spent = 0.0


def daily_cap() -> float:
    return float(os.environ.get("NONPROFIT_DAILY_SPEND_CAP_USD", "2.00"))


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.UTC).date()


def _roll_over(today: datetime.date) -> None:
    global _day, _spent
    if _day != today:
        _day, _spent = today, 0.0


def can_start(worst_case: float, today: datetime.date | None = None) -> bool:
    """Whether a question that could cost up to worst_case fits in what's left of today's cap."""
    with _lock:
        _roll_over(today or _today())
        return _spent + worst_case <= daily_cap()


def record(cost: float, today: datetime.date | None = None) -> None:
    """Add a finished question's actual cost to today's total."""
    global _spent
    with _lock:
        _roll_over(today or _today())
        _spent += cost


def spent_today(today: datetime.date | None = None) -> float:
    with _lock:
        _roll_over(today or _today())
        return _spent


def seconds_until_reset(now: datetime.datetime | None = None) -> int:
    """Seconds until midnight UTC, when the cap resets (for the Retry-After header)."""
    now = now or datetime.datetime.now(datetime.UTC)
    midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(), datetime.UTC)
    return max(1, int((midnight - now).total_seconds()))


def reset() -> None:
    """Forget today's spending (for tests)."""
    global _day, _spent
    with _lock:
        _day, _spent = None, 0.0
