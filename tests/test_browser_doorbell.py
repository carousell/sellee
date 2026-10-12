"""The doorbell: hearing a marketplace ring without opening it.

On a marketplace that polices automation the agent opens the inbox only when the marketplace
notifies its Chrome. What is held here is the hearing — once per notification however often Chrome
replays it, a person's reaction time before the visit, nothing owed for what rang while nobody was
listening, a wake from sleep settled before anything moves — and the honesty: a doorbell that
cannot hear says so, because silence from it would otherwise read as a quiet inbox.
"""

from __future__ import annotations

import json
import random
from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting

from sellee import migrations, paths
from sellee.browser import doorbell
from sellee.config import Config
from sellee.db import Database
from sellee.engines import pacing
from sellee.events import EventBus, EventStore
from sellee.store import Store
from sellee.store.browser import RING_KEPT_SEC

_FB = "https://www.facebook.com/"


class _Clock:
    def __init__(self, t: float = 1_790_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _ring(tag="mid.$a", *, shown=None, origin=_FB, title="Gerry Tan", body="is it available?"):
    return {"origin": origin, "tag": tag, "shown_ts": shown, "title": title, "body": body}


class _Chrome:
    """What Chrome's notification log answers: a list, or None when it could not be asked."""

    def __init__(self, heard=()):
        self.heard = list(heard)
        self.asked = 0

    def __call__(self, port):
        self.asked += 1
        return None if self.heard is None else list(self.heard)


@pytest.fixture(autouse=True)
def _fb_connected(store):
    seed_setting(store, "connected_markets", ["fb"])


def _deps(store, bus, chrome, clock, port=9222):
    return doorbell.DoorbellDeps(
        store=store,
        bus=bus,
        config=Config(),
        port=lambda: port,
        listen=chrome,
        now=clock,
        rng=random.Random(3),
    )


@contextmanager
def _a_store_of_its_own(root):
    """A freshly migrated store and bus, so one Hypothesis example never hears another's rings.
    Closed when the example ends rather than the test, so their files are not all held open."""
    data_db = Database(root / "sellee.db")
    events_db = Database(root / "events.db")
    try:
        migrations.run_startup_migrations(
            data_db=data_db, events_db=events_db, backups_dir=root / "backups", backups_keep=5
        )
        store = Store(data_db)
        seed_setting(store, "connected_markets", ["fb"])
        yield store, EventBus(EventStore(events_db))
    finally:
        data_db.close()
        events_db.close()


def _texts(store):
    return [n["text"] for n in store.list_queued_notices()]


# --- hearing ------------------------------------------------------------------------------------


def test_a_ring_is_owed_a_visit_after_a_persons_reaction_time(store, bus) -> None:
    clock = _Clock()
    chrome = _Chrome([_ring(shown=clock.t - 2)])

    doorbell.doorbell_lane(_deps(store, bus, chrome, clock))

    assert not store.ring_owed("fb", now=clock.t + pacing.REACTION_FLOOR_SEC - 1)
    assert store.ring_owed("fb", now=clock.t + pacing.REACTION_CEILING_SEC)


def test_a_replayed_ring_is_heard_once(store, bus) -> None:
    clock = _Clock()
    chrome = _Chrome([_ring(shown=clock.t - 2)])
    deps = _deps(store, bus, chrome, clock)

    doorbell.doorbell_lane(deps)
    clock.t += 20
    doorbell.doorbell_lane(deps)

    assert store.ring_count("fb") == 1
    assert len(bus.store.read(kinds=["doorbell.rang"])) == 1


def test_another_sites_notification_is_not_facebook_ringing(store, bus) -> None:
    clock = _Clock()
    chrome = _Chrome([_ring(shown=clock.t - 2, origin="https://www.carousell.sg/")])

    doorbell.doorbell_lane(_deps(store, bus, chrome, clock))

    assert store.ring_count("fb") == 0


def test_what_rang_while_nobody_was_listening_is_kept_and_owed_nothing(store, bus) -> None:
    """The first look after an install or a restart replays days of notifications. They prove the
    doorbell works; they are not a reason to open anything."""
    clock = _Clock()
    chrome = _Chrome([_ring("mid.$old", shown=clock.t - doorbell.STALE_RING_SEC - 60)])

    doorbell.doorbell_lane(_deps(store, bus, chrome, clock))

    assert store.ring_count("fb") == 1
    assert not store.ring_owed("fb", now=clock.t + 10 * pacing.REACTION_CEILING_SEC)


def test_a_wake_from_sleep_is_settled_before_anything_moves(store, bus) -> None:
    """Pushes sent while the laptop slept arrive together on wake, while the network is still
    coming back. The visit they ask for waits until the machine has been awake a while."""
    clock = _Clock()
    chrome = _Chrome()
    deps = _deps(store, bus, chrome, clock)
    doorbell.doorbell_lane(deps)

    clock.t += doorbell.SLEPT_AFTER_SEC + 3600
    chrome.heard = [_ring("mid.$woke", shown=clock.t - 1)]
    doorbell.doorbell_lane(deps)

    assert not store.ring_owed("fb", now=clock.t + doorbell.WAKE_SETTLE_SEC - 1)
    assert store.ring_owed(
        "fb", now=clock.t + doorbell.WAKE_SETTLE_SEC + pacing.REACTION_CEILING_SEC
    )


def test_a_ring_shown_while_the_laptop_slept_is_still_owed_a_visit(store, bus) -> None:
    """Chrome takes a push while the laptop sleeps, and the doorbell first hears it on wake, older
    than a replay would be. It rang while the doorbell was listening, so it is a buyer to answer."""
    clock = _Clock()
    chrome = _Chrome()
    deps = _deps(store, bus, chrome, clock)
    doorbell.doorbell_lane(deps)

    clock.t += 4913  # 82 minutes asleep
    chrome.heard = [_ring("mid.$night", shown=clock.t - 2044)]  # 34 minutes old when first heard
    doorbell.doorbell_lane(deps)

    assert store.ring_owed(
        "fb", now=clock.t + doorbell.WAKE_SETTLE_SEC + pacing.REACTION_CEILING_SEC
    )


def test_an_answered_ring_let_go_of_is_not_owed_again(store, bus) -> None:
    """Chrome may replay a notification for longer than an answered ring is kept. Once let go it is
    no longer recognised, but it is still the ring that was answered, not a buyer to visit."""
    clock = _Clock()
    chrome = _Chrome()
    deps = _deps(store, bus, chrome, clock)
    doorbell.doorbell_lane(deps)
    clock.t += 20
    chrome.heard = [_ring("mid.$month", shown=clock.t - 2)]
    doorbell.doorbell_lane(deps)
    store.answer_rings("fb", heard_before=clock.t, now=clock.t + pacing.REACTION_CEILING_SEC)

    clock.t += RING_KEPT_SEC + 86400
    doorbell.doorbell_lane(deps)
    clock.t += 20
    doorbell.doorbell_lane(deps)

    assert not store.ring_owed("fb", now=clock.t + 10 * doorbell.WAKE_SETTLE_SEC)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    asleep=st.floats(doorbell.SLEPT_AFTER_SEC + 1, 7 * 86400),
    into_sleep=st.floats(0.01, 0.99),
)
def test_any_ring_shown_after_the_first_look_is_owed_a_visit(tmp_path_factory, asleep, into_sleep):
    with _a_store_of_its_own(tmp_path_factory.mktemp("example")) as (store, bus):
        clock = _Clock()
        chrome = _Chrome()
        deps = _deps(store, bus, chrome, clock)
        doorbell.doorbell_lane(deps)
        looked_at = clock.t

        clock.t += asleep
        chrome.heard = [_ring(shown=looked_at + asleep * into_sleep)]
        doorbell.doorbell_lane(deps)

        assert store.ring_owed(
            "fb", now=clock.t + doorbell.WAKE_SETTLE_SEC + pacing.REACTION_CEILING_SEC
        )


