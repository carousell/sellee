"""The Telegram receive loop — a dedicated thread that owns ALL Bot API traffic.

Because it is the one consumer of getUpdates, "an unbound channel consumes nothing" is a property
of this thread's state, not a convention spread across callers. Three states, derived from durable
rows every tick (so a restart resumes correctly), always failing toward the *less* capable one:

  * off            — no token, or a token with neither a live nonce nor a bound chat. Zero Bot API
                     calls; the thread idles and re-checks (a fresh `connect` flips it to
                     awaiting-bind). A nonce that outlived its TTL lands here and is cleared on the
                     tick, so an abandoned bind stops waiting for a /start it must not honour.
  * awaiting-bind  — token + an unexpired nonce, no chat. getUpdates runs; a /start whose payload
                     matches the nonce binds; everything else is consumed and discarded
                     (unattributable).
  * bound          — a chat is bound. Only that chat's updates are ingested (persist-then-ack in
                     one transaction); other chats are dropped before ingest.

The long-poll shape is Telegram-specific; once a batch is ingested, the fan-out to observability
events, fast-path dispatch, and channel-pass routing is provider-agnostic (channel.fastpaths /
channel.routing). Transport errors back off 5s -> 60s so a DNS outage never hot-spins.
"""

from __future__ import annotations

import logging

from sellee import paths, secrets
from sellee.channel import asks, fastpaths, outbound, routing
from sellee.channel.telegram import commands
from sellee.channel.telegram.transport import (
    ChannelError,
    TelegramClient,
    _normalize,
    commands_hash,
)
from sellee.store import bind_nonce_live

log = logging.getLogger(__name__)

POLL_TIMEOUT_SEC = 25
OFF_IDLE_SEC = 1.0
BACKOFF_BASE_SEC = 5.0
BACKOFF_CAP_SEC = 60.0
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
            self._retire_lapsed_nonce(ch)
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
        # token with neither nonce nor chat is off (e.g. a crash between token write and arm_bind);
        # a nonce past its deadline is off too, so an abandoned bind stops long-polling for a
        # /start it must no longer honour.
        if not token:
            return "off"
        if ch["chat_id"] is not None:
            return "bound"
        if bind_nonce_live(ch):
            return "awaiting_bind"
        return "off"

    def _retire_lapsed_nonce(self, ch) -> None:
        """Drop a nonce whose window closed. Without this the row keeps a dead secret and reads as
        armed to anything that looks at the column rather than the deadline."""
        if ch["bind_nonce"] and ch["chat_id"] is None and not bind_nonce_live(ch):
            self.store.clear_bind_nonce(ch["bind_nonce"])
            log.info("bind nonce expired before the chat bound — channel off until a fresh connect")

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
                # A long poll can straddle the deadline, so the nonce that was live when this poll
                # started is re-read here rather than trusted from the batch's own snapshot.
                if not bind_nonce_live(self.store.get_channel()):
                    break
                if self._complete_bind(client, chat, upd["update_id"] + 1, nonce):
                    return
                break  # a re-arm retired this nonce — ack the /start, never bind it
        # No matching /start: the whole batch is unattributable pre-bind traffic — discard it (ack
        # by advancing the cursor). A bare /start or a stranger's message never binds.
        self.store.advance_offset(max_id + 1)

    def _complete_bind(self, client, chat_id, update_offset, nonce) -> bool:
        # chat_id + nonce-clear + cursor advance in one transaction (the store enforces atomicity).
        if not self.store.complete_bind(chat_id, update_offset, nonce):
            return False
        self._ensure_commands(client)
        ch = self.store.get_channel()
        if not ch["welcomed_at"]:
            # Queued durably (the drain lane delivers, retried, catchup-backstopped) rather than
            # sent from here — a network hiccup at bind time must not eat the greeting.
            try:
                outbound.queue_welcome(self.store)
            except Exception:
                log.exception("welcome queue failed (bind still complete)")
        self.bus.publish("channel.bound", {"bot_username": ch["bot_username"]})
        return True

    def _ensure_commands(self, client) -> None:
        """Register the "/" menu, content-hash idempotent so a re-bind of the same bot is free.

        Called on every bound poll, not only at bind: the menu is a property of the code, and a
        seller who bound months ago is exactly the one who never learns a command the release
        added. The hash makes the steady state a single store read — the API is touched only on
        the first poll after the set actually changed.
        """
        digest = commands_hash(commands.BOT_COMMANDS)
        if self.store.get_channel()["commands_hash"] == digest:
            return
        client.set_my_commands(commands.BOT_COMMANDS)
        self.store.stamp_commands_hash(digest)

    # --- bound ----------------------------------------------------------------------------

    def _poll_bound(self, client, ch) -> None:
        self._ensure_commands(client)
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
        # Same pre-ingest slot, same reason: a tap arrives carrying a token, and resolving it to the
        # words the seller tapped here means the durable row — and so the pass prompt, the
        # transcript, and catchup — all read as them saying it, with no special path downstream.
        events = asks.resolve_ask_answers(self.store, events)
        inserted = self.store.ingest_updates(events, max_id + 1)
        for row in inserted:
            routing.publish_channel_in(self.bus, row)
        self._ack_taps(client, inserted)
        self._dispatch_fast_paths(client, ch["chat_id"], inserted)
        routing.route_channel_pass(self.store, self.bus)

    def _ack_taps(self, client, inserted) -> None:
        """Stop the spinner on every button tap, fast path or not.

        Telegram leaves a button spinning until answerCallbackQuery lands, so this has to cover taps
        that route to a pass as well — a decision button ("Send checkout link") is answered by the
        pass seconds later, and an unacked one reads as the tap not having registered. Best-effort:
        a failed ack is cosmetic, and the reply still goes out.
        """
        for row in inserted:
            cq_id = (row["payload"] or {}).get("callback_query_id")
            if not cq_id:
                continue
            try:
                client.answer_callback_query(cq_id)
            except ChannelError as exc:
                log.warning("answerCallbackQuery failed: %s", exc)

    def _dispatch_fast_paths(self, client, chat_id, inserted) -> None:
        """Answer deterministic fast paths (/pause, /resume, /status, /catchup, /sellee and the
        control-row buttons) instantly, before any pass routing — so `/pause` is heard even mid-
        pass. The row is marked handled so it never routes; everything else stays pending for the
        channel pass. Taps are already acked by `_ack_taps`, which covers the ones that route."""
        handled: list = []
        for row in inserted:
            event = {"kind": row["kind"], "text": row["text"], "payload": row["payload"]}
            if not fastpaths.is_fast_path(event):
                continue
            if fastpaths.is_settings_door(event):
                # A door — button or exact text token — routed to the deterministic apply; it never
                # touches an LLM and works while paused (seller-initiated control). The reply may
                # carry its own controls (an Undo button on an applied change).
                text, controls = fastpaths.handle_settings_door(self.store, self.bus, event)
            else:
                text, controls = fastpaths.handle_fast_path(self.store, self.bus, event)
            try:
                client.send_message(chat_id, text, reply_markup=commands.render_controls(controls))
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
