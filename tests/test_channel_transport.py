"""The Telegram transport: the pure `_normalize` mapping and the TelegramClient over the fake API.

`_normalize` is exercised without a network (it is a pure function); the client is driven against
the in-process fake Bot API so the real urllib path (chunking, keyboards, downloads, token-free
errors) is covered end to end.
"""

from __future__ import annotations

import pytest

from fake_telegram_api import BOT, CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent.channel.telegram import transport
from selly_agent.channel.telegram.transport import (
    ChannelError,
    TelegramClient,
    build_inline_keyboard,
    chunk_text,
    commands_hash,
    is_valid_token_format,
)

# --- token format ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", [FAKE_TOKEN, "123:AAABBBCCCDDDEEEFFFGGGHHHIIIJJJKKK00"])
def test_valid_token_format(token) -> None:
    assert is_valid_token_format(token)


@pytest.mark.parametrize("token", ["", "notatoken", "123:short", "abc:AA" + "x" * 30])
def test_invalid_token_format(token) -> None:
    assert not is_valid_token_format(token)


# --- _normalize (pure) ----------------------------------------------------------------------


def _msg(text, uid=5, date=111):
    return {"update_id": uid, "message": {"chat": {"id": CHAT_ID}, "date": date, "text": text}}


def test_normalize_text() -> None:
    ev, chat = transport._normalize(_msg("hello there"), CHAT_ID)
    assert chat == CHAT_ID
    assert ev["kind"] == "text" and ev["text"] == "hello there"
    assert ev["src_ts"] == 111 and ev["payload"] == {}


def test_normalize_command_without_arg() -> None:
    ev, _ = transport._normalize(_msg("/status"), CHAT_ID)
    assert ev["kind"] == "command" and ev["text"] == "/status"
    assert ev["payload"] == {}


def test_normalize_start_lifts_nonce_payload() -> None:
    ev, _ = transport._normalize(_msg("/start nonce-abc123"), CHAT_ID)
    assert ev["kind"] == "command" and ev["text"] == "/start"
    assert ev["payload"]["start_param"] == "nonce-abc123"


def test_normalize_bare_start_has_empty_param() -> None:
    ev, _ = transport._normalize(_msg("/start"), CHAT_ID)
    assert ev["text"] == "/start" and ev["payload"]["start_param"] == ""


def test_normalize_callback_query_splits_ref() -> None:
    upd = {
        "update_id": 7,
        "callback_query": {
            "id": "cbq9",
            "data": "chg_1:approve",
            "message": {"chat": {"id": CHAT_ID}, "date": 222},
        },
    }
    ev, chat = transport._normalize(upd, CHAT_ID)
    assert ev["kind"] == "action" and ev["text"] == "approve"
    assert ev["payload"]["ref"] == "chg_1" and ev["payload"]["choice"] == "approve"
    assert ev["payload"]["callback_query_id"] == "cbq9"


def test_normalize_photo_picks_largest_and_group_hint() -> None:
    upd = {
        "update_id": 8,
        "message": {
            "chat": {"id": CHAT_ID},
            "date": 1,
            "caption": "for sale",
            "media_group_id": "mg1",
            "photo": [
                {"file_id": "small", "file_size": 100},
                {"file_id": "big", "file_size": 9000},
            ],
        },
    }
    ev, _ = transport._normalize(upd, CHAT_ID)
    assert ev["kind"] == "photo" and ev["text"] == "for sale"
    assert ev["payload"]["file_id"] == "big"
    assert ev["payload"]["media_group_id"] == "mg1"


def test_normalize_drops_unauthorized_chat() -> None:
    ev, chat = transport._normalize(_msg("hi"), authorized_chat=CHAT_ID + 1)
    assert ev is None and chat == CHAT_ID


def test_normalize_accepts_any_chat_when_none_authorized() -> None:
    # awaiting-bind: authorized_chat None means "not yet bound" — the update is returned so the
    # poller can inspect a /start payload; a non-matching one is discarded by the poller, not here.
    ev, chat = transport._normalize(_msg("/start n1"), authorized_chat=None)
    assert ev is not None and chat == CHAT_ID


# --- pure helpers ---------------------------------------------------------------------------


def test_chunk_text_splits_at_limit() -> None:
    chunks = chunk_text("a" * 5000, limit=4096)
    assert len(chunks) == 2 and len(chunks[0]) == 4096 and len(chunks[1]) == 904


def test_chunk_text_prefers_newline_boundary() -> None:
    text = "x" * 4000 + "\n" + "y" * 200
    chunks = chunk_text(text, limit=4096)
    assert chunks[0] == "x" * 4000 and chunks[1] == "y" * 200


def test_chunk_text_empty_yields_one_chunk() -> None:
    assert chunk_text("") == [""]


def test_build_keyboard_enforces_callback_cap() -> None:
    build_inline_keyboard([[("ok", "short")]])  # fine
    with pytest.raises(ValueError, match="64 bytes"):
        build_inline_keyboard([[("ok", "x" * 65)]])


def test_commands_hash_is_stable_and_change_sensitive() -> None:
    a = [{"command": "status", "description": "s"}]
    b = [{"command": "status", "description": "changed"}]
    assert commands_hash(a) == commands_hash(a)
    assert commands_hash(a) != commands_hash(b)


# --- TelegramClient over the fake API -------------------------------------------------------


def test_get_me_round_trip() -> None:
    with FakeTelegramAPI() as api:
        client = TelegramClient(FAKE_TOKEN, api_base=api.base_url)
        assert client.get_me()["username"] == BOT["username"]


def test_send_message_chunks_and_keyboard_on_last() -> None:
    with FakeTelegramAPI() as api:
        client = TelegramClient(FAKE_TOKEN, api_base=api.base_url)
        kb = build_inline_keyboard([[("Resume", "resume")]])
        ids = client.send_message(CHAT_ID, "a" * 5000, reply_markup=kb)
        assert len(ids) == 2
        assert api.outbox[0]["reply_markup"] is None  # keyboard only on the last chunk
        assert api.outbox[1]["reply_markup"] == kb


def test_get_updates_and_actions() -> None:
    with FakeTelegramAPI() as api:
        api.inject_text("hello")
        client = TelegramClient(FAKE_TOKEN, api_base=api.base_url)
        updates = client.get_updates(0, timeout=0, allowed_updates=["message"])
        assert updates[0]["message"]["text"] == "hello"
        client.send_chat_action(CHAT_ID)
        client.answer_callback_query("cbq1")
        assert api.chat_actions == [CHAT_ID] and api.answered == ["cbq1"]


def test_get_file_downloads_bytes(tmp_path) -> None:
    with FakeTelegramAPI() as api:
        api.files["photo1"] = b"\xff\xd8real-jpeg"
        client = TelegramClient(FAKE_TOKEN, api_base=api.base_url)
        dest = client.get_file_and_download("photo1", tmp_path / "sub" / "p.jpg")
        assert dest.read_bytes() == b"\xff\xd8real-jpeg"


def test_api_error_text_never_carries_the_token() -> None:
    with FakeTelegramAPI() as api:
        client = TelegramClient(FAKE_TOKEN, api_base=api.base_url)
        with pytest.raises(ChannelError) as excinfo:
            client._api("bogusMethod", {})  # the fake returns HTTP 404 for an unknown method
        assert FAKE_TOKEN not in str(excinfo.value)
        assert "404" in str(excinfo.value)
