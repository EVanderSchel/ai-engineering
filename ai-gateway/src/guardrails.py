"""Applying a route's (and team's) guardrail policy to a request and its answer (design decisions 13-15).

Input, in this order:

1. **Personal data** (src/pii.py) in every message: flag it, replace it with placeholders, or refuse the request.
   Redaction happens first, so nothing after it, the injection judge included, sees the values.
2. **Prompt injection** (src/injection.py) in the user's messages (the system prompt is the operator's own): the
   pattern rules, and, if the policy asks, the model judge. With the judge on, the judge decides (on the labelled
   cases it caught everything the patterns did, plus the data-shaped attacks, and cleared the patterns' false alarm);
   the patterns decide only when the judge can't answer.

Output: personal data in the answer, flagged, redacted or withheld (finish reason `content_filter`).

What's recorded (response header, audit log) is which check fired, its action, and the kinds or rule names: never
the text, never a value (decision 15).
"""

from dataclasses import dataclass, field

import injection
import pii
from config import Guardrails
from providers import ProviderError, Usage
from schemas import ChatRequest, Message


@dataclass(frozen=True)
class Finding:
    check: str  # pii_input, injection, pii_output
    action: str  # flagged, redacted, blocked, judge_unavailable
    detail: tuple[str, ...] = ()  # personal-data kinds or pattern rules

    def header(self) -> str:
        return f"{self.check}={self.action}" + (f":{','.join(self.detail)}" if self.detail else "")


class Blocked(Exception):
    """The request may not go on. `status`, `code` and `message` become the caller's error."""

    def __init__(self, status: int, code: str, message: str, findings: list[Finding], judge_usage: Usage | None):
        super().__init__(message)
        self.status, self.code, self.message, self.findings, self.judge_usage = (
            status,
            code,
            message,
            findings,
            judge_usage,
        )


@dataclass
class InputResult:
    request: ChatRequest  # with personal data replaced, if the policy redacts
    findings: list[Finding] = field(default_factory=list)
    judge_usage: Usage | None = None  # tokens the judge used, to be charged to the team


def _redact_message(message: Message, kinds) -> tuple[Message, set[str]]:
    found: set[str] = set()
    if isinstance(message.content, str):
        content, findings = pii.redact(message.content, kinds)
        found |= {f.kind for f in findings}
        return message.model_copy(update={"content": content}), found
    parts = []
    for part in message.content:
        text, findings = pii.redact(part.text, kinds)
        found |= {f.kind for f in findings}
        parts.append(part.model_copy(update={"text": text}))
    return message.model_copy(update={"content": parts}), found


def _kinds_in(messages: list[Message], kinds) -> list[str]:
    found = {f.kind for m in messages for f in pii.find(m.text(), kinds)}
    return [k for k in pii.KINDS if k in found]


async def check_input(request: ChatRequest, policy: Guardrails, judge: injection.Judge | None) -> InputResult:
    """The request as it may go on (possibly redacted), and what was found. Raises Blocked."""
    result = InputResult(request)
    if policy.pii_input != "off":
        kinds = _kinds_in(request.messages, policy.pii_kinds)
        if kinds:
            if policy.pii_input == "block":
                finding = Finding("pii_input", "blocked", tuple(kinds))
                raise Blocked(
                    400, "personal_data_blocked",
                    f"the request contains personal data this route doesn't accept ({', '.join(kinds)}); remove it "
                    "and try again", [finding], None,
                )  # fmt: skip
            if policy.pii_input == "redact":
                messages = [_redact_message(m, policy.pii_kinds)[0] for m in request.messages]
                result.request = request.model_copy(update={"messages": messages})
                result.findings.append(Finding("pii_input", "redacted", tuple(kinds)))
            else:
                result.findings.append(Finding("pii_input", "flagged", tuple(kinds)))

    if policy.injection != "off":
        text = "\n\n".join(m.text() for m in result.request.messages if m.role == "user")
        rules = injection.screen_patterns(text)
        suspected = bool(rules)
        if policy.injection_judge and judge is not None:
            try:
                verdict = await judge.judge(text)
                result.judge_usage = verdict.usage
                answer = verdict.injection
            except ProviderError:
                answer = None
            if answer is None:  # the judge couldn't answer: the policy says whether to go on, on the patterns alone
                if policy.judge_unavailable == "block":
                    finding = Finding("injection", "judge_unavailable")
                    raise Blocked(
                        503, "screen_unavailable", "the injection screen is unavailable right now; try again shortly",
                        [*result.findings, finding], result.judge_usage,
                    )  # fmt: skip
                result.findings.append(Finding("injection", "judge_unavailable"))
            else:
                suspected = answer
                if answer and not rules:
                    rules = ("judge",)
        if suspected:
            action = "blocked" if policy.injection == "block" else "flagged"
            finding = Finding("injection", action, rules)
            if action == "blocked":
                raise Blocked(
                    400, "injection_blocked",
                    "the request looks like it contains instructions aimed at the AI inside the material it should "
                    "work on (prompt injection), so this route won't send it",
                    [*result.findings, finding], result.judge_usage,
                )  # fmt: skip
            result.findings.append(finding)
    return result


@dataclass(frozen=True)
class OutputResult:
    text: str
    withheld: bool  # the answer is replaced by nothing (finish reason content_filter)
    findings: list[Finding]


def check_output(text: str, policy: Guardrails) -> OutputResult:
    if policy.pii_output == "off":
        return OutputResult(text, False, [])
    redacted, findings = pii.redact(text, policy.pii_kinds)
    if not findings:
        return OutputResult(text, False, [])
    kinds = tuple(k for k in pii.KINDS if k in {f.kind for f in findings})
    if policy.pii_output == "block":
        return OutputResult("", True, [Finding("pii_output", "blocked", kinds)])
    if policy.pii_output == "redact":
        return OutputResult(redacted, False, [Finding("pii_output", "redacted", kinds)])
    return OutputResult(text, False, [Finding("pii_output", "flagged", kinds)])


def header(findings: list[Finding]) -> str:
    """For the x-gateway-guardrails response header: e.g. "pii_input=redacted:email;injection=flagged:judge"."""
    return ";".join(f.header() for f in findings)
