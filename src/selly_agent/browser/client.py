"""The daemon's Playwright MCP client: JSON-RPC over a stdio subprocess.

The passes already reach the browser through Playwright MCP, so the daemon becomes another client of
it rather than hand-rolling CDP — the lesson of the `_MiniWS` WebSocket code this replaces.
Transport is a subprocess and a pair of pipes: no socket, no port, nothing to authenticate, and
nothing for another process on the machine to connect to.

The shape follows `rail/client.py` — typed errors, no internal retry (a lane backs off; a hot retry
against a marketplace is the anti-automation tell), timeouts as named constants, and injection as a
factory closure so an unavailable browser surfaces where it can be reported rather than at startup.

One mutex guards the whole client. The inbox lane and the reply sink run on different daemon threads
and genuinely overlap — a reply can be sent while the lane is mid-read — and they share one Chrome
tab, so every call serializes. Operations that span several calls (navigate, then read; locate, then
type, then verify) hold it for the whole sequence via `exclusive()`.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from collections import deque
from contextlib import contextmanager

log = logging.getLogger(__name__)

# The npx invocation used when config.playwright_mcp_cmd is unset. The installer pre-installs the
# package so this resolves locally; a cold npx would fetch from the network on the hot path.
DEFAULT_MCP_PACKAGE = "@playwright/mcp"
_PROTOCOL_VERSION = "2025-06-18"

# A tool call is a browser action: a navigate on a slow marketplace page is seconds, so this is
# generous. It is a backstop against a wedged server, not a latency budget.
DEFAULT_TIMEOUT_SEC = 45.0
# Spawning node and completing the MCP handshake.
STARTUP_TIMEOUT_SEC = 30.0
# stderr kept for diagnosis — enough to carry a node stack trace into an error message.
_STDERR_LINES = 40
_SHUTDOWN_JOIN_SEC = 5.0

# The response sections Playwright MCP renders (`### <Title>`); we read Result and Error.
_SECTION_PREFIX = "### "
_RESULT_SECTION = "Result"
_ERROR_SECTION = "Error"


class BrowserError(Exception):
    """Base for browser failures. Messages are caller-facing and carry no secret."""


class BrowserUnavailable(BrowserError):
    """The browser layer cannot run at all: node/npx absent, or the server died at startup.

    Distinct from a failed action because the response is different — the daemon keeps running with
    its browser lanes skipped and one needs-me notice, rather than treating every market as quiet.
    """


class BrowserTransportError(BrowserError):
    """The server was there but the exchange failed: it exited mid-call, timed out, or sent a frame
    we could not parse."""


class BrowserToolError(BrowserError):
    """The server ran the tool and the tool itself failed (a selector matched nothing, a navigation
    was refused). The browser is healthy; the action is not."""


def default_command(cdp_endpoint: str) -> list:
    return ["npx", "--yes", DEFAULT_MCP_PACKAGE, "--cdp-endpoint", cdp_endpoint]


def cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def result_text(payload: dict) -> str:
    """The concatenated text of an MCP tool result's content blocks."""
    parts = []
    for block in payload.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def sections(text: str) -> dict:
    """Split a Playwright MCP response into its `### Title` sections.

    The server renders one text block with markdown section headers — Error, Result, Ran Playwright
    code, Page, Snapshot — so a caller that wants the return value of an evaluate reads the Result
    section rather than scraping the whole body.
    """
    found: dict = {}
    title = None
    body: list = []
    for line in text.splitlines():
        if line.startswith(_SECTION_PREFIX):
            if title is not None:
                found[title] = "\n".join(body).strip()
            title = line[len(_SECTION_PREFIX) :].strip()
            body = []
        elif title is not None:
            body.append(line)
    if title is not None:
        found[title] = "\n".join(body).strip()
    return found


def evaluate_result(text: str):
    """The value a `browser_evaluate` returned, decoded from its Result section.

    The server JSON-encodes the function's return value into that section, so a JS artifact can hand
    back a real structure (a list of message bubbles) instead of a string we would have to parse.
    A function that returns nothing has no Result section, which reads as None.
    """
    body = sections(text).get(_RESULT_SECTION)
    if body is None or not body.strip():
        return None
    # A code fence appears when the server attaches one to a section; strip it before decoding.
    stripped = body.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    try:
        return json.loads(stripped)
    except ValueError as exc:
        raise BrowserTransportError("browser evaluate returned an undecodable result") from exc


