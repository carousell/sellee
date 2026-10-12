"""The stream-json parser maps each line to the common event schema, truncating and failing soft."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sellee.pass_stream import is_cap_hit, parse_stream_line


def test_system_init_becomes_pass_init() -> None:
    line = json.dumps(
        {"type": "system", "subtype": "init", "session_id": "s1", "tools": ["get_item"]}
    )
    assert parse_stream_line(line) == [("pass.init", {"session_id": "s1", "tools": ["get_item"]})]


def test_assistant_text_and_tool_use() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "publishing now"},
                    {"type": "tool_use", "name": "carousell_ai_publish_listing", "input": {}},
                ]
            },
        }
    )
    events = parse_stream_line(line)
    assert events[0] == ("pass.message", {"text": "publishing now"})
    assert events[1] == ("pass.tool_use", {"name": "carousell_ai_publish_listing", "id": None})


def test_tool_use_carries_the_block_id() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "get_item", "id": "toolu_9"}]},
        }
    )
    ((kind, payload),) = parse_stream_line(line)
    assert (kind, payload) == ("pass.tool_use", {"name": "get_item", "id": "toolu_9"})


def test_user_message_becomes_tool_result() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        }
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.tool_result"
    assert payload == {"tool_use_id": "t1", "content": "ok"}


def test_multiple_tool_results_become_one_event_each() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "first"},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "second"},
                ]
            },
        }
    )
    events = parse_stream_line(line)
    assert [payload["tool_use_id"] for _, payload in events] == ["t1", "t2"]
    assert [payload["content"] for _, payload in events] == ["first", "second"]


def test_non_tool_result_content_keeps_the_single_blob_form() -> None:
    line = json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": "carry on"}]}}
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.tool_result"
    assert "tool_use_id" not in payload
    assert "carry on" in payload["content"]


def test_result_carries_usage_and_flags() -> None:
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 3,
            "session_id": "s1",
            "usage": {"input_tokens": 5},
        }
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.result"
    assert payload["is_error"] is False and payload["num_turns"] == 3
    assert payload["usage"] == {"input_tokens": 5}


def test_garbage_line_becomes_pass_raw_not_a_crash() -> None:
    assert parse_stream_line("not json at all")[0][0] == "pass.raw"
    assert parse_stream_line("[1,2,3]")[0][0] == "pass.raw"  # valid json, wrong shape
    assert parse_stream_line("   ") == []


def test_text_is_truncated_to_cap() -> None:
    big = "x" * 5000
    line = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": big}]}}
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.message"
    assert len(payload["text"]) < 5000 and payload["text"].endswith("chars)")


def test_a_text_block_whose_text_is_not_a_string_is_skipped() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": None}, {"type": "text", "text": "ok"}]
            },
        }
    )
    assert parse_stream_line(line) == [("pass.message", {"text": "ok"})]


def _tool_result_line(content) -> str:
    return json.dumps({"type": "user", "message": {"content": content}})


def test_an_image_result_is_summarized_not_stored_as_base64() -> None:
    """Reading a photo returns ~170KB of base64. Clipping it to the text cap would store a couple
    of KB of meaningless prefix and push the useful part out of the event."""
    data = "A" * 200_000
    line = _tool_result_line(
        [
            {
                "tool_use_id": "toolu_1",
                "type": "tool_result",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
                    }
                ],
            }
        ]
    )
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.tool_result"
    assert payload["tool_use_id"] == "toolu_1"
    assert data[:100] not in payload["content"]  # no base64 prefix survives
    assert "image/jpeg" in payload["content"]
    assert "150000" in payload["content"]  # 200k base64 chars ≈ 150KB decoded
    assert not payload["content"].endswith("chars)")  # summarized, so the cap never engaged


def test_an_image_block_without_a_usable_source_still_summarizes() -> None:
    for source in (None, {}, "nonsense"):
        line = _tool_result_line(
            [
                {
                    "tool_use_id": "toolu_1",
                    "type": "tool_result",
                    "content": [{"type": "image", "source": source}],
                }
            ]
        )
        ((_, payload),) = parse_stream_line(line)
        assert "image" in payload["content"]


def test_text_tool_results_are_unaffected() -> None:
    line = _tool_result_line(
        [{"tool_use_id": "toolu_1", "type": "tool_result", "content": '{"queued":true}'}]
    )
    ((_, payload),) = parse_stream_line(line)
    assert payload["content"] == '{"queued":true}'


def test_a_tool_result_is_capped_per_block() -> None:
    line = _tool_result_line(
        [{"tool_use_id": "toolu_1", "type": "tool_result", "content": "y" * 5000}]
    )
    ((_, payload),) = parse_stream_line(line)
    assert len(payload["content"]) < 5000 and payload["content"].endswith("chars)")


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def test_a_line_nested_too_deep_to_parse_becomes_pass_raw() -> None:
    """json.loads gives up on deep nesting with RecursionError, not ValueError."""
    ((kind, _),) = parse_stream_line(_nested(1_000_000))
    assert kind == "pass.raw"


@pytest.mark.parametrize(
    "content",
    [
        '[{"type":"tool_result","tool_use_id":"t1","content":' + _nested(5_000) + "}]",
        "[" + _nested(5_000) + "]",
    ],
    ids=["tool_result", "plain"],
)
def test_user_content_nested_past_the_recursion_limit_is_elided(content: str) -> None:
    """The line parses, but summarizing its images and re-serializing it recursed until
    RecursionError, which the reader thread does not catch: it died, stdout went unread, and the
    pass's result was never recorded."""
    line = '{"type":"user","message":{"content":' + content + "}}"
    ((kind, payload),) = parse_stream_line(line)
    assert kind == "pass.tool_result"
    assert len(payload["content"]) < 200


def test_is_cap_hit_reads_result_subtype() -> None:
    assert is_cap_hit({"subtype": "error_max_turns"}) is True
    assert is_cap_hit({"subtype": "success"}) is False
    assert is_cap_hit(None) is False


# Arbitrary text plus well-formed JSON, so generation spends some of its budget past the
# json.loads guard rather than only on garbage that fails to parse.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=8,
)
# Typed lines carry message content too, so generation reaches the per-block handling rather
# than stopping at the dispatch on type.
blocks = st.fixed_dictionaries(
    {"type": st.sampled_from(["text", "tool_use", "tool_result", "image"])},
    optional={
        key: json_values for key in ("text", "name", "id", "tool_use_id", "content", "source")
    },
)
messages = st.fixed_dictionaries(
    {}, optional={"content": st.lists(blocks | json_values, max_size=4)}
)
typed = st.fixed_dictionaries(
    {"type": st.sampled_from(["system", "assistant", "user", "result"])},
    optional={"subtype": json_values, "message": messages | json_values},
)
lines = st.one_of(st.text(), json_values.map(json.dumps), typed.map(json.dumps))


@settings(max_examples=200, deadline=None)
@given(lines)
def test_it_never_raises_and_always_returns_pairs(line: str) -> None:
    """The line is another process's stdout, and the reader thread depends on "Never raises":
    anything unrecognised must degrade to an event, never to an exception that kills it."""
    events = parse_stream_line(line)
    assert isinstance(events, list)
    for event in events:
        kind, payload = event
        assert isinstance(kind, str)
        assert isinstance(payload, dict)
