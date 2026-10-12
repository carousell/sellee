"""The channel manager — the daemon's registry of running channel providers.

Provider-agnostic: it is handed a `{name: provider}` map of the providers that *exist* (each a
module/object exposing `start(**deps) -> handle`, `is_configured() -> bool`), and it owns which
are *running*. A provider runs only when it is registered — at boot for those already configured,
and at runtime when `connect` brings one up — so a daemon with no channel set up starts no channel
thread at all (rather than a thread that idles doing nothing).

register/deregister are the symmetric pair: `register` starts a provider and tracks its handle
(idempotent); `deregister` shuts one down and drops it; `shutdown_all` tears them all down at
daemon stop. Handles are shut down outside the lock (shutdown joins a thread).

At most one provider is ever meant to be running at a time: the bound channel is a singleton row
with one `adapter` (see `store.arm_bind`), and every provider registers its drain lane on the
shared scheduler under the same literal task name (`notice_drain` — see channel/*/provider.py).
Two providers running at once would silently overwrite each other's scheduler task rather than
error, so `register` enforces the invariant itself: starting a different provider first deregisters
whichever one is currently running, and `register_configured` picks at most one when more than one
happens to be configured.

The typing keeper is a thread rather than a lane, so it shares no namespace to collide in and the
shared task name does not cover it. What covers it is the handle's own bounded join: `deregister`
runs under the register lock and waits for the outgoing keeper to stop before the next provider
starts, and a keeper that outlives that join is logged rather than left silently pulsing into a
chat the new provider now owns.
"""

from __future__ import annotations

import threading


class ChannelManager:
    def __init__(self, *, providers: dict, bus, store, config, scheduler):
        self._providers = providers  # name -> provider (has start(**deps) + is_configured())
        self._deps = {"bus": bus, "store": store, "config": config, "scheduler": scheduler}
        self._handles: dict = {}
        self._lock = threading.Lock()
        self._register_lock = threading.Lock()

    def register(self, name: str) -> None:
        """Start `name` and track its handle. Idempotent — a re-register while running is a no-op
        (the running provider picks up any new state on its own). Only one provider is ever meant
        to be running (see module docstring), so registering a different provider first
        deregisters whichever one is currently running rather than letting both stay up.

        The whole method runs under `_register_lock`, held for the full deregister-then-start
        sequence rather than just around the `_handles` reads/writes: the control server is
        threaded, so two connect calls can land at once, and a lock scoped only to the dict
        accesses would still let two *different* names both see an empty `others` and both start —
        two providers live together, silently colliding on the scheduler task names they share.
        Serializing the whole method closes that the same way it closes the same-name case."""
        with self._register_lock:
            with self._lock:
                if name in self._handles:
                    return
                others = [n for n in self._handles if n != name]
            for other in others:
                self.deregister(other)
            with self._lock:
                self._handles[name] = self._providers[name].start(**self._deps)

    def deregister(self, name: str) -> None:
        """Stop `name` (join its thread, remove its scheduler lanes) and drop it. No-op if not
        running."""
        with self._lock:
            handle = self._handles.pop(name, None)
        if handle is not None:
            handle.shutdown()

    def register_configured(self) -> None:
        """Start every provider that is already set up (e.g. a returning user with a bound bot).
        When more than one happens to be configured — a seller can leave a stale token file
        behind after switching from one provider to the other — only the one matching the channel
        row's current `adapter` is started; `register`'s own invariant would otherwise leave the
        outcome to dict iteration order instead of the seller's actual bound provider."""
        configured = [name for name, p in self._providers.items() if p.is_configured()]
        if len(configured) > 1:
            store = self._deps["store"]
            adapter = store.get_channel()["adapter"] if store is not None else None
            if adapter in configured:
                configured = [adapter]
        for name in configured:
            self.register(name)

    def shutdown_all(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            handle.shutdown()
