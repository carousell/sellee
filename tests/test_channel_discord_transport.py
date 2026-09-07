"""The Discord REST transport: the pure `_normalize` mapping and DiscordClient over the fake API.

`_normalize` is exercised without a network (a pure function); the client is driven against the
in-process fake REST server so the real urllib path (auth header, JSON bodies, error mapping) is
covered end to end.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fake_discord_api import BOT_ID, CHANNEL_ID, FAKE_TOKEN, FakeDiscordAPI
from sellee.channel.discord import transport
from sellee.channel.discord.transport import ChannelError, DiscordClient

# --- _normalize (pure) ------------------------------------------------------------------------


def _message_create(content, event_id="1", author_bot=False, attachments=None):
    return {
        "t": "MESSAGE_CREATE",
        "d": {
            "id": event_id,
            "channel_id": str(CHANNEL_ID),
            "author": {"id": "555", "username": "seller", "bot": author_bot},
            "content": content,
            "attachments": attachments or [],
            "timestamp": "2026-08-12T00:00:00.000000+00:00",
        },
    }


def test_normalize_text() -> None:
    ev = transport._normalize(_message_create("hello there"))
    assert ev["kind"] == "text"
    assert ev["text"] == "hello there"
    assert ev["payload"] == {}
    assert ev["event_id"] == 1


def test_normalize_ignores_the_bot_s_own_messages() -> None:
    assert transport._normalize(_message_create("echo", author_bot=True)) is None


def test_normalize_converts_the_iso_timestamp_to_an_epoch_float() -> None:
    """`src_ts` is a REAL column and rides on `channel.in` bus events; Discord sends ISO 8601 text
    where Telegram sends an epoch, so the conversion belongs here rather than leaving one provider
    putting a string in a numeric field."""
    ev = transport._normalize(_message_create("hello"))
    assert isinstance(ev["src_ts"], float)
    assert ev["src_ts"] == datetime(2026, 8, 12, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("timestamp", [None, "", "not-a-timestamp"])
def test_normalize_degrades_an_unusable_timestamp_to_none(timestamp) -> None:
    """src_ts is informational, never the ordering clock, so a timestamp we cannot read must not
    cost the seller the message itself."""
    event = _message_create("hello")
    event["d"]["timestamp"] = timestamp
    assert transport._normalize(event)["src_ts"] is None


def test_normalize_lifts_an_attachment_to_photo_kind() -> None:
    attachments = [
        {
            "id": "a1",
            "filename": "photo.jpg",
            "url": "http://example/attachments/fake.jpg",
            "content_type": "image/jpeg",
            "size": 123,
        }
    ]
    ev = transport._normalize(_message_create("a caption", attachments=attachments))
    assert ev["kind"] == "photo"
    assert ev["text"] == "a caption"
    assert ev["payload"]["url"] == "http://example/attachments/fake.jpg"


def test_normalize_button_click() -> None:
    # A real Discord snowflake shape (all-digit string) — _normalize casts event_id to int for the
    # channel_inbox INTEGER column, so a non-numeric placeholder here would mask that behavior.
    interaction = {
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "111222333444555666",
            "token": "int-token",
            "type": 3,  # MESSAGE_COMPONENT
            "channel_id": str(CHANNEL_ID),
            "data": {"custom_id": "chg_1:approve"},
        },
    }
    ev = transport._normalize(interaction)
    assert ev["kind"] == "action"
    assert ev["text"] == "approve"
    assert ev["event_id"] == 111222333444555666
    assert ev["payload"] == {
        "ref": "chg_1",
        "choice": "approve",
        "interaction_id": "111222333444555666",
        "interaction_token": "int-token",
    }


def test_normalize_ignores_dispatch_kinds_we_do_not_handle() -> None:
    assert transport._normalize({"t": "TYPING_START", "d": {}}) is None


# --- DiscordClient over the fake API -----------------------------------------------------------


def _client(api) -> DiscordClient:
    return DiscordClient(FAKE_TOKEN, api_base=api.base_url + "/api/v10")


def test_get_me() -> None:
    with FakeDiscordAPI() as api:
        me = _client(api).get_me()
        assert me["id"] == BOT_ID


def test_get_application() -> None:
    with FakeDiscordAPI() as api:
        app = _client(api).get_application()
        assert "id" in app


def test_send_message_records_content_and_components() -> None:
    with FakeDiscordAPI() as api:
        _client(api).send_message(CHANNEL_ID, "hi there", components=[("Pause", "pause")])
        assert api.outbox[-1]["content"] == "hi there"
        assert api.outbox[-1]["components"][0]["components"][0]["custom_id"] == "pause"


def test_components_never_exceed_discords_action_row_cap() -> None:
    """Discord rejects an action row holding a sixth button — the whole message, not just the
    button. The marketplace picker is as long as the seller's enabled list, so this has to wrap
    rather than trust the spec to be short.

    The cap, not a row count: the shared legibility packing (channel/controls.py) decides how many
    actually land in a row, and it is stricter than Discord's limit. What this pins is that the
    limit can never be crossed however that packing is tuned."""
    spec = [(f"m{i}", f"m{i}:connectmkt") for i in range(9)]

    rows = transport.build_components(spec)

    assert rows and all(len(row["components"]) <= transport.MAX_BUTTONS_PER_ROW for row in rows)
    assert [b["custom_id"] for row in rows for b in row["components"]] == [t for _l, t in spec]


def test_a_short_spec_is_still_one_action_row() -> None:
    rows = transport.build_components([("Pause", "pause"), ("What needs me", "needsme")])
    assert len(rows) == 1


def test_send_message_over_the_2000_char_limit_is_chunked() -> None:
    with FakeDiscordAPI() as api:
        _client(api).send_message(CHANNEL_ID, "x" * 2500)
        assert len(api.outbox) == 2
        assert all(len(m["content"]) <= 2000 for m in api.outbox)


def test_send_message_returns_the_message_id_as_an_int() -> None:
    # Discord serializes snowflake ids as JSON strings (the fake mirrors this: {"id": "111", ...}),
    # so send_message must cast before returning — its declared -> int signature must match reality.
    with FakeDiscordAPI() as api:
        message_id = _client(api).send_message(CHANNEL_ID, "hi")
        assert isinstance(message_id, int)
        assert message_id == 111


def test_trigger_typing() -> None:
    with FakeDiscordAPI() as api:
        _client(api).trigger_typing(CHANNEL_ID)
        assert api.typing_pulses == [True]


def test_acknowledge_interaction() -> None:
    with FakeDiscordAPI() as api:
        _client(api).acknowledge_interaction("int1", "int-token")
        assert api.acknowledged == ["/api/v10/interactions/int1/int-token/callback"]


def test_download_attachment(tmp_path) -> None:
    with FakeDiscordAPI() as api:
        dest = tmp_path / "photo.jpg"
        _client(api).download_attachment(api.base_url + "/attachments/fake.jpg", dest)
        assert dest.read_bytes() == api.files["/attachments/fake.jpg"]


def test_every_request_carries_the_documented_user_agent() -> None:
    """Discord's edge blocklists urllib's default User-Agent and 403s before reading the token, so
    a missing header surfaces as a rejected credential — the failure the fake now reproduces on
    every route. This pins the header's shape; the rest of the suite covers its presence."""
    with FakeDiscordAPI() as api:
        client = _client(api)
        client.get_me()
        client.get_application()
        client.send_message(CHANNEL_ID, "hi")
        assert api.user_agents  # the fake saw requests at all
        assert set(api.user_agents) == {transport.USER_AGENT}
        assert transport.USER_AGENT.startswith("DiscordBot (https://")
        assert "urllib" not in transport.USER_AGENT


def test_the_attachment_cdn_gets_the_user_agent_too(tmp_path) -> None:
    # The CDN sits behind the same edge filter, so a photo download 403s without the header.
    with FakeDiscordAPI() as api:
        dest = tmp_path / "photo.jpg"
        _client(api).download_attachment(api.base_url + "/attachments/fake.jpg", dest)
        assert dest.read_bytes() == api.files["/attachments/fake.jpg"]
        assert api.user_agents == [transport.USER_AGENT]


def test_a_missing_user_agent_is_refused_the_way_discord_refuses_it(tmp_path) -> None:
    """The guard that makes the suite load-bearing: with urllib's default UA the fake answers the
    same 403 real Discord does, so a transport that stops sending the header fails here."""
    import urllib.error
    import urllib.request

    with FakeDiscordAPI() as api:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(api.base_url + "/api/v10/users/@me", timeout=5)  # noqa: S310
        assert exc.value.code == 403


def test_transport_error_never_carries_the_token() -> None:
    with FakeDiscordAPI() as api:
        client = DiscordClient("bad-token-shape", api_base=api.base_url + "/api/v10")
        with pytest.raises(ChannelError) as exc:
            client.get_me()
        assert "bad-token-shape" not in str(exc.value)
