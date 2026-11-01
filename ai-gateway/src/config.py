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


PiiAction = Literal["off", "flag", "redact", "block"]
InjectionAction = Literal["off", "flag", "block"]
STRICTNESS = {"off": 0, "flag": 1, "redact": 2, "block": 3}
PII_KINDS = ("ssn", "card", "bank", "email", "phone")


class Guardrails(Strict):
    """What a route (or a team) checks, and what it does about what it finds (design decision 13).

    `flag` lets the request through and records it; `redact` (personal data only) replaces the values with
    placeholders like [EMAIL]; `block` refuses the request (input) or withholds the answer (output).
    """

    pii_input: PiiAction = "off"
    pii_output: PiiAction = "off"
    pii_kinds: tuple[Literal["ssn", "card", "bank", "email", "phone"], ...] = PII_KINDS
    injection: InjectionAction = "off"
    injection_judge: bool = False  # also ask the model judge (paid, about a second), not just the patterns
    judge_unavailable: Literal["allow", "block"] = "allow"  # when the judge can't answer: let through, or refuse

    def strictest(self, other: Guardrails) -> Guardrails:
        """Each setting at the stricter of the two: a team can tighten its routes' guardrails, never loosen them.

        Only the settings `other` was given count: a team that sets `injection=block` and nothing else leaves the
        route's other settings as they are (its unset fields' defaults, like every personal-data kind, don't apply).
        """
        chosen = other.model_fields_set

        def stricter(field: str) -> str:
            mine, theirs = getattr(self, field), getattr(other, field)
            if field not in chosen:
                return mine
            return mine if STRICTNESS[mine] >= STRICTNESS[theirs] else theirs

        kinds = self.pii_kinds
        if "pii_kinds" in chosen:
            kinds = tuple(k for k in PII_KINDS if k in self.pii_kinds or k in other.pii_kinds)
        unavailable = self.judge_unavailable
        if "judge_unavailable" in chosen and other.judge_unavailable == "block":
            unavailable = "block"
        return Guardrails(
            pii_input=stricter("pii_input"),
            pii_output=stricter("pii_output"),
            pii_kinds=kinds,
            injection=stricter("injection"),
            injection_judge=self.injection_judge or ("injection_judge" in chosen and other.injection_judge),
            judge_unavailable=unavailable,
        )

    @property
    def buffers_output(self) -> bool:
        """A stream must be held back and checked before it's sent: a shown answer can't be taken back (decision 14)."""
        return self.pii_output in ("redact", "block")

    @property
    def any(self) -> bool:
        return self.pii_input != "off" or self.pii_output != "off" or self.injection != "off"

    @model_validator(mode="after")
    def judge_needs_injection_screening(self):
        if self.injection_judge and self.injection == "off":
            raise ValueError("injection_judge needs injection set to flag or block")
        return self


class Route(Strict):
    candidates: list[Candidate] = Field(min_length=1)
    max_tokens_ceiling: int = Field(gt=0)
    timeout: Timeouts = Field(default_factory=Timeouts)
    guardrails: Guardrails = Field(default_factory=Guardrails)


def load_routes(path: pathlib.Path = DEFAULT_ROUTES) -> dict[str, Route]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: expected a mapping of route names to routes")
    return {name: Route.model_validate(route) for name, route in raw.items()}
