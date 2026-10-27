import pytest

from providers import Turn
from schemas import ChatRequest
from translate import InvalidMessages, finish_reason, to_provider_request


def request(*messages, **fields) -> ChatRequest:
    return ChatRequest(model="smart", messages=[{"role": r, "content": c} for r, c in messages], **fields)


def test_leading_system_and_developer_messages_become_the_system_prompt():
    sent = to_provider_request(
        request(("system", "Be brief."), ("developer", "Use US English."), ("user", "hi")), "m", 10
    )
    assert sent.system == "Be brief.\n\nUse US English."
    assert sent.turns == [Turn("user", "hi")]


def test_no_system_message_means_no_system_prompt():
    assert to_provider_request(request(("user", "hi")), "m", 10).system is None


def test_consecutive_messages_from_one_role_are_merged():
    sent = to_provider_request(request(("user", "a"), ("user", "b"), ("assistant", "c"), ("user", "d")), "m", 10)
    assert sent.turns == [Turn("user", "a\n\nb"), Turn("assistant", "c"), Turn("user", "d")]


def test_content_parts_are_joined():
    chat = ChatRequest(model="smart", messages=[{"role": "user", "content": [
        {"type": "text", "text": "one "}, {"type": "text", "text": "two"},
    ]}])  # fmt: skip
    assert to_provider_request(chat, "m", 10).turns == [Turn("user", "one two")]


@pytest.mark.parametrize(
    "messages",
    [
        [("user", "hi"), ("system", "late")],
        [("system", "only a system prompt")],
        [("assistant", "I start"), ("user", "hi")],
        [("user", "hi"), ("assistant", "prefill")],
        [("user", "   ")],
    ],
)
def test_shapes_claude_cant_take_are_refused(messages):
    with pytest.raises(InvalidMessages):
        to_provider_request(request(*messages), "m", 10)


def test_request_fields_and_route_options_are_carried():
    sent = to_provider_request(
        request(("user", "hi"), stop="END", gateway={"cache_system_prompt": True}),
        "claude-x", 99, effort="low", refusal_fallback=True,
    )  # fmt: skip
    assert (sent.model, sent.max_tokens, sent.stop) == ("claude-x", 99, ["END"])
    assert (sent.cache_system_prompt, sent.effort, sent.refusal_fallback) == (True, "low", True)


@pytest.mark.parametrize(
    "stop_reason, expected",
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("refusal", "content_filter"),
        (None, "stop"),
    ],
)
def test_stop_reasons_map_to_finish_reasons(stop_reason, expected):
    assert finish_reason(stop_reason) == expected