def test_nothing_is_listened_for_when_no_market_rings(store, bus) -> None:
    seed_setting(store, "connected_markets", ["carousell"])
    chrome = _Chrome()

    doorbell.doorbell_lane(_deps(store, bus, chrome, _Clock()))

    assert chrome.asked == 0


def test_a_paused_agent_does_not_listen(store, bus) -> None:
    store.set_paused(True)
    chrome = _Chrome()

    doorbell.doorbell_lane(_deps(store, bus, chrome, _Clock()))

    assert chrome.asked == 0


# --- saying so when it cannot hear ----------------------------------------------------------------


def test_a_doorbell_that_cannot_hear_says_so_once(store, bus) -> None:
    clock = _Clock()
    chrome = _Chrome()
    chrome.heard = None
    deps = _deps(store, bus, chrome, clock)

    for _ in range(doorbell.DEAF_AFTER + 3):
        doorbell.doorbell_lane(deps)
        clock.t += 20

    texts = _texts(store)
    assert len(texts) == 1
    assert "can't hear" in texts[0]


def test_a_chrome_that_is_not_running_is_a_doorbell_that_cannot_hear(store, bus) -> None:
    clock = _Clock()
    deps = _deps(store, bus, _Chrome(), clock, port=None)

    for _ in range(doorbell.DEAF_AFTER):
        doorbell.doorbell_lane(deps)
        clock.t += 20

    assert len(_texts(store)) == 1


