"""The channel manager — the daemon's registry of running channel providers.

Provider-agnostic: it is handed a `{name: provider}` map of the providers that *exist* (each a
module/object exposing `start(**deps) -> handle`, `is_configured() -> bool`), and it owns which
are *running*. A provider runs only when it is registered — at boot for those already configured,
and at runtime when `connect` brings one up — so a daemon with no channel set up starts no channel
thread at all (rather than a thread that idles doing nothing).

register/deregister are the symmetric pair: `register` starts a provider and tracks its handle
(idempotent); `deregister` shuts one down and drops it; `shutdown_all` tears them all down at
daemon stop. Handles are shut down outside the lock (shutdown joins a thread).
"""

from __future__ import annotations

import threading


class ChannelManager:
    def __init__(self, *, providers: dict, bus, store, config, scheduler):
        self._providers = providers  # name -> provider (has start(**deps) + is_configured())
        self._deps = {"bus": bus, "store": store, "config": config, "scheduler": scheduler}
        self._handles: dict = {}
        self._lock = threading.Lock()

    def register(self, name: str) -> None:
        """Start `name` and track its handle. Idempotent — a re-register while running is a no-op
        (the running provider picks up any new state on its own)."""
        with self._lock:
            if name in self._handles:
                return
            self._handles[name] = self._providers[name].start(**self._deps)

    def deregister(self, name: str) -> None:
        """Stop `name` (join its thread, remove its scheduler lanes) and drop it. No-op if not
        running."""
        with self._lock:
            handle = self._handles.pop(name, None)
        if handle is not None:
            handle.shutdown()

    def register_configured(self) -> None:
        """Start every provider that is already set up (e.g. a returning user with a bound bot)."""
        for name, provider in self._providers.items():
            if provider.is_configured():
                self.register(name)

    def shutdown_all(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            handle.shutdown()
