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
import re
import shutil
import subprocess
import threading
from collections import deque
from contextlib import contextmanager

from selly_agent import paths, spawn

log = logging.getLogger(__name__)

# The package spawned via npx when config.playwright_mcp_cmd is unset. The installer verifies the
# spawn and warms the package, and the daemon re-warms it at startup, so this resolves from the npx
# cache; a cold npx would fetch from the network on the hot path.
DEFAULT_MCP_PACKAGE = "@playwright/mcp"
# Pinned rather than floating. Unpinned, every machine resolves whatever npm called latest the first
# time it asked, so a bad upstream release reaches sellers with nothing in between — and the npx
# cache key stops being exact. Bumping this is a one-line change that ships like any other.
MCP_VERSION = "0.0.78"
PINNED_MCP_SPEC = f"{DEFAULT_MCP_PACKAGE}@{MCP_VERSION}"
_PROTOCOL_VERSION = "2025-06-18"
# How much of its own output the server may keep before evicting the oldest. Small on purpose: these
# files are page content, useful for diagnosis for a short while and not worth hoarding.
OUTPUT_MAX_BYTES = 32 * 1024 * 1024

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
# What the server writes for a function that returned nothing — the one body that is not JSON.
_UNDEFINED = "undefined"

# How the server refuses every page-level tool — evaluate, snapshot, click — while the tab it is
# pointed at carries "modal state": an open dialog or an open file chooser.
#
# That state is per tab, in the server's own memory, and only the tool that owns it clears it
# (`browser_handle_dialog`, `browser_file_upload`). A second server attached to the same Chrome
# wraps every tab in it, not only the ones it opened, so it records the same dialogs and file
# choosers — and never clears them, because it is not the one handling them. A file chooser a
# publish pass opened and consumed therefore leaves *our* view of that tab refusing every read,
# permanently: the state outlives the page, so navigating does not clear it, and the tool that
# would is one we must never call on a flow that is not ours.
_MODAL_STATE_MARKER = "does not handle the modal state"


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
    """The npx invocation, with the server's own output kept somewhere we chose.

    `--output-dir` is not optional in practice: the server saves a page snapshot per navigation, and
    left to itself it writes them into whatever directory it started in — a developer's checkout, or
    wherever the daemon was launched from. The size cap makes it evict its own old files, so this
    never grows without bound.
    """
    return [
        "npx",
        "--yes",
        PINNED_MCP_SPEC,
        "--cdp-endpoint",
        cdp_endpoint,
        "--output-dir",
        str(paths.browser_output_dir()),
        "--output-max-size",
        str(OUTPUT_MAX_BYTES),
    ]


def cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def ensure_available(command) -> None:
    """Raise `BrowserUnavailable` when the command's binary is not on PATH.

    The cheap check that lets a factory fail before anything spawns, so a machine with no Node
    reports the browser layer absent — with the install hint — instead of the absence surfacing
    later as a failed read or a send that reserved pacing for nothing.
    """
    if not command or shutil.which(str(command[0])) is None:
        head = command[0] if command else "(empty command)"
        raise BrowserUnavailable(
            f"{head!r} is not installed — install Node and the Playwright MCP package, "
            "or set playwright_mcp_cmd"
        )


_PAGE_STATE_JS = "() => ({visible: document.visibilityState === 'visible', url: location.href})"

# How long to give a tab we just brought forward to notice. Chrome tells the renderer it became
# visible asynchronously, so reading the state straight after selecting reports the old value and a
# healthy tab looks like it refused to come forward.
VISIBILITY_WAIT_MS = 3000

_AWAIT_VISIBLE_JS = f"""async () => {{
  if (document.visibilityState !== 'visible') {{
    await new Promise((resolve) => {{
      document.addEventListener('visibilitychange', resolve, {{ once: true }});
      setTimeout(resolve, {VISIBILITY_WAIT_MS});
    }});
  }}
  return {{ visible: document.visibilityState === 'visible', url: location.href }};
}}"""