def _preferences(setting: int | None) -> None:
    profile = paths.browser_profile_dir() / "Default"
    profile.mkdir(parents=True, exist_ok=True)
    exceptions = {}
    if setting is not None:
        exceptions = {"notifications": {"https://www.facebook.com:443,*": {"setting": setting}}}
    (profile / "Preferences").write_text(
        json.dumps({"profile": {"content_settings": {"exceptions": exceptions}}})
    )


def test_notifications_switched_off_in_chrome_are_said_once(store, bus, xdg_tmp) -> None:
    _preferences(2)
    clock = _Clock()
    deps = _deps(store, bus, _Chrome(), clock)

    doorbell.doorbell_lane(deps)
    clock.t += doorbell.PERMISSION_CHECK_SEC + 1
    doorbell.doorbell_lane(deps)

    texts = _texts(store)
    assert len(texts) == 1
    assert "notifications" in texts[0] and "switched off" in texts[0]


def test_notifications_never_allowed_are_off_too(store, bus, xdg_tmp) -> None:
    _preferences(None)

    doorbell.doorbell_lane(_deps(store, bus, _Chrome(), _Clock()))

    assert len(_texts(store)) == 1


def test_notifications_allowed_say_nothing(store, bus, xdg_tmp) -> None:
    _preferences(1)

    doorbell.doorbell_lane(_deps(store, bus, _Chrome(), _Clock()))

    assert _texts(store) == []


def test_a_profile_this_machine_cannot_read_is_not_a_reason_to_worry_the_seller(
    store, bus, xdg_tmp
) -> None:
    """In the container the agent's Chrome, and its profile, are on the seller's own desktop."""
    doorbell.doorbell_lane(_deps(store, bus, _Chrome(), _Clock()))

    assert _texts(store) == []


def test_a_doorbell_that_has_heard_nothing_for_days_says_so_once(store, bus, xdg_tmp) -> None:
    """Facebook rings a few times a day on its own. Days of silence from a doorbell that is
    listening is a session that died without saying so — and the one thing this must never do is
    let that pass for an inbox with nothing in it."""
    _preferences(1)
    clock = _Clock()
    deps = _deps(store, bus, _Chrome(), clock)

    doorbell.doorbell_lane(deps)
    clock.t += doorbell.SILENT_AFTER_SEC + 1
    doorbell.doorbell_lane(deps)
    clock.t += 3600
    doorbell.doorbell_lane(deps)

    texts = _texts(store)
    assert len(texts) == 1
    assert "haven't heard" in texts[0]


def test_a_recent_ring_means_the_doorbell_is_not_silent(store, bus, xdg_tmp) -> None:
    _preferences(1)
    clock = _Clock()
    chrome = _Chrome()
    deps = _deps(store, bus, chrome, clock)
    doorbell.doorbell_lane(deps)

    clock.t += doorbell.SILENT_AFTER_SEC + 1
    chrome.heard = [_ring("like.9", shown=clock.t - 60)]
    doorbell.doorbell_lane(deps)

    assert _texts(store) == []


