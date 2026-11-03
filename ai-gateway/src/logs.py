"""Logs as JSON lines: one line per request that says what happened to it, and every other gateway log line in the
same shape (design section 8, step 7 stage 1).

A request's line is written when its response has been sent (for a stream, after the last event, or when the client
went away), so its duration is what the caller waited. It carries the request id the caller sees in `x-request-id`,
and names the team, route, provider, outcome, tokens and cost, never the messages or the answer (decision 15).

Any other line logged while a request is being handled (a warning, an exception) carries the same request id, taken
from a context variable, so a request's lines can be found together:

    {"ts": "2026-11-03T15:04:05.123Z", "level": "info", "logger": "gateway", "message": "request",
     "request_id": "3f2a...", "status": 200, "duration_ms": 412, "team": "research", "route": "fast", ...}
"""

import contextvars
import datetime as dt
import json
import logging
import sys
from decimal import Decimal

REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_CONFIGURED = "_gateway_json_handler"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(timespec="milliseconds")[:-6] + "Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (request_id := REQUEST_ID.get()) is not None:
            line["request_id"] = request_id
        line.update(getattr(record, "fields", {}))
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=_plain, separators=(",", ":"))


def _plain(value):
    return float(value) if isinstance(value, Decimal) else str(value)


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is at the time (a test runner swaps it per test), not the one at setup."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


def configure(level: str = "INFO") -> None:
    """JSON lines on stdout for the "gateway" loggers, once per process. Containers and Kubernetes collect stdout."""
    logger = logging.getLogger("gateway")
    if any(getattr(h, _CONFIGURED, False) for h in logger.handlers):
        return
    handler = _StdoutHandler()
    handler.setFormatter(JsonFormatter())
    setattr(handler, _CONFIGURED, True)
    logger.addHandler(handler)
    logger.setLevel(level.upper())


def request_line(fields: dict) -> None:
    """The one line per request. Fields with no value (a request refused before it reached a provider has no
    provider) are left out rather than written as null."""
    logging.getLogger("gateway").info("request", extra={"fields": {k: v for k, v in fields.items() if v is not None}})
