"""Routes: which model answers each route name, loaded from a versioned YAML file (design decision 7).

The file is validated when the gateway starts; a mistake stops it from starting rather than sending live traffic to
the wrong place. Step 2 uses each route's first candidate; step 3 adds trying the rest in order.
"""

import pathlib
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

import paths

DEFAULT_ROUTES = paths.ROOT / "config" / "routes.yaml"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(Strict):
    provider: Literal["anthropic", "stub"]
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    refusal_fallback: bool = False


class Timeouts(Strict):
    total: float = Field(default=120.0, gt=0)  # seconds, from request to the last byte


class Route(Strict):
    candidates: list[Candidate] = Field(min_length=1)
    max_tokens_ceiling: int = Field(gt=0)
    timeout: Timeouts = Field(default_factory=Timeouts)


def load_routes(path: pathlib.Path = DEFAULT_ROUTES) -> dict[str, Route]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: expected a mapping of route names to routes")
    return {name: Route.model_validate(route) for name, route in raw.items()}