# --- leaving the page once the visit is over ------------------------------------------------------


class _Tab:
    def __init__(self, fail=False):
        self.urls: list = []
        self.fail = fail

    def navigate(self, url):
        if self.fail:
            from sellee.browser.client import BrowserToolError

            raise BrowserToolError("gone")
        self.urls.append(url)


def test_a_visit_to_a_market_that_rings_ends_with_the_tab_stepping_away() -> None:
    """Every acquisition tells the tab it is focused and visible. Left on Facebook, it is someone
    sitting in front of Messenger, and a marketplace may hold back a push from a person it thinks
    is already looking — which would silence the doorbell exactly when a buyer writes."""
    from sellee.browser import markets as market_adapters

    tab = _Tab()
    with doorbell.visiting(tab, market_adapters.get_adapter("fb")):
        tab.navigate("https://www.facebook.com/messages/")

    assert tab.urls[-1] == doorbell.AWAY_URL


def test_a_market_read_on_a_timer_is_left_where_it_is() -> None:
    from sellee.browser import markets as market_adapters

    tab = _Tab()
    with doorbell.visiting(tab, market_adapters.get_adapter("carousell")):
        tab.navigate("https://www.carousell.sg/inbox/")

    assert doorbell.AWAY_URL not in tab.urls


def test_stepping_away_is_done_even_when_the_visit_failed_and_never_fails_it() -> None:
    from sellee.browser import markets as market_adapters
    from sellee.browser.client import BrowserToolError

    tab = _Tab()
    with pytest.raises(BrowserToolError):
        with doorbell.visiting(tab, market_adapters.get_adapter("fb")):
            raise BrowserToolError("the read failed")
    assert tab.urls == [doorbell.AWAY_URL]

    broken = _Tab(fail=True)
    with doorbell.visiting(broken, market_adapters.get_adapter("fb")):
        pass  # a tab that will not leave must not turn a finished visit into a failed one


def test_a_blocked_market_is_not_also_called_silent(store, bus, xdg_tmp) -> None:
    """A signed-out account gets no pushes, and the seller has already been told why it stopped.
    Days of silence on top of that is the same news in a worse sentence."""
    _preferences(1)
    store.block_market("fb", "logged_out", ttl_sec=None)
    clock = _Clock()
    deps = _deps(store, bus, _Chrome(), clock)

    doorbell.doorbell_lane(deps)
    clock.t += doorbell.SILENT_AFTER_SEC + 1
    doorbell.doorbell_lane(deps)

    assert not [t for t in _texts(store) if "haven't heard" in t]


# --- the shapes Chrome records a permission in ----------------------------------------------------


def _raw_preferences(exceptions: dict, default: int | None = None) -> None:
    profile = paths.browser_profile_dir() / "Default"
    profile.mkdir(parents=True, exist_ok=True)
    content: dict = {"exceptions": {"notifications": exceptions}}
    prefs: dict = {"profile": {"content_settings": content}}
    if default is not None:
        prefs["profile"]["default_content_setting_values"] = {"notifications": default}
    (profile / "Preferences").write_text(json.dumps(prefs))


def test_a_rule_for_every_facebook_subdomain_counts(xdg_tmp) -> None:
    _raw_preferences({"[*.]facebook.com,*": {"setting": 1}})

    assert doorbell.notifications_allowed("fb") is True


def test_notifications_allowed_for_every_site_count(xdg_tmp) -> None:
    _raw_preferences({}, default=1)

    assert doorbell.notifications_allowed("fb") is True


def test_a_block_on_facebook_wins_over_allowing_every_site(xdg_tmp) -> None:
    _raw_preferences({"https://www.facebook.com:443,*": {"setting": 2}}, default=1)

    assert doorbell.notifications_allowed("fb") is False


def test_a_rule_for_another_site_is_not_facebooks(xdg_tmp) -> None:
    _raw_preferences({"https://www.notfacebook.com:443,*": {"setting": 1}})

    assert doorbell.notifications_allowed("fb") is False
