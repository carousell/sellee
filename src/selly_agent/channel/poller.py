"""The channel poller — a dedicated thread that owns ALL Bot API traffic.

Because it is the one consumer of getUpdates, "an unbound channel consumes nothing" is a property
of this thread's state, not a convention spread across callers. Three states, derived from durable
rows every tick (so a restart resumes correctly), always failing toward the *less* capable one:

  * off            — no token, or a token with neither a nonce nor a bound chat. Zero Bot API
                     calls; the thread idles and re-checks (a fresh `connect` flips it to
                     awaiting-bind).
  * awaiting-bind  — token + nonce, no chat. getUpdates runs; a /start whose payload matches the
                     nonce binds; everything else is consumed and discarded (unattributable).
  * bound          — a chat is bound. Only that chat's updates are ingested (persist-then-ack in
                     one transaction); other chats are dropped before ingest.

Transport errors back off 5s -> 60s so a DNS outage never hot-spins. Acting on ingested rows (fast
paths, pass routing) is layered on top of the durable inbox by later steps — this thread's job is
correct intake.
"""

from __future__ import annotations

import logging

from .. import paths, secrets
from . import commands
from .commands import BOT_COMMANDS, WELCOME_TEXT
from .telegram import ChannelError, TelegramClient, _normalize, commands_hash

log = logging.getLogger(__name__)

POLL_TIMEOUT_SEC = 25
OFF_IDLE_SEC = 1.0
BACKOFF_BASE_SEC = 5.0
BACKOFF_CAP_SEC = 60.0
_CHANNEL_IN_PREVIEW_CAP = 200
_ALLOWED_UPDATES = ["message", "callback_query"]


