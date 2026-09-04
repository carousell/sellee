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
import time
from collections import deque
from contextlib import contextmanager

from sellee import paths

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
# What actually gets executed. Named separately because "is this machine able to spawn the server at
# all" is answerable from the binary alone — no CDP endpoint, hence no port, hence askable before
# Chrome has been brought up and chosen one.
SERVER_BINARY = "npx"
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

# How many tool failures in a row make "the browser is healthy; the action is not" stop being a
# credible reading. One failure is a selector that moved or a page that would not load. Three in a
# row, with no success between them, is the server itself — and the daemon's factory pairs this with
# its own proof that Chrome is answering before it acts on it.
#
# Deliberately a count and not a probe. The obvious probe is `browser_tabs {"action": "list"}`, and
# it is unsafe: the server's handler for it calls `ensureTab()`, so asking would create a page and
# repoint the current tab — on a Chrome whose window the seller has closed, diagnosing the fault
# would pop a new window, and on the send path it would run after the commit.
RECYCLE_AFTER_FAILURES = 3


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
    was refused). The browser is healthy; the action is not.

    That claim is true of a single failure and false of a run of them — see
    `RECYCLE_AFTER_FAILURES`.
    """


class BrowserDetached(BrowserError):
    """The server is answering us and has lost Chrome.

    Its process is alive and its JSON-RPC pipe is fine, so nothing here is `BrowserUnavailable`; but
    every browser tool it runs fails, so nothing here is a healthy browser either. On 2026-08-27 a
    server in this state answered the daemon for 28 hours while failing every navigate with
    `async initializeServer: Timeout 30000ms exceeded` — and because that arrives as a tool error,
    the lane counted 126 blind reads and told the seller to go and check a Chrome that was running
    and signed in the whole time.

    Only the daemon's factory raises this: deciding it needs both halves of the diagnosis, and the
    second half — that Chrome itself is answering — is not the client's to know.
    """


def default_command(cdp_endpoint: str) -> list:
    """The npx invocation, with the server's own output kept somewhere we chose.

    `--output-dir` is not optional in practice: the server saves a page snapshot per navigation, and
    left to itself it writes them into whatever directory it started in — a developer's checkout, or
    wherever the daemon was launched from. The size cap makes it evict its own old files, so this
    never grows without bound.
    """
    return [
        SERVER_BINARY,
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
        # Watch mode: bring our tab forward after every navigation, so the seller sees the page we
        # are on. Off by default and set per acquisition from the seller's setting.
        self._follow = False
        # Tool failures since the last success. Read by the daemon's factory, which pairs it with
        # its own probe of Chrome to tell a server that lost the browser from an action that failed.
        self._failing_streak = 0
        # Set by `close()`. A closed client is done: without this, a stale reference still held by
        # an in-flight send would spawn a whole new server on its next call, because `ensure_tab`
        # and `ensure_frontmost` call `_start()` directly and `close()` leaves `_proc` as None. That
        # process would be in no holder and reaped by nobody.
        self._retired = False
        self._started_ts: float | None = None
        # Set by the daemon's factory once it has diagnosed this server as having lost Chrome.
        self._detached: str | None = None

    # --- lifecycle ----------------------------------------------------------------------------

    @contextmanager
    def exclusive(self):
        """Hold the client for a multi-call operation, so a read and a send never interleave."""
        with self._lock:
            yield self

    def _start(self) -> None:
        if self._retired:
            # Closed is final. Anything still holding this object is holding a corpse, and the one
            # thing it must not do is quietly acquire a fresh server nobody owns.
            raise BrowserUnavailable("this browser client has been closed")
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
        lines: queue.Queue = queue.Queue()
        stderr: deque = deque(maxlen=_STDERR_LINES)
        self._lines = lines
        self._stderr = stderr
        self._next_id = 0
        self._tab_opened = False
        self._failing_streak = 0
        self._detached = None
        self._started_ts = time.monotonic()
        # The queue and the buffer are handed to the reader rather than resolved through `self`.
        # A replaced process's reader runs its `finally` asynchronously, and reading `self._lines`
        # at put time would let that sentinel land in the *new* process's queue — where the very
        # next handshake would read it as "the server exited". Rare while restarts only followed a
        # dead process; the recycle below makes restarts routine.
        threading.Thread(target=self._read_stdout, args=(proc, lines), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc, stderr), daemon=True).start()
        try:
            self._handshake()
        except BrowserError:
            # A handshake that failed against a process that is still alive would otherwise be
            # permanent: `_proc.poll()` says None, so every later `_start()` returns early and the
            # server it returns early for never completed `initialize`. That is the same immortal
            # wedge this whole change exists to end, rebuilt by the code meant to cure it.
            self._kill(proc)
            self._proc = None
            raise

    def _kill(self, proc) -> None:
        """End a server process without asking it anything. Used where it cannot answer."""
        try:
            proc.terminate()
            proc.wait(timeout=_SHUTDOWN_JOIN_SEC)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def _read_stdout(self, proc, lines: queue.Queue) -> None:
        try:
            for line in proc.stdout:
                if line.strip():
                    lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)  # sentinel: the pipe closed, so no reply is ever coming

    def _read_stderr(self, proc, stderr: deque) -> None:
        try:
            for line in proc.stderr:
                stderr.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _handshake(self) -> None:
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "sellee", "version": "1"},
                },
                timeout=self._startup_timeout,
            )
            self._notify("notifications/initialized")
        except BrowserTransportError as exc:
            # A server that never completed startup is the browser layer being absent, not a
            # failed action — the caller's response is skip-and-notify, never the blind counter.
            raise BrowserUnavailable(f"the browser server did not start: {exc}") from exc

    def close(self, *, graceful: bool = True) -> None:
        """Retire this client and end its server.

        `graceful=False` skips the courtesy tab close, and is what a recycle passes. On a server
        that has lost Chrome that call cannot succeed and cannot fail quickly either: it waits the
        full tool timeout, and it waits holding `_lock`, so the lane that came to replace the client
        is blocked for 45 seconds on a tidying step whose whole point was to be polite.
        """
        with self._lock:
            proc = self._proc
            if graceful and proc is not None and proc.poll() is None and self._tab_opened:
                # Leave the warm Chrome as we found it: our tab is ours to clean up. A hard kill
                # skips this and leaves one tab behind, which is untidy but harmless. Called without
                # recovery, because nothing on the way out should be opening a tab — and before
                # the retirement below, which is what would refuse to reach the server at all.
                try:
                    self._call_once("browser_tabs", {"action": "close"})
                except BrowserError:
                    pass
            self._retired = True
            self._proc = None
            if proc is not None:
                self._kill(proc)

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

    def mark_detached(self, reason: str) -> None:
        """Record that this server has lost Chrome, as the daemon's factory has just established.

        Two things follow, and both matter. Every later call fails immediately instead of waiting
        out the server's own thirty-second timeout — a tick that opens twenty conversations was
        otherwise minutes of a lane doing nothing but time out — and it fails as `BrowserDetached`,
        so the lane can say whose fault it is instead of sending the seller to check a Chrome that
        is answering perfectly well.

        Cleared by `_start`, because a fresh process has a fresh connection.
        """
        self._detached = reason

    def is_detached(self) -> bool:
        return self._detached is not None

    def _call_once(self, name: str, arguments: dict) -> str:
        if self._detached is not None:
            raise BrowserDetached(self._detached)
        with self._lock:
            self._start()
            try:
                payload = self._rpc("tools/call", {"name": name, "arguments": arguments})
                text = result_text(payload)
                if payload.get("isError"):
                    detail = sections(text).get(_ERROR_SECTION) or text
                    raise BrowserToolError(f"{name} failed: {detail.strip()[:300]}")
            except BrowserToolError:
                # Only tool errors are counted. A transport error means the process died, which
                # `_start` already answers by respawning; a run of *tool* errors is the shape a
                # server that has lost Chrome makes, because it keeps answering us and keeps
                # failing everything it is asked to do.
                self._failing_streak += 1
                raise
            self._failing_streak = 0
            return text

    def failing_streak(self) -> int:
        """Tool failures since the last success.

        Read without the lock, on purpose. The factory asks this on every acquisition and must not
        block behind a send that is holding the client for its whole bracket; the cost of reading a
        value one call out of date is that a recycle happens on the next acquisition instead of this
        one, where the cost of taking the lock would be a lane stalling on an in-flight operation.
        """
        return self._failing_streak

    def age_sec(self, *, now=time.monotonic) -> float:
        """How long this client's server process has been running, or 0.0 if it has not started."""
        started = self._started_ts
        return 0.0 if started is None else max(0.0, now() - started)

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

    def set_follow(self, follow: bool) -> None:
        """Whether the seller is watching: with this on, every navigation brings our tab to the
        front of its window. Set at acquisition from the `watch_browser` setting, so a seller who
        flips it is followed (or left alone) from the next lane tick on."""
        with self._lock:
            self._follow = bool(follow)

    def navigate(self, url: str) -> None:
        with self._lock:
            self.ensure_tab()
            self.call_tool("browser_navigate", {"url": url})
            self._last_url = url
            if self._follow:
                self._follow_page(url)

    def navigate_visible(self, url: str) -> None:
        """Navigate, and put our tab in front before anything reads the page.

        A hidden tab is throttled (IntersectionObserver, requestAnimationFrame), and marketplaces
        build their lists that way — so a read on a background tab can be served a fraction of the
        page and no error at all. Bringing the tab forward is best-effort: it must not fail a read
        that might still succeed. This is a tab select inside the agent's own Chrome, not a window
        raise, so a read tick never raises the seller's window.
        """
        self.navigate(url)
        try:
            self.ensure_frontmost(url)
        except BrowserError:
            log.debug("could not bring our tab forward before reading %s", url, exc_info=True)

    def _follow_page(self, url: str) -> None:
        """Bring our tab forward so the seller can watch. Best-effort: it must never fail a
        navigation, because watch mode is a view onto the work and not part of it.

        The recovery is not optional. Bringing a tab forward selects one before it can check which
        tab it got, and a select repoints every later call — so a failure can leave the caller's
        read running against somebody else's page. Navigating again re-opens a tab of ours and puts
        it back on the page the caller asked for, which is what its next call has to be reading.
        """
        try:
            self.ensure_frontmost(url)
        except BrowserError:
            log.debug("could not bring our tab forward for watch mode", exc_info=True)
            self.ensure_tab()
            self.call_tool("browser_navigate", {"url": url})

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

        Typing is not the only caller — `navigate_visible` also brings the tab forward, for pages
        that only build themselves while visible. Nothing happens when the tab is already active —
        the steady state on the agent's own Chrome — so its window comes forward at most once
        rather than on every call.

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
