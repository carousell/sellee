"""Pure decision engines: money and safety logic ported from the legacy CLIs.

Every module here is a pure decision layer over state the caller supplies — engines never import
tool or server code, never open a socket, and never touch the store directly. A tool composes an
engine with the store (one transaction per decision); the engine just decides. This keeps the
money/safety logic unit-testable in isolation and keeps the network-free guarantee structural.
"""

from __future__ import annotations
