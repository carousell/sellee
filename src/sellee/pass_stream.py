"""Parse `claude -p --output-format stream-json` output into the common event schema, live.

Each stdout line is one JSON object; this maps it to zero or more (kind, payload) pairs the pass
runner publishes to the bus as the pass runs — so logs --follow shows a live pass, not a
post-mortem of a log tail. Text and tool-result payloads are truncated to a cap, and an image
block's base64 is summarized to its size (the transcript store is an observability record, not a
verbatim harness log; the per-pass stderr file keeps the rest). A line that is not parseable JSON
becomes pass.raw rather than crashing the reader.
"""

from __future__ import annotations

import json

_TEXT_CAP = 2000


def _truncate(text: str) -> str:
    if len(text) <= _TEXT_CAP:
        return text
    return text[:_TEXT_CAP] + f"...(+{len(text) - _TEXT_CAP} chars)"


def parse_stream_line(line: str) -> list:
    """Return a list of (kind, payload) events for one stream-json line. Never raises."""
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        return [("pass.raw", {"line": _truncate(line)})]
    if not isinstance(obj, dict):
        return [("pass.raw", {"line": _truncate(line)})]

    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "init":
        return [("pass.init", {"session_id": obj.get("session_id"), "tools": obj.get("tools", [])})]
    if kind == "assistant":
        return _assistant_events(obj)
    if kind == "user":
        return _user_events(obj)
    if kind == "result":
        return [
            (
                "pass.result",
                {
                    "subtype": obj.get("subtype"),
                    "is_error": obj.get("is_error"),
                    "num_turns": obj.get("num_turns"),
                    "duration_ms": obj.get("duration_ms"),
                    "session_id": obj.get("session_id"),
                    "usage": obj.get("usage"),
                },
            )
        ]
    return [("pass.raw", {"line": _truncate(line)})]


def _assistant_events(obj: dict) -> list:
    events = []
    texts = []
    for block in _message_content(obj):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            events.append(("pass.tool_use", {"name": block.get("name"), "id": block.get("id")}))
    joined = "".join(texts).strip()
    if joined:
        events.insert(0, ("pass.message", {"text": _truncate(joined)}))
    return events


def _user_events(obj: dict) -> list:
    """One event per tool_result block, carrying the id of the tool_use it answers.

    The harness ids are what let a reader pair a call with its result exactly instead of guessing
    from adjacency — which stops being reliable the moment two passes interleave. Content that is
    not a tool_result block (a plain user turn) has nothing to pair with and stays a single blob.
    """
    events = []
    leftover = []
    for block in _message_content(obj):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            events.append(
                (
                    "pass.tool_result",
                    {
                        "tool_use_id": block.get("tool_use_id"),
                        "content": _truncate(_stringify(block.get("content"))),
                    },
                )
            )
        else:
            leftover.append(block)
    if leftover or not events:
        events.append(("pass.tool_result", {"content": _truncate(_stringify(leftover))}))
    return events


def _message_content(obj: dict) -> list:
    message = obj.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
    return []


def _elide_images(value: object) -> object:
    """Replace an image block's base64 payload with its media type and decoded size.

    A photo read arrives as a content block carrying ~170KB of base64. Truncating that to the text
    cap would store a couple of KB of meaningless prefix and push the useful part of the result out
    of the event — so the payload is summarized rather than clipped, which is also the only form a
    person tailing `logs` can read.
    """
    if isinstance(value, list):
        return [_elide_images(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image":
            source = value.get("source")
            source = source if isinstance(source, dict) else {}
            data = source.get("data")
            summary = {"type": "image", "media_type": source.get("media_type")}
            if isinstance(data, str):
                # base64 encodes 3 bytes per 4 characters, padding included.
                summary["bytes"] = len(data) * 3 // 4
            return summary
        return {key: _elide_images(item) for key, item in value.items()}
    return value


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_elide_images(value), default=str)


def is_cap_hit(result_payload: dict | None) -> bool:
    """A max-turns termination, read from the result event's subtype. Best-effort."""
    if not result_payload:
        return False
    subtype = result_payload.get("subtype") or ""
    return "max_turns" in subtype
