"""Bringing a listing's photographs across — the one part of the survey that opens a socket.

Every test here is a refusal. The URLs come off a marketplace page rather than from us, so what
matters is not that a good photo arrives but that everything else is turned away: another scheme,
another host, a redirect off the allowlisted one, a body bigger than the ceiling, and bytes that are
not an image at all.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from sellee.browser import photo_fetch

_JPEG = b"\xff\xd8\xff" + b"0" * 64
_HOST = "media.karousell.com"
_URL = f"https://{_HOST}/media/photos/products/a.jpg"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Opener:
    """Stands in for the built opener, recording what it was asked for."""

    def __init__(self, body=_JPEG, error=None):
        self.body = body
        self.error = error
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _Response(self.body)


@pytest.fixture
def opener(monkeypatch):
    built = _Opener()
    monkeypatch.setattr(photo_fetch.urllib.request, "build_opener", lambda *a: built)
    return built


# --- what is allowed at all ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://media.karousell.com/a.jpg",  # not https
        "https://evil.test/a.jpg",  # not a host the registry names
        "https://media.karousell.com.evil.test/a.jpg",  # a suffix match must not pass for one
        "https://sub.media.karousell.com/a.jpg",  # nor a subdomain of one
        "",
        "not a url",
    ],
)
def test_only_https_on_a_named_media_host_is_allowed(url) -> None:
    assert photo_fetch.allowed_url(url, [_HOST]) is False


def test_the_allowed_host_is_allowed() -> None:
    assert photo_fetch.allowed_url(_URL, [_HOST]) is True


def test_a_market_with_no_media_hosts_fetches_nothing(tmp_path, opener) -> None:
    """No recorded hosts is not "fetch freely" — it is a market whose photographs we cannot bring
    across, which the caller reports rather than working around."""
    stored = photo_fetch.fetch_listing_photos([_URL], market="fb", dest_dir=tmp_path)

    assert stored == []
    assert opener.requests == []


def test_a_disallowed_url_is_never_even_requested(tmp_path, opener) -> None:
    stored = photo_fetch.fetch_listing_photos(
        ["https://evil.test/a.jpg"], market="carousell", dest_dir=tmp_path
    )

    assert stored == []
    assert opener.requests == []


# --- what comes back ------------------------------------------------------------------------------


def test_a_good_photo_is_stored_and_named_by_its_type(tmp_path, opener) -> None:
    stored = photo_fetch.fetch_listing_photos(
        [_URL], market="carousell", dest_dir=tmp_path, referer="https://www.carousell.sg/p/x-1/"
    )

    assert len(stored) == 1
    assert stored[0].endswith("01.jpg")
    assert open(stored[0], "rb").read() == _JPEG
    assert opener.requests[0].get_header("Referer") == "https://www.carousell.sg/p/x-1/"


def test_a_body_that_is_not_an_image_is_dropped(tmp_path, monkeypatch) -> None:
    """The store's containment gate accepts a path, not a claim about what is in it — so an HTML
    error page served with an image URL must never become a listing photo."""
    monkeypatch.setattr(
        photo_fetch.urllib.request, "build_opener", lambda *a: _Opener(b"<html>nope</html>")
    )

    stored = photo_fetch.fetch_listing_photos([_URL], market="carousell", dest_dir=tmp_path)

    assert stored == []
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_body_is_refused_on_what_arrived(tmp_path, monkeypatch) -> None:
    """Decided by the bytes read, not by a Content-Length nobody verified."""
    huge = _JPEG + b"0" * (photo_fetch.MAX_PHOTO_BYTES + 10)
    monkeypatch.setattr(photo_fetch.urllib.request, "build_opener", lambda *a: _Opener(huge))

    stored = photo_fetch.fetch_listing_photos([_URL], market="carousell", dest_dir=tmp_path)

    assert stored == []


def test_a_failed_fetch_drops_only_that_photo(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        photo_fetch.urllib.request,
        "build_opener",
        lambda *a: _Opener(error=urllib.error.URLError("refused")),
    )

    stored = photo_fetch.fetch_listing_photos([_URL, _URL], market="carousell", dest_dir=tmp_path)

    assert stored == []


def test_redirects_are_refused_rather_than_followed() -> None:
    """Checked by construction: following one would undo the host check the fetch just made."""
    handler = photo_fetch._NoRedirects()

    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test/a.jpg") is None


def test_more_photos_than_an_item_can_hold_are_capped_by_count(tmp_path, opener) -> None:
    from sellee.store import MAX_PHOTOS

    stored = photo_fetch.fetch_listing_photos(
        [_URL] * (MAX_PHOTOS + 5), market="carousell", dest_dir=tmp_path
    )

    assert len(stored) == MAX_PHOTOS
    assert len(opener.requests) == MAX_PHOTOS, "photos past the cap must not even be requested"


def test_a_set_that_reaches_the_total_ceiling_stops_there(tmp_path, monkeypatch) -> None:
    """Each file is under its own cap, so only the running total can stop this — which is the half
    a per-file cap does not cover."""
    each = photo_fetch.MAX_PHOTO_BYTES - 1
    body = _JPEG + b"0" * (each - len(_JPEG))
    monkeypatch.setattr(photo_fetch.urllib.request, "build_opener", lambda *a: _Opener(body))
    fits = photo_fetch.MAX_TOTAL_BYTES // each

    stored = photo_fetch.fetch_listing_photos([_URL] * 12, market="carousell", dest_dir=tmp_path)

    assert 0 < len(stored) <= fits + 1
    assert sum(len(open(p, "rb").read()) for p in stored) <= photo_fetch.MAX_TOTAL_BYTES + each


def test_a_body_that_ends_early_is_dropped_rather_than_stored_half(tmp_path, monkeypatch) -> None:
    """A short read still sniffs as a jpeg on its first three bytes, so without this check half a
    photograph would be published as the listing's picture."""

    class _Short(_Response):
        length = 4096  # what Content-Length still owes after the read

    class _ShortOpener(_Opener):
        def open(self, request, timeout=None):
            self.requests.append(request)
            return _Short(self.body)

    monkeypatch.setattr(photo_fetch.urllib.request, "build_opener", lambda *a: _ShortOpener())

    stored = photo_fetch.fetch_listing_photos([_URL], market="carousell", dest_dir=tmp_path)

    assert stored == []


def test_a_malformed_response_does_not_escape_the_fetch(tmp_path, monkeypatch) -> None:
    """http.client.HTTPException is not an OSError. Uncaught it would abort the whole lane tick
    without counting the listing's attempt, so the same row would be re-served forever."""
    import http.client

    monkeypatch.setattr(
        photo_fetch.urllib.request,
        "build_opener",
        lambda *a: _Opener(error=http.client.IncompleteRead(b"partial")),
    )

    assert photo_fetch.fetch_listing_photos([_URL], market="carousell", dest_dir=tmp_path) == []