class BrowserClient:
    """One Playwright MCP subprocess, started on first use and reused for the daemon's lifetime."""

    def __init__(
        self,
        *,
        command: list,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        startup_timeout_sec: float = STARTUP_TIMEOUT_SEC,
    ):
        self._command = list(command)
        self._timeout = timeout_sec
        self._startup_timeout = startup_timeout_sec
        # Re-entrant so a compound operation can hold the lock across its own call_tool calls.
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._stderr: deque = deque(maxlen=_STDERR_LINES)
        self._next_id = 0
        self._tab_opened = False

    # --- lifecycle ----------------------------------------------------------------------------

    @contextmanager
    def exclusive(self):
        """Hold the client for a multi-call operation, so a read and a send never interleave."""
        with self._lock:
            yield self

    def _start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv is a config list, never a shell string
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            raise BrowserUnavailable(
                f"could not start the browser server ({self._command[0]!r} not found?) — "
                "install Node and the Playwright MCP package, or set playwright_mcp_cmd"
            ) from exc
        self._proc = proc
        self._lines = queue.Queue()
        self._stderr = deque(maxlen=_STDERR_LINES)
        self._next_id = 0
        self._tab_opened = False
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        self._handshake()

    def _read_stdout(self, proc) -> None:
        try:
            for line in proc.stdout:
                if line.strip():
                    self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)  # sentinel: the pipe closed, so no reply is ever coming

    def _read_stderr(self, proc) -> None:
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _handshake(self) -> None:
        self._rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "selly-agent", "version": "1"},
            },
            timeout=self._startup_timeout,
        )
        self._notify("notifications/initialized")

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is None and self._tab_opened:
                # Leave the warm Chrome as we found it: our tab is ours to clean up. A hard kill
                # skips this and leaves one tab behind, which is untidy but harmless.
                try:
                    self.call_tool("browser_tabs", {"action": "close"})
                except BrowserError:
                    pass
            self._proc = None
            try:
                proc.terminate()
                proc.wait(timeout=_SHUTDOWN_JOIN_SEC)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    # --- JSON-RPC -----------------------------------------------------------------------------

    def _stderr_tail(self) -> str:
        return " | ".join(line for line in self._stderr if line.strip())[-400:]

    def _notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def _write(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise BrowserTransportError("the browser server is not running")
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise BrowserTransportError("could not write to the browser server") from exc

    def _rpc(self, method: str, params: dict, timeout: float | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = timeout if timeout is not None else self._timeout
        while True:
            try:
                line = self._lines.get(timeout=deadline)
            except queue.Empty as exc:
                raise BrowserTransportError(f"browser server timed out on {method}") from exc
            if line is None:
                tail = self._stderr_tail()
                raise BrowserTransportError(
                    f"browser server exited during {method}" + (f": {tail}" if tail else "")
                )
            try:
                message = json.loads(line)
            except ValueError as exc:
                raise BrowserTransportError("browser server sent an unparseable frame") from exc
            if not isinstance(message, dict):
                raise BrowserTransportError("browser server sent a non-object frame")
            # Notifications and replies to a call we already gave up on are not ours; skip them.
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise BrowserToolError(str(message["error"].get("message", "browser error")))
            result = message.get("result")
            if not isinstance(result, dict):
                raise BrowserTransportError(f"browser server returned no result for {method}")
            return result

    # --- tools --------------------------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call one Playwright tool and return its response text. Raises rather than retrying."""
        with self._lock:
            self._start()
            payload = self._rpc("tools/call", {"name": name, "arguments": arguments})
            text = result_text(payload)
            if payload.get("isError"):
                detail = sections(text).get(_ERROR_SECTION) or text
                raise BrowserToolError(f"{name} failed: {detail.strip()[:300]}")
            return text

    def evaluate(self, function: str, *, target: str | None = None, element: str | None = None):
        """Run a JS function in the page and return its value.

        `function` is a function expression — `() => {...}`, or `(element) => {...}` when a
        target is given. With a target this is a *locate-and-read* on that element; the daemon
        uses it for reads only, and never to set a field's value (synthetic value-setting is
        untrusted input, the fingerprint the whole real-session posture exists to avoid).
        """
        arguments: dict = {"function": function}
        if target is not None:
            arguments["target"] = target
            arguments["element"] = element or "element to read"
        return evaluate_result(self.call_tool("browser_evaluate", arguments))

    def navigate(self, url: str) -> None:
        with self._lock:
            self.ensure_tab()
            self.call_tool("browser_navigate", {"url": url})

    def ensure_tab(self) -> None:
        """Make sure this client is driving its own tab, creating it once.

        The daemon owns this server process exclusively, so the tab it opens stays the current one —
        that is the handle. Nothing here selects a tab by index or guesses one by host: indices
        renumber whenever any tab opens or closes, and a tab picked by host could be one a pass is
        mid-flow on.
        """
        with self._lock:
            self._start()
            if self._tab_opened:
                return
            self.call_tool("browser_tabs", {"action": "new"})
            self._tab_opened = True