class Poller:
    """The long-poll loop. Run `run()` on its own thread; `tick()` is one iteration, called
    directly by tests. Shares the daemon's stop_event so a stop is heard between long polls."""

    def __init__(
        self,
        *,
        store,
        config,
        bus,
        stop_event,
        client_factory=None,
        poll_timeout: int = POLL_TIMEOUT_SEC,
    ):
        self.store = store
        self.config = config
        self.bus = bus
        self.stop = stop_event
        self._client_factory = client_factory or self._default_client_factory
        self._poll_timeout = poll_timeout
        self._backoff = 0.0

    def _default_client_factory(self, token: str) -> TelegramClient:
        return TelegramClient(token, api_base=self.config.telegram_api_base)

    # --- the loop -------------------------------------------------------------------------

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                idle = self.tick()
            except ChannelError as exc:
                # A transport/API failure: back off (5s -> 60s) so a persistent outage never spins.
                self._arm_backoff()
                log.warning(
                    "channel poll transport error (backing off %.0fs): %s", self._backoff, exc
                )
                self.stop.wait(self._backoff)
            except Exception:  # a bug must not kill the thread — back off and keep listening
                self._arm_backoff()
                log.exception("channel poller tick failed (backing off %.0fs)", self._backoff)
                self.stop.wait(self._backoff)
            else:
                self._backoff = 0.0
                if idle:
                    self.stop.wait(idle)

    def _arm_backoff(self) -> None:
        self._backoff = min(
            self._backoff * 2 if self._backoff else BACKOFF_BASE_SEC, BACKOFF_CAP_SEC
        )

    def tick(self) -> float:
        """One iteration. Returns how long to idle before the next tick (0 when a long poll already
        paced it). Raises ChannelError on a transport failure (the loop backs off)."""
        token = secrets.read_telegram_bot_token()
        ch = self.store.get_channel()
        state = self._state(token, ch)
        if state == "off":
            return OFF_IDLE_SEC
        client = self._client_factory(token)
        if state == "awaiting_bind":
            self._poll_awaiting_bind(client, ch)
        else:
            self._poll_bound(client, ch)
        return 0.0

    @staticmethod
    def _state(token, ch) -> str:
        # Precedence fails toward the less capable state: no token is off even with a live row; a
        # token with neither nonce nor chat is off (e.g. a crash between token write and arm_bind).
        if not token:
            return "off"
        if ch["chat_id"] is not None:
            return "bound"
        if ch["bind_nonce"]:
            return "awaiting_bind"
        return "off"

    # --- awaiting-bind --------------------------------------------------------------------

    def _poll_awaiting_bind(self, client, ch) -> None:
        updates = client.get_updates(ch["update_offset"], self._poll_timeout, _ALLOWED_UPDATES)
        if not updates:
            return
        nonce = ch["bind_nonce"]
        max_id = ch["update_offset"] - 1
        for upd in updates:
            max_id = max(max_id, upd["update_id"])
            ev, chat = _normalize(upd, authorized_chat=None)
            if (
                ev is not None
                and ev["kind"] == "command"
                and ev["text"] == "/start"
                and chat is not None
                and ev["payload"].get("start_param") == nonce
            ):
                self._complete_bind(client, chat, upd["update_id"] + 1)
                return
        # No matching /start: the whole batch is unattributable pre-bind traffic — discard it (ack
        # by advancing the cursor). A bare /start or a stranger's message never binds.
        self.store.advance_offset(max_id + 1)

    def _complete_bind(self, client, chat_id, update_offset) -> None:
        # chat_id + nonce-clear + cursor advance in one transaction (the store enforces atomicity).
        self.store.complete_bind(chat_id, update_offset)
        self._ensure_commands(client)
        ch = self.store.get_channel()
        if not ch["welcomed_at"]:
            try:
                client.send_message(chat_id, WELCOME_TEXT)
            except ChannelError as exc:
                log.warning("welcome send failed (bind still complete): %s", exc)
            else:
                self.store.stamp_welcomed()
        self.bus.publish("channel.bound", {"bot_username": ch["bot_username"]})

    def _ensure_commands(self, client) -> None:
        """Register the "/" menu, content-hash idempotent so a re-bind of the same bot is free."""
        digest = commands_hash(BOT_COMMANDS)
        if self.store.get_channel()["commands_hash"] == digest:
            return
        client.set_my_commands(BOT_COMMANDS)
        self.store.stamp_commands_hash(digest)

    # --- bound ----------------------------------------------------------------------------

    def _poll_bound(self, client, ch) -> None:
        updates = client.get_updates(ch["update_offset"], self._poll_timeout, _ALLOWED_UPDATES)
        if not updates:
            return
        max_id = ch["update_offset"] - 1
        events: list = []
        for upd in updates:
            max_id = max(max_id, upd["update_id"])
            ev, _chat = _normalize(upd, authorized_chat=ch["chat_id"])
            if ev is None:
                continue  # not the authorized chat — dropped before ingest, still acked by cursor
            if ev["kind"] == "photo":
                # Durable intake: download media BEFORE the ingest transaction so a crashed pass
                # can never lose it (getFile is cursor-free — a crash before the txn re-downloads).
                ev["media_paths"] = self._download_photos(client, ev)
            events.append(ev)
        inserted = self.store.ingest_updates(events, max_id + 1)
        for row in inserted:
            self._publish_in(row)
        self._dispatch_fast_paths(client, ch["chat_id"], inserted)

    def _dispatch_fast_paths(self, client, chat_id, inserted) -> None:
        """Answer deterministic fast paths (/pause, /resume, /status, /catchup, /selly and the
        control-row buttons) instantly, before any pass routing — so `/pause` is heard even mid-
        pass. A button tap is acked first (stop its spinner), then the reply is sent; the row is
        marked handled so it never routes. Everything else stays pending for the channel pass."""
        handled: list = []
        for row in inserted:
            event = {"kind": row["kind"], "text": row["text"], "payload": row["payload"]}
            if not commands.is_fast_path(event):
                continue
            text, keyboard = commands.handle_fast_path(self.store, event)
            cq_id = row["payload"].get("callback_query_id")
            if cq_id:
                try:
                    client.answer_callback_query(cq_id)
                except ChannelError as exc:
                    log.warning("answerCallbackQuery failed: %s", exc)
            try:
                client.send_message(chat_id, text, reply_markup=keyboard)
            except ChannelError as exc:
                log.warning("fast-path reply send failed: %s", exc)
            handled.append(row["id"])
            self.bus.publish("channel.handled", {"kind": row["kind"], "by": "fast_path"})
        if handled:
            self.store.mark_inbox_handled(handled, "fast_path")

    def _download_photos(self, client, ev) -> list:
        file_id = ev["payload"].get("file_id")
        if not file_id:
            return []
        dest = paths.media_dir() / str(ev["event_id"]) / "photo.jpg"
        try:
            client.get_file_and_download(file_id, dest)
        except ChannelError as exc:
            log.warning("photo download failed for update %s: %s", ev["event_id"], exc)
            return []
        return [str(dest)]

    def _publish_in(self, row) -> None:
        preview = (row["text"] or "")[:_CHANNEL_IN_PREVIEW_CAP]
        self.bus.publish(
            "channel.in",
            {"kind": row["kind"], "preview": preview, "src_ts": row["src_ts"]},
        )
