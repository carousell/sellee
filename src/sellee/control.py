"""The client half of the daemon's control routes — the one place a CLI talks to the daemon.

Every verb that changes state goes through the running daemon rather than opening sellee.db:
one writer per store holds at the process level too. That makes "call a control route" something
five CLI modules do, and this is the single implementation of it — which is also what keeps the
socket capability to one module in the stdlib guard's allowlist, instead of one grant per verb.

Requests carry the attended bearer from the config-dir secret and a localhost Origin, because the
server rejects anything else (its DNS-rebinding defense). Reads put the token in the query
string, which is what the routes a browser also opens accept.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from sellee import secrets

_LOCALHOST_ORIGIN = "http://127.0.0.1"
DEFAULT_TIMEOUT_SEC = 30.0

NO_DAEMON_MESSAGE = "sellee: no MCP token found — start the daemon first (sellee daemon run)"


class DaemonUnreachable(Exception):
    """The daemon did not answer at all — not running, or not on the port config names.

    Its own type rather than urllib's, so a CLI catching "the daemon is down" needs no network
    import of its own: the socket capability stays in this module, where the guard can see it.
    """


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def tail_url(port: int, ticket: str, since: str | None = None) -> str:
    """The web tail's address, carrying a one-shot ticket — never the attended token, which
    would otherwise sit in the address bar and the browser's history for good. The ticket rides
    in the fragment (the browser never sends a fragment over the wire), is minutes-lived, and
    dies the moment the page trades it in.

    Composed here rather than by the caller because query encoding is this module's business —
    it is the one place allowed to reach for urllib at all.
    """
    query = f"?{urllib.parse.urlencode({'since': since})}" if since else ""
    return f"{base_url(port)}/tail{query}#ticket={ticket}"


def require_token():
    """The attended bearer, or None having already explained what to do about its absence.

    The token is minted at first daemon start, so its absence means precisely one thing: the
    daemon has never run here.
    """
    token = secrets.read_mcp_token()
    if not token:
        print(NO_DAEMON_MESSAGE, file=sys.stderr)
    return token


def post(port: int, token: str, route: str, body: dict, *, timeout: float = DEFAULT_TIMEOUT_SEC):
    """POST to a control route, answering (status, parsed body).

    An error body is read and returned rather than raised: a 400 from these routes carries the
    reason a value was refused, which is the whole point of validating at the door.
    """
    request = urllib.request.Request(
        f"{base_url(port)}{route}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": _LOCALHOST_ORIGIN,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return exc.code, {}
    except (urllib.error.URLError, OSError) as exc:
        raise DaemonUnreachable(str(exc)) from exc


def get(port: int, token: str, route: str, params=None, *, timeout: float = DEFAULT_TIMEOUT_SEC):
    """GET a control route, answering (status, parsed body) — the same contract as `post`.

    DaemonUnreachable is reserved for nothing answering at all. An HTTP error *is* the daemon
    answering — a stale token's 401, a busy browser's 503 — and reporting one as "the daemon
    isn't running" sends whoever reads the message off to debug the wrong thing entirely.
    """
    query = dict(params or {})
    query["token"] = token
    url = f"{base_url(port)}{route}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Origin": _LOCALHOST_ORIGIN})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return exc.code, {}
    except (urllib.error.URLError, OSError) as exc:
        raise DaemonUnreachable(str(exc)) from exc
