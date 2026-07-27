"""PassSpec — the one internal representation every harness emitter consumes.

A pass is fully described by this; the claude and codex emitters are pure functions of it. Its
fields are validated at construction so a malformed spec fails at the source, not deep in an
emitter or (worse) at spawn time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PassSpec:
    prompt: str
    model: str
    mcp_endpoint: str
    mcp_token: str
    allowed_tools: tuple = ()
    max_turns: int | None = None
    output_format: str = "stream-json"
    permission_mode: str = "default"
    append_system_prompt: str | None = None
    # Whether this pass may reach the harness's own web research tools. Off by default: the web is
    # untrusted input, so a flow gets it only because its skills call for research. Carried on the
    # spec rather than hand-written into a config so the emitters' round-trip validators cover it.
    web_tools: bool = False
    server_name: str = "selly"

    def __post_init__(self) -> None:
        if not self.prompt:
            raise ValueError("PassSpec.prompt must be non-empty")
        if not self.model:
            raise ValueError("PassSpec.model must be non-empty")
        if not self.mcp_endpoint.startswith(("http://", "https://")):
            raise ValueError("PassSpec.mcp_endpoint must be an http(s) URL")
        if not self.mcp_token:
            raise ValueError("PassSpec.mcp_token must be non-empty")
        if not self.server_name or not self.server_name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("PassSpec.server_name must be an identifier")
