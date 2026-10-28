"""Routes: which model answers each route name, loaded from a versioned YAML file (design decision 7).

The file is validated when the gateway starts; a mistake stops it from starting rather than sending live traffic to
the wrong place. Candidates are tried in order (router.py).
"""

import pathlib
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

import paths

DEFAULT_ROUTES = paths.ROOT / "config" / "routes.yaml"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(Strict):
    provider: str  # a configured provider's name; checked when the app starts
    model: str
    # Claude API options: Bedrock's Converse and other providers have no equivalent, so they're refused elsewhere.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    refusal_fallback: bool = False

    @model_validator(mode="after")
    def claude_api_options_only_on_the_claude_api(self):
        if self.provider != "anthropic" and (self.effort or self.refusal_fallback):
            raise ValueError(f"effort and refusal_fallback only apply to provider anthropic, not {self.provider!r}")
        return self


class Timeouts(Strict):
    """Seconds. `first_token` and `attempt` are per candidate, so a slow one leaves time to fall back."""

    total: float = Field(default=120.0, gt=0)  # from request to the last byte, across every attempt
    first_token: float = Field(default=20.0, gt=0)  # a stream's first event from one candidate
    attempt: float = Field(default=60.0, gt=0)  # a whole non-streamed answer from one candidate


class Route(Strict):
    candidates: list[Candidate] = Field(min_length=1)
    max_tokens_ceiling: int = Field(gt=0)
    timeout: Timeouts = Field(default_factory=Timeouts)


def load_routes(path: pathlib.Path = DEFAULT_ROUTES) -> dict[str, Route]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: expected a mapping of route names to routes")
    return {name: Route.model_validate(route) for name, route in raw.items()}
