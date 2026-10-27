"""OpenAI chat messages to the provider-neutral request, and Claude's stop reasons to OpenAI's finish reasons.

OpenAI lets system messages appear anywhere and a conversation start or end with any role; Claude takes one system
prompt and a conversation that starts with the user and (on current models) ends with the user. Rather than guess
what a misplaced message meant, the gateway refuses those shapes with a 400 that says why (decision 6).
"""

from providers import FinishReason, ProviderRequest, Turn
from schemas import ChatRequest


class InvalidMessages(ValueError):
    pass


def to_provider_request(request: ChatRequest, model: str, max_tokens: int, **options) -> ProviderRequest:
    messages = list(request.messages)
    system_parts = []
    while messages and messages[0].role in ("system", "developer"):
        system_parts.append(messages.pop(0).text())
    if any(m.role in ("system", "developer") for m in messages):
        raise InvalidMessages("system and developer messages are only supported at the start of the conversation")
    if not messages:
        raise InvalidMessages("the conversation needs at least one user message")
    if messages[0].role != "user":
        raise InvalidMessages("the first message after the system prompt must be from the user")
    if messages[-1].role != "user":
        raise InvalidMessages("the last message must be from the user (prefilling the answer isn't supported)")

    turns: list[Turn] = []
    for message in messages:
        if turns and turns[-1].role == message.role:
            # Consecutive messages from the same role become one turn, as Claude expects alternating roles.
            turns[-1] = Turn(message.role, f"{turns[-1].text}\n\n{message.text()}")
        else:
            turns.append(Turn(message.role, message.text()))
    if any(not turn.text.strip() for turn in turns):
        raise InvalidMessages("messages must not be empty")

    return ProviderRequest(
        model=model,
        system="\n\n".join(system_parts) or None,
        turns=turns,
        max_tokens=max_tokens,
        stop=request.stop_sequences(),
        temperature=request.temperature,
        top_p=request.top_p,
        cache_system_prompt=request.gateway.cache_system_prompt,
        **options,
    )


# Claude's stop reasons that can occur on a text-only request, mapped to OpenAI's finish reasons.
FINISH_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}


def finish_reason(stop_reason: str | None) -> FinishReason:
    return FINISH_REASONS.get(stop_reason or "end_turn", "stop")
