"""Bringing a listing's photographs across, so a listing the seller already had can be relisted.

The one part of the survey that opens a socket, which is why it is its own module — the network
allowlist grants socket access per file, and a grant should be as narrow as its job.

Everything here is a bound, because the URLs come off a page rather than from us:

  * https, and a host the registry names (`marketplaces.media_hosts`) — anything else is refused.
  * No redirects — refused rather than checked afterwards, so a 302 cannot walk the fetch off an
    allowlisted host.
  * Byte caps applied to the body, not its Content-Length: each file is read one byte past its cap,
    and the set has its own ceiling.
  * Sniffed, not trusted: a body that is not an image is dropped.

A photo that fails any of these is dropped and the rest are kept: a listing with three of its four
pictures is still worth having; one with a stranger's file in it is not.
"""

from __future__ import annotations

import http.client
import logging
import urllib.error
import urllib.parse
import urllib.request

from sellee import images, marketplaces
from sellee.store import MAX_PHOTOS

log = logging.getLogger(__name__)

# Long enough for a large photograph on a slow connection, short enough that a hung CDN cannot hold
# the lane's tick open.
FETCH_TIMEOUT_SEC = 20.0
# A marketplace photograph is a phone picture; the rail downsizes past 4MB anyway.
MAX_PHOTO_BYTES = 12 * 1024 * 1024
# The ceiling for one listing's whole set.
MAX_TOTAL_BYTES = 60 * 1024 * 1024
# A CDN may require one; ours names the listing the photo belongs to.
_USER_AGENT = "Mozilla/5.0 (compatible; sellee/1.0)"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: returning None makes urllib raise the response as an HTTPError instead
    of following it, so a 302 cannot undo the host check."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def allowed_url(url: str, hosts, suffixes=()) -> bool:
    """Whether `url` is an https URL on one of `hosts`, or under one of `suffixes`.

    `hosts` matches exactly. `suffixes` is dot-anchored — the host must equal the suffix or sit
    directly under it, never a substring match — the same boundary
    `engines.hosts.host_is_marketplace` uses. Suffixes exist because some marketplaces serve media
    from per-request hostnames (Facebook's `scontent-*.fbcdn.net`) that cannot be enumerated ahead
    of time.
    """
    if not url or (not hosts and not suffixes):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {h.lower() for h in hosts}:
        return True
    return any(
        host == s or host.endswith("." + s) for s in (t.lower().strip(". ") for t in suffixes) if s
    )


def fetch_listing_photos(urls, *, market: str, dest_dir, referer: str = "") -> list:
    """Download a listing's photographs into `dest_dir`, and answer with the stored paths.

    Fewer paths than URLs whenever something was refused or failed; empty when nothing could be
    brought across — which the caller reports rather than treating as an item with no pictures.
    """
    hosts = marketplaces.media_hosts(market)
    suffixes = marketplaces.media_host_suffixes(market)
    if not hosts and not suffixes:
        log.warning("no media hosts recorded for %s — not fetching any photos", market)
        return []

    opener = urllib.request.build_opener(_NoRedirects)
    stored: list = []
    budget = MAX_TOTAL_BYTES
    for url in list(urls)[:MAX_PHOTOS]:
        if not allowed_url(url, hosts, suffixes):
            log.warning("refusing a listing photo that is not https on a %s media host", market)
            continue
        data = _fetch_one(opener, url, referer=referer, cap=min(MAX_PHOTO_BYTES, budget))
        if data is None:
            continue
        sniffed = images.sniff_bytes(data[: images.MAGIC_READ_BYTES])
        if sniffed is None:
            log.warning("a listing photo from %s was not an image — dropped", market)
            continue
        _kind, suffix, _content_type = sniffed
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{len(stored) + 1:02d}{suffix}"
        try:
            dest.write_bytes(data)
        except OSError as exc:
            log.warning("could not store a listing photo: %s", exc)
            continue
        budget -= len(data)
        stored.append(str(dest))
        if budget <= 0:
            log.warning(
                "listing photo set hit the total byte ceiling — stopping at %d", len(stored)
            )
            break
    return stored


def _fetch_one(opener, url: str, *, referer: str, cap: int) -> bytes | None:
    """One photo's bytes, or None when it could not be had within the bounds."""
    if cap <= 0:
        return None
    headers = {"User-Agent": _USER_AGENT, "Accept": "image/*"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 — https-and-host checked
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SEC) as response:
            # One byte past the cap: the bound is what arrived, not a Content-Length nobody
            # verified.
            data = response.read(cap + 1)
            # What the response still owes: 0 on a complete body, None when it declared no length.
            # Read here, before the response is closed.
            owed = getattr(response, "length", None)
    except (
        urllib.error.URLError,
        # Not an OSError: a truncated chunked body or bad status line raises here, and would abort
        # the lane tick uncaught.
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        log.warning("listing photo fetch failed: %s", type(exc).__name__)
        return None
    if len(data) > cap:
        log.warning("listing photo is over the size ceiling — dropped")
        return None
    if owed:
        # The connection ended mid-body. The first bytes still sniff as an image, so this would be
        # stored as half a picture.
        log.warning("listing photo ended %d bytes short — dropped", owed)
        return None
    return data or None
