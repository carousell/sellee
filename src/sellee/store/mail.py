"""The mail transport's rows: whether a mailbox is connected, and what has been read from it.

Three tables, each existing because the alternative is a specific silent failure. The reasoning is
in `migrations/data/0019_mail_relay.sql`; this is the typed way in.

**`mail_transport`** — one row per market whose buyers arrive as mail. "The mailbox is connected"
used to mean a non-empty settings string, which was never navigated, never probed, and never
re-checked; a signed-out seller was told their replies were going out.

**`mail_relay_threads`** — a conversation, keyed on the mail provider's own thread id, because a
craigslist relay address identifies nothing (measured: five addresses for one posting, two from
consecutive reloads seconds apart). Carries the two facts that decide what may be sent: whether the
handoff invitation has gone out, and whether the conversation has moved off the relay.

**`mail_relay_seen`** — idempotence. A mailbox read has no cursor it can trust, so without a record
of what has been folded, `reconcile` finds a new tail row every tick: the buyer appears to repeat
themselves and the agent answers each repeat.
"""

from __future__ import annotations

from typing import TypedDict

from sellee.db import Database
from sellee.store.helpers import _now

# The states a relay conversation can be in. Only `open` may be sent to.
MAIL_STATE_OPEN = "open"
MAIL_STATE_OPTED_OUT = "opted_out"
MAIL_STATE_UNREACHABLE = "unreachable"
MAIL_STATE_EXPIRED = "expired"
MAIL_STATES = (
    MAIL_STATE_OPEN,
    MAIL_STATE_OPTED_OUT,
    MAIL_STATE_UNREACHABLE,
    MAIL_STATE_EXPIRED,
)


class MailTransport(TypedDict):
    market: str
    provider: str
    view: str
    handoff_address: str
    signed_in: bool
    verified_ts: float | None
    last_probe_ts: float | None


class MailThread(TypedDict):
    provider_thread_id: str
    thread_id: str
    market: str
    relay_address: str
    posting_url: str
    state: str
    closed_reason: str
    handoff_sent_ts: float | None
    off_relay_ts: float | None


def _existing(conn, market: str) -> dict:
    """The row as it stands, inside the caller's transaction, defaulted when absent.

    Read-then-write rather than a `CASE WHEN` on a numbered binding: this table has three writers
    that each own different columns and must leave the others exactly as they were, and expressing
    that in SQL made the bindings unreadable enough to get wrong once.
    """
    found = conn.execute(
        "SELECT provider, view, handoff_address, signed_in, verified_ts FROM mail_transport "
        "WHERE market = ?",
        (market,),
    ).fetchone()
    if found is None:
        return {
            "provider": "",
            "view": "",
            "handoff_address": "",
            "signed_in": 0,
            "verified_ts": None,
        }
    return dict(found)


class MailMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    # --- the mailbox itself ------------------------------------------------------------------

    def mail_transport(self, market: str) -> MailTransport | None:
        """What is known about this market's mailbox, or `None` if it has never been probed."""
        rows = self._db.query(
            "SELECT market, provider, view, handoff_address, signed_in, verified_ts, "
            "last_probe_ts FROM mail_transport WHERE market = ?",
            (market,),
        )
        if not rows:
            return None
        row = rows[0]
        return MailTransport(
            market=row["market"],
            provider=row["provider"],
            view=row["view"],
            handoff_address=row["handoff_address"],
            signed_in=bool(row["signed_in"]),
            verified_ts=row["verified_ts"],
            last_probe_ts=row["last_probe_ts"],
        )

    def record_mail_probe(
        self,
        market: str,
        *,
        provider: str,
        signed_in: bool,
        view: str | None = None,
        now: float | None = None,
    ) -> None:
        """Record what a probe of this mailbox found.

        `view` is left alone when `None` rather than cleared: a probe answers "are you signed in",
        and a signed-out mailbox has not stopped having the view the seller confirmed. Clearing it
        would silently demote them from "sign in again" to "set this up from scratch".
        """
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            held = _existing(conn, market)
            conn.execute(
                "INSERT INTO mail_transport (market, provider, view, handoff_address, signed_in, "
                "verified_ts, last_probe_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (market) DO UPDATE SET provider = excluded.provider, "
                "view = excluded.view, signed_in = excluded.signed_in, "
                "last_probe_ts = excluded.last_probe_ts, updated_ts = excluded.updated_ts",
                (
                    market,
                    provider,
                    held["view"] if view is None else view,
                    held["handoff_address"],
                    1 if signed_in else 0,
                    held["verified_ts"],
                    now,
                    now,
                ),
            )

    def record_mail_view(self, market: str, view: str, now: float | None = None) -> None:
        """Record the scoped view, once a probe has actually read it.

        Separate from the probe because the ordering matters: a view written before it is read
        flips the seller from an actionable message ("sign in to your mailbox") to an unactionable
        one ("I can't read your mailbox").
        """
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            held = _existing(conn, market)
            conn.execute(
                "INSERT INTO mail_transport (market, provider, view, handoff_address, signed_in, "
                "verified_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (market) DO UPDATE SET view = excluded.view, "
                "updated_ts = excluded.updated_ts",
                (
                    market,
                    held["provider"],
                    view,
                    held["handoff_address"],
                    held["signed_in"],
                    held["verified_ts"],
                    now,
                ),
            )

    def record_mail_handoff(
        self, market: str, address: str, *, verified: bool = False, now: float | None = None
    ) -> None:
        """Record the handoff address, and whether the scoped view queries it coherently.

        `verified` means the *query* was confirmed — Gmail answering the `deliveredto:` clause with
        results or an honest "no messages matched" rather than an error. That is the failure which
        actually happened: a literal `+` in a Gmail hash query is a space, so the clause silently
        became `deliveredto:you cl@x.com` and the view reported an empty mailbox with a buyer's
        thread sitting in it.

        It deliberately does **not** claim mail reaches the address. Delivery proves itself:
        `mail_relay_threads.off_relay_ts` can only be set by a real message arriving here, and the
        gate that depends on delivery — the close, which may only send a payment link on the direct
        leg — rests on that instead of on a synthetic test that could pass while real forwards fail.
        """
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            held = _existing(conn, market)
            # A re-record without `verified` keeps an existing verification: the address has not
            # changed, and re-deriving it must not demote a mailbox that has already made the trip.
            stamped = now if verified else held["verified_ts"]
            if address != held["handoff_address"] and not verified:
                # A *different* address has proved nothing, so its verification starts over.
                stamped = None
            conn.execute(
                "INSERT INTO mail_transport (market, provider, view, handoff_address, signed_in, "
                "verified_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (market) DO UPDATE SET "
                "handoff_address = excluded.handoff_address, "
                "verified_ts = excluded.verified_ts, updated_ts = excluded.updated_ts",
                (
                    market,
                    held["provider"],
                    held["view"],
                    address,
                    held["signed_in"],
                    stamped,
                    now,
                ),
            )

    def mail_ready(self, market: str) -> bool:
        """Whether this market's buyers can actually be answered right now.

        Every clause is one measured failure. Signed out reads nothing. No view means no scope, and
        an unscoped read is the seller's whole mailbox. No handoff address means every conversation
        eventually expires unreachable, because craigslist mints a relay address per view and a
        retired one cannot be recovered. An unverified one means the address is in the reply but not
        coherently in the view — the shape of the `+`-encoding bug, where the mailbox reads as empty
        with a buyer's thread in it.

        All four, or the copy must not promise replies.
        """
        found = self.mail_transport(market)
        if found is None:
            return False
        return bool(
            found["signed_in"]
            and found["view"]
            and found["handoff_address"]
            and found["verified_ts"]
        )

    # --- conversations -----------------------------------------------------------------------

    def mail_thread(self, provider_thread_id: str) -> MailThread | None:
        rows = self._db.query(
            "SELECT provider_thread_id, thread_id, market, relay_address, posting_url, state, "
            "closed_reason, handoff_sent_ts, off_relay_ts FROM mail_relay_threads "
            "WHERE provider_thread_id = ?",
            (provider_thread_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return MailThread(
            provider_thread_id=row["provider_thread_id"],
            thread_id=row["thread_id"],
            market=row["market"],
            relay_address=row["relay_address"] or "",
            posting_url=row["posting_url"] or "",
            state=row["state"],
            closed_reason=row["closed_reason"] or "",
            handoff_sent_ts=row["handoff_sent_ts"],
            off_relay_ts=row["off_relay_ts"],
        )

    def mail_thread_by_thread_id(self, thread_id: str) -> MailThread | None:
        """The relay conversation behind a threads row, or `None` if there is no record of one.

        The send path holds a thread and needs the conversation: which provider conversation to
        open, where the next reply goes, whether the invitation has gone out, and which leg it is
        on. Looked up rather than carried on the thread row, because every one of those facts is
        volatile and the threads table is not where volatile transport state belongs.
        """
        rows = self._db.query(
            "SELECT provider_thread_id FROM mail_relay_threads WHERE thread_id = ? "
            "ORDER BY last_seen_ts DESC LIMIT 1",
            (thread_id,),
        )
        if not rows:
            return None
        return self.mail_thread(rows[0]["provider_thread_id"])

    def upsert_mail_thread(
        self,
        *,
        provider_thread_id: str,
        thread_id: str,
        market: str,
        relay_address: str = "",
        posting_url: str = "",
        off_relay: bool = False,
        now: float | None = None,
    ) -> None:
        """Record a conversation, or refresh the volatile parts of one already known.

        `relay_address` is overwritten every time on purpose — it is where the *next* reply goes and
        craigslist mints a new one per view, so the newest message's address is the only one worth
        keeping. `posting_url` and `off_relay_ts` are sticky: the permalink does not rotate, and a
        conversation that has moved off the relay does not move back.
        """
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO mail_relay_threads (provider_thread_id, thread_id, market, "
                "relay_address, posting_url, state, off_relay_ts, first_seen_ts, last_seen_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (provider_thread_id) DO UPDATE SET "
                "relay_address = excluded.relay_address, "
                "posting_url = CASE WHEN excluded.posting_url = '' "
                "THEN mail_relay_threads.posting_url ELSE excluded.posting_url END, "
                "off_relay_ts = COALESCE(mail_relay_threads.off_relay_ts, excluded.off_relay_ts), "
                "last_seen_ts = excluded.last_seen_ts",
                (
                    provider_thread_id,
                    thread_id,
                    market,
                    relay_address,
                    posting_url,
                    MAIL_STATE_OPEN,
                    now if off_relay else None,
                    now,
                    now,
                ),
            )

    def close_mail_thread(
        self, provider_thread_id: str, state: str, reason: str = "", now: float | None = None
    ) -> None:
        """End a conversation, with the reason the seller needs to hear.

        The reason is kept because two of the endings look identical from outside and mean opposite
        things to a seller: a buyer who opted out made a choice, and a relay address that expired
        before we answered is our latency.
        """
        if state not in MAIL_STATES:
            raise ValueError(f"unknown mail thread state: {state!r}")
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE mail_relay_threads SET state = ?, closed_reason = ?, last_seen_ts = ? "
                "WHERE provider_thread_id = ?",
                (state, reason, now, provider_thread_id),
            )

    def mark_mail_handoff_sent(self, provider_thread_id: str, now: float | None = None) -> None:
        """The invitation has gone out on this conversation, so it must not go out again.

        Durable because a restart that forgot would repeat it every message, which reads as a bot
        and buries the answer the buyer actually asked for.
        """
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE mail_relay_threads SET handoff_sent_ts = COALESCE(handoff_sent_ts, ?) "
                "WHERE provider_thread_id = ?",
                (now, provider_thread_id),
            )

    # --- idempotence --------------------------------------------------------------------------

    def mail_messages_seen(self, provider_message_ids) -> set:
        """Which of these messages have already been folded in.

        Asked in one query rather than per message: a read returns a whole tail, and the answer
        decides which rows are new.
        """
        wanted = [str(found) for found in provider_message_ids if str(found or "")]
        if not wanted:
            return set()
        marks = ",".join("?" for _ in wanted)
        rows = self._db.query(
            "SELECT provider_message_id FROM mail_relay_seen "
            f"WHERE provider_message_id IN ({marks})",
            tuple(wanted),
        )
        return {row["provider_message_id"] for row in rows}

    def mark_mail_messages_seen(
        self, provider_thread_id: str, provider_message_ids, now: float | None = None
    ) -> None:
        """Record that these messages have been folded in.

        **Must be called in the same transaction as the transcript rows they became.** Written
        after a crash-free commit of one but not the other, every read re-journals the buyer's
        message and the agent answers it again on every tick.
        """
        now = _now() if now is None else now
        wanted = [str(found) for found in provider_message_ids if str(found or "")]
        if not wanted:
            return
        with self._db.transaction() as conn:
            conn.executemany(
                "INSERT INTO mail_relay_seen (provider_message_id, provider_thread_id, seen_ts) "
                "VALUES (?, ?, ?) ON CONFLICT (provider_message_id) DO NOTHING",
                [(found, provider_thread_id, now) for found in wanted],
            )
