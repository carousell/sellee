"""The one host-boundary matcher, shared by scam detection and listing-URL verification.

The legacy repo duplicated this logic three times (each of scam_scan, verify_listing_url, and
scam_bank had its own _strip_www and host parsing); the single source here kills that drift. It
serves both directions: allowlisting a listing URL (a real marketplace host) and flagging an
off-platform link in a scam scan (anything that is NOT one).

The match is boundary-exact: a host matches a marketplace only when it equals the domain or is a
subdomain of it, so facebook.com.scam.site and carousell-pay.net both fail. The checkout carve-out
recognises the single inbound link the agent may open — a genuine carousell.ai checkout link —
keyed on the config-derived base (api.carousell.ai/checkout), rejecting every lookalike
(userinfo tricks, subdomains, punycode, wrong scheme/port/path).

Network-free by construction: URLs are parsed with pure string ops, never urllib — an engine must
never be able to open a socket.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Cheap/scam-favoured TLDs plus the common ones: a bare token (no scheme, no path) is treated as a
# link only when its TLD is plausible, so ordinary prose with a dot isn't mistaken for a URL.
PLAUSIBLE_TLDS = frozenset(
    {
        "com",
        "net",
        "org",
        "io",
        "ai",
        "co",
        "info",
        "biz",
        "me",
        "app",
        "dev",
        "link",
        "live",
        "site",
        "shop",
        "store",
        "online",
        "click",
        "vip",
        "top",
        "xyz",
        "cc",
        "buzz",
        "icu",
        "cn",
        "ru",
        "sg",
        "my",
        "ph",
        "hk",
        "tw",
        "id",
        "au",
        "uk",
        "us",
        "ca",
        "de",
        "jp",
    }
)

_SCHEME_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'\)\]]+", re.IGNORECASE)
_BARE_URL_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?:/[^\s<>\"'\)\]]*)?",
    re.IGNORECASE,
)
_TRAILING_PUNCT = ".,!?;:)]}>\"'"


@dataclass(frozen=True)
class UrlParts:
    scheme: str
    userinfo: str | None
    host: str
    port: str | None
    path: str


def parse_url(raw: str) -> UrlParts:
    """Split a URL into parts with pure string ops (no urllib — engines open no sockets). A bare
    host with no scheme parses with scheme='' and the whole authority as the host."""
    text = (raw or "").strip()
    scheme = ""
    rest = text
    if "://" in text:
        scheme, _, rest = text.partition("://")
        scheme = scheme.lower()
    cut = len(rest)
    for ch in "/?#":
        i = rest.find(ch)
        if i != -1:
            cut = min(cut, i)
    authority = rest[:cut]
    remainder = rest[cut:]
    path = remainder
    for ch in "?#":
        i = path.find(ch)
        if i != -1:
            path = path[:i]
    userinfo = None
    hostport = authority
    if "@" in authority:
        userinfo, _, hostport = authority.rpartition("@")
    host, port = hostport, None
    if ":" in hostport:
        host, _, port = hostport.rpartition(":")
    return UrlParts(
        scheme=scheme, userinfo=userinfo or None, host=host.lower(), port=port, path=path
    )


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def host_of(raw: str) -> str:
    """The lowercased host of a raw link or bare token, or '' if none."""
    return parse_url(raw).host


def host_is_marketplace(host: str, allowlist) -> bool:
    """Strict boundary match: the host must equal or be a subdomain of a known marketplace domain.
    Never a substring — facebook.com.scam.site and carousell-pay.net both fail."""
    h = strip_www((host or "").lower())
    if not h:
        return False
    return any(h == d or h.endswith("." + d) for d in allowlist)


def build_allowlist(marketplaces: list) -> frozenset:
    """The set of exact registrable marketplace hosts, from every domains{} value plus any FULL
    listing_url host. A bare-suffix listing host like 'carousell.' is deliberately ignored — the
    domains map already enumerates the real regional hosts, and a suffix would wave through a
    lookalike like carousell.ai.scam.site."""
    exact: set = set()
    for entry in marketplaces:
        for host in (entry.get("domains") or {}).values():
            h = strip_www(str(host).lower().strip())
            if "." in h:
                exact.add(h)
        listing_host = strip_www(
            str((entry.get("listing_url") or {}).get("host") or "").lower().strip()
        )
        if "." in listing_host and not listing_host.endswith(".") and listing_host != "localhost":
            exact.add(listing_host)
    return frozenset(exact)


def extract_links(text: str) -> list:
    """Raw link strings found in `text`, de-duplicated, trailing punctuation trimmed. Scheme'd and
    www. links are always taken; a bare token needs a plausible TLD or a path to count as a link."""
    found: list = []
    seen: set = set()
    for match in _SCHEME_URL_RE.finditer(text or ""):
        raw = match.group(0).rstrip(_TRAILING_PUNCT)
        if raw and raw.lower() not in seen:
            seen.add(raw.lower())
            found.append(raw)
    for match in _BARE_URL_RE.finditer(text or ""):
        raw = match.group(0).rstrip(_TRAILING_PUNCT)
        low = raw.lower()
        if not raw or low in seen:
            continue
        if any(low == s.lower() or s.lower().endswith(low) for s in found):
            continue  # already captured by a scheme'd/www match
        host = host_of(raw)
        tld = host.rsplit(".", 1)[-1] if "." in host else ""
        if "/" in raw or tld in PLAUSIBLE_TLDS:
            seen.add(low)
            found.append(raw)
    return found


def defang(raw: str) -> str:
    """Neutralise a link so it can be shown to a human without becoming clickable."""
    return raw.replace("https://", "hxxps://").replace("http://", "hxxp://").replace(".", "[.]")


def checkout_base_parts(checkout_base: str) -> UrlParts:
    """Parse the config-derived checkout base (e.g. https://api.carousell.ai/checkout)."""
    return parse_url(checkout_base)


def is_checkout_link(raw: str, checkout_base: str) -> bool:
    """True only for a genuine carousell.ai checkout link under the config-derived base — the sole
    inbound link the agent may open. Every lookalike (userinfo, subdomain, punycode, wrong
    scheme/port/path) fails, so a scammer's imitation is never navigable."""
    if "://" not in raw:  # a checkout link is always a full URL
        return False
    p = parse_url(raw)
    base = checkout_base_parts(checkout_base)
    if p.scheme != (base.scheme or "https"):
        return False
    if p.userinfo:  # reject api.carousell.ai@evil.com tricks
        return False
    if p.host != base.host:  # EXACT host, not a subdomain or lookalike
        return False
    if p.host.startswith("xn--") or any(ord(c) > 127 for c in p.host):  # no punycode/homoglyphs
        return False
    if p.port not in (None, "443"):
        return False
    prefix = base.path.rstrip("/") + "/"  # require a sale id after /checkout/
    return p.path.startswith(prefix) and len(p.path) > len(prefix)


def verify_listing_pattern(
    url: str, host_pattern: str, path_pattern: str, region_host: str | None = None
) -> tuple:
    """Deterministic host+path (+optional region) check for a listing URL. Returns (ok, reason).
    A fabricated or wrong-host/-region/-path link fails closed. The carousell-ai live check is the
    tool's job (it needs the network); this is the pure gate for every browser marketplace."""
    text = (url or "").strip()
    if not text:
        return False, "empty url — nothing was read from the live page"
    p = parse_url(text)
    if p.scheme not in ("http", "https"):
        return False, f"url scheme must be http(s), got '{p.scheme or 'none'}'"
    if not p.host:
        return False, "url has no host"
    if region_host and "." in region_host:
        want = strip_www(region_host.lower())
        got = strip_www(p.host)
        if got != want and not got.endswith("." + want):
            return False, f"host '{p.host}' is not the expected regional site ('{region_host}')"
    else:
        want_host = host_pattern.lower()
        if want_host not in p.host:
            return False, f"host '{p.host}' does not match expected '{want_host}'"
    if path_pattern not in p.path:
        return False, f"path '{p.path}' does not contain expected '{path_pattern}'"
    return True, "valid"