# A line of the server's tab listing, marking the tab our own calls act on: `- 2: (current) [t](u)`.
_CURRENT_TAB_RE = re.compile(r"^-\s*(\d+):\s*\(current\)")


def same_page(left: str, right: str) -> bool:
    """Whether two URLs address the same page, ignoring a query string, a fragment and a trailing
    slash — the differences a navigation may introduce on its own."""
    return _page_key(left) == _page_key(right)


def _page_key(url: str) -> str:
    return str(url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")


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

    A function that returns nothing is the one case that is not JSON: the server writes the literal
    `undefined`, which reads as None here. That matters beyond tidiness — an artifact that falls off
    its own end would otherwise look like a transport failure, and for a read that means a market
    reporting itself blind rather than simply empty.
    """
    body = sections(text).get(_RESULT_SECTION)
    if body is None or not body.strip():
        return None
    # A code fence appears when the server attaches one to a section; strip it before decoding.
    stripped = body.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    if stripped == _UNDEFINED:
        return None
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
        # The page our tab was last sent to, so a tab given up mid-operation is replaced by one
        # showing what the caller had already navigated to.
        self._last_url: str | None = None
        # Set while replacing a tab, so the calls that do the replacing cannot themselves trigger
        # another replacement.
        self._reopening = False

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
                spawn.resolve(self._command),
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
        # The framing is one JSON message per "\n". Text mode would otherwise write os.linesep,
        # putting a carriage return the server has to guess about at the end of every request.
        proc.stdin.reconfigure(newline="")
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
        try:
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
        except BrowserTransportError as exc:
            # A server that never completed startup is the browser layer being absent, not a
            # failed action — the caller's response is skip-and-notify, never the blind counter.
            raise BrowserUnavailable(f"the browser server did not start: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is None and self._tab_opened:
                # Leave the warm Chrome as we found it: our tab is ours to clean up. A hard kill
                # skips this and leaves one tab behind, which is untidy but harmless. Called without
                # recovery, because nothing on the way out should be opening a tab.
                try:
                    self._call_once("browser_tabs", {"action": "close"})
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
        """Call one Playwright tool and return its response text. Raises rather than retrying.

        The one exception is a tool refused because the tab carries modal state, which is retried
        once on a tab of our own — see `_reopen_after_modal_state` for why that is a recovery and
        not a retry in the sense this client otherwise forbids. Everything else raises on the first
        failure: a hot retry against a marketplace is the anti-automation tell, so backing off is
        the lane's job.

        Repeating that one call is safe even under the send path, where a repeat would mean a buyer
        hearing from us twice: the refusal comes from a gate the server applies *before* running the
        tool, so a call it refuses has had no effect on the page to repeat.
        """
        with self._lock:
            try:
                return self._call_once(name, arguments)
            except BrowserToolError as exc:
                if self._reopening or _MODAL_STATE_MARKER not in str(exc):
                    raise
                log.warning("browser: %s — taking a fresh tab", exc)
            self._reopen_after_modal_state()
            # Deliberately not through `call_tool`: one recovery per call, so a tab that somehow
            # reports modal state again surfaces the failure instead of reopening tabs in a loop.
            return self._call_once(name, arguments)

    def _call_once(self, name: str, arguments: dict) -> str:
        with self._lock:
            self._start()
            payload = self._rpc("tools/call", {"name": name, "arguments": arguments})
            text = result_text(payload)
            if payload.get("isError"):
                detail = sections(text).get(_ERROR_SECTION) or text
                raise BrowserToolError(f"{name} failed: {detail.strip()[:300]}")
            return text

    def _reopen_after_modal_state(self) -> None:
        """Give up the tab we were driving and take a fresh one, back on the page we were reading.

        A tab that reports modal state is unusable to us from here (see `_MODAL_STATE_MARKER`), and
        a tab we have only just opened cannot carry any — so recovery is a new tab rather than a
        repair. Without it the daemon reads that tab as an unreadable market on every tick until it
        restarts, which is a market reporting itself blind for a reason that has nothing to do with
        the market.

        The replacement is sent to the page the caller had navigated to, so the read it was in the
        middle of resumes on what it expected rather than on `about:blank`.
        """
        self._reopening = True
        self._tab_opened = False  # what we held is not a tab we can drive any more
        try:
            self.ensure_tab()
            if self._last_url is not None:
                self._call_once("browser_navigate", {"url": self._last_url})
        finally:
            self._reopening = False

    def evaluate(self, function: str, *, target: str | None = None, element: str | None = None):
        """Run a JS function in the page and return its value.

        `function` is a function expression — `() => {...}`, or `(element) => {...}` when a
        target is given. With a target this is a *locate-and-read* on that element, plus the one
        market-supplied submit that dispatches into the composer. Nothing here ever sets a field's
        value: the text a buyer will see is always typed as real input.
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
            self._last_url = url

    def ensure_tab(self) -> None:
        """Make sure this client is driving its own tab, creating it once.

        The daemon owns this server process exclusively, so the tab it opens stays the one its calls
        act on. Nothing here selects a tab by index or guesses one by host: indices renumber
        whenever any tab opens or closes, and a tab picked by host could be one a pass is mid-flow
        on.

        The handle is a claim, not a guarantee. The server names no tab in a call — every tool acts
        on whatever it currently considers the current tab, and it re-points that silently when a
        tab closes, so a tab of ours that the seller closes hands our calls to whatever tab is left.
        Nothing here can see that; what it costs, and how the client gets out of it, is
        `_reopen_after_modal_state`.
        """
        with self._lock:
            self._start()
            if self._tab_opened:
                return
            self.call_tool("browser_tabs", {"action": "new"})
            self._tab_opened = True

    def ensure_frontmost(self, url: str) -> None:
        """Make the tab this client drives the active tab of its window, so keys reach it.

        Chrome routes key events only to a visible renderer, and a tab is visible when it is the
        active tab of its window — so a tab opened in the background swallows every keystroke in
        silence. Filling a text box still works there (that input is injected below the page), which
        is what makes the failure so quiet: the text lands, the key that would commit it never
        arrives, and nothing reports an error.

        Only what needs typing needs this: a scripted read runs fine on a background tab, which is
        what keeps the read lane out of the seller's way. Nothing happens when the tab is already
        active — the steady state on the agent's own Chrome — so its window comes forward at most
        once rather than on every send.

        Selecting is by index, and an index is a position that renumbers whenever any tab opens or
        closes; worse, selecting repoints every later call at whatever was chosen. So the page is
        read back afterwards: a tab that came forward and is not the page we navigated to belongs to
        something else, and this abandons our own tab handle and raises rather than typing into it.
        """
        with self._lock:
            self._start()
            if (self.evaluate(_PAGE_STATE_JS) or {}).get("visible"):
                return
            self.call_tool("browser_tabs", {"action": "select", "index": self._current_tab_index()})
            state = self.evaluate(_AWAIT_VISIBLE_JS) or {}
            if not same_page(state.get("url") or "", url):
                self._tab_opened = False  # not ours any more; the next call opens a fresh one
                raise BrowserToolError(
                    f"selecting our own tab landed on {state.get('url')!r}, not {url!r}"
                )
            if not state.get("visible"):
                raise BrowserToolError(f"our tab would not come forward — {url!r} is still hidden")

    def _current_tab_index(self) -> int:
        """Where the server currently numbers the tab our calls act on."""
        for line in self.call_tool("browser_tabs", {"action": "list"}).splitlines():
            found = _CURRENT_TAB_RE.match(line.strip())
            if found:
                return int(found.group(1))
        raise BrowserToolError("the browser server reports no current tab")
