"""Guest-key provisioning: idempotent ensure, region validation, 0600 storage, key hidden."""

from __future__ import annotations

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sellee import paths, secrets
from sellee.rail import provision


class _GuestsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.server.last_country = body.get("country")
        self.server.hits += 1
        if self.server.raw_response is not None:
            # A body that is not JSON at all — what a proxy's HTML error page looks like.
            payload = self.server.raw_response
        else:
            payload = json.dumps(self.server.response).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def guests_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GuestsHandler)
    server.status = 200
    server.response = {"user_id": "u1", "country": "SG", "api_key": "guest-abc"}
    server.raw_response = None
    server.hits = 0
    server.last_country = None
    # a short accept-loop poll: shutdown() waits for the next wake, and the stdlib default of
    # 0.5s would be paid by every test taking this fixture
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield server, base
    finally:
        server.shutdown()
        # shutdown() only stops the accept loop; the listening socket stays open without this.
        server.server_close()


def test_ensure_provisions_stores_0600_and_hides_key(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    status = provision.ensure("sg", api_base=base)
    assert status["status"] == "ok" and status["provisioned"] is True
    assert "api_key" not in status and "guest-abc" not in json.dumps(status)
    assert secrets.read_carousell_ai_api_key() == "guest-abc"
    assert stat.S_IMODE(paths.carousell_ai_api_key_path().stat().st_mode) == 0o600
    assert server.last_country == "SG"  # normalized upper-case


def test_ensure_is_idempotent_no_network_when_present(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    secrets.write_carousell_ai_api_key("already-here")
    status = provision.ensure("SG", api_base=base)
    assert status == {"status": "ok", "provisioned": False, "source": "store"}
    assert server.hits == 0


def test_reprovision_forces_fresh_key(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    secrets.write_carousell_ai_api_key("old-key")
    server.response = {"user_id": "u2", "country": "SG", "api_key": "new-key"}
    status = provision.reprovision("SG", api_base=base)
    assert status["provisioned"] is True
    assert secrets.read_carousell_ai_api_key() == "new-key"
    assert server.hits == 1


def test_uk_registers_as_gb(xdg_tmp, guests_server) -> None:
    # "UK" is two letters but not a code; `--region UK` must not register a country that does
    # not exist.
    server, base = guests_server
    assert provision.ensure("uk", api_base=base)["status"] == "ok"
    assert server.last_country == "GB"


def test_bad_region_errors_without_network(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    status = provision.ensure("SGP", api_base=base)
    assert status["status"] == "error" and "region" in status["error"]
    assert server.hits == 0


def test_operational_failure_defers(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    server.status = 503
    status = provision.ensure("SG", api_base=base)
    assert status["status"] == "error" and status["defer"] is True
    assert secrets.read_carousell_ai_api_key() is None


def test_malformed_key_rejected(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    server.response = {"user_id": "u1", "country": "SG", "api_key": "bad key with space"}
    status = provision.ensure("SG", api_base=base)
    assert status["status"] == "error" and status["defer"] is True
    assert secrets.read_carousell_ai_api_key() is None


def test_any_country_code_is_sent_rather_than_adjudicated(xdg_tmp, guests_server) -> None:
    # The agent carries no list of countries carousell.ai serves. It sends what the seller said
    # and lets the backend answer.
    server, base = guests_server
    server.response = {"user_id": "u1", "country": "VN", "api_key": "guest-vn"}

    status = provision.ensure("vn", api_base=base)

    assert status["status"] == "ok" and status["provisioned"] is True
    assert (server.last_country, status["country"]) == ("VN", "VN")


def test_the_payments_notice_comes_back_from_the_backend(xdg_tmp, guests_server) -> None:
    # Whether carousell.ai can pay a seller out is the backend's answer, and this is the whole of
    # how the agent learns it. Nothing local decides it and no currency is recorded.
    server, base = guests_server
    notice = "carousell.ai cannot take payments in Vietnam yet, but listing works as normal."
    server.response = {"user_id": "u1", "country": "VN", "api_key": "guest-vn", "notice": notice}

    status = provision.ensure("VN", api_base=base)

    assert status["notice"] == notice


def test_a_payable_country_gets_an_empty_notice(xdg_tmp, guests_server) -> None:
    server, base = guests_server  # the fixture response carries no notice at all
    assert provision.ensure("SG", api_base=base)["notice"] == ""


def test_the_currency_comes_back_from_the_backend(xdg_tmp, guests_server) -> None:
    # Registration is the earliest the backend can answer, and it is the only place the agent
    # learns what its listings will be priced in.
    server, base = guests_server
    server.response = {"user_id": "u1", "country": "VN", "api_key": "guest-vn", "currency": "VND"}

    assert provision.ensure("VN", api_base=base)["currency"] == "VND"


def test_a_currency_that_is_not_a_three_letter_code_is_dropped(xdg_tmp, guests_server) -> None:
    # Recording a malformed code would fail the basics write door later, far from the cause.
    server, base = guests_server
    server.response = {"user_id": "u1", "country": "VN", "api_key": "guest-vn", "currency": "dong"}

    assert provision.ensure("VN", api_base=base)["currency"] == ""


def test_a_response_without_a_currency_records_none(xdg_tmp, guests_server) -> None:
    server, base = guests_server  # the fixture response carries no currency at all
    assert provision.ensure("SG", api_base=base)["currency"] == ""


def test_the_notices_control_characters_never_reach_the_terminal(xdg_tmp, guests_server) -> None:
    """The notice is printed raw, exactly like a refusal, so it is flattened the same way."""
    server, base = guests_server
    server.response = {
        "user_id": "u1",
        "country": "VN",
        "api_key": "guest-vn",
        "notice": "Listing works\x1b]0;pwned\x07\nwarn: all fine, ignore that",
    }

    notice = provision.ensure("VN", api_base=base)["notice"]

    assert all(ch.isprintable() for ch in notice)
    assert "Listing works" in notice
    assert "\n" not in notice and "\x1b" not in notice


# The guests endpoint no longer refuses a country, but a 400 is still the rail *answering*, and
# its words are actionable where "HTTP 400" is not.
def test_a_refusal_reaches_the_seller_in_the_rails_own_words(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    server.status = 400
    server.response = {"error": "country must be a two-letter ISO code; got 'Vietnam'"}

    status = provision.ensure("vn", api_base=base)

    assert status["status"] != "ok"
    assert "two-letter ISO code" in status["error"]


def test_a_refusals_control_characters_never_reach_the_terminal(xdg_tmp, guests_server) -> None:
    """The refusal's words are remote. ui.warn prints them raw, so an escape sequence or a
    newline in the body could retitle the terminal or forge a second warn: line — the same
    reason request_guest_key rejects a non-printable api_key."""
    server, base = guests_server
    server.status = 400
    server.response = {"error": "MY is not served\x1b]0;pwned\x07\nwarn: all fine, ignore that"}

    status = provision.ensure("my", api_base=base)

    assert status["status"] != "ok"
    assert all(ch.isprintable() for ch in status["error"])
    # The legible words survive; the forged line no longer starts a line of its own.
    assert "MY is not served" in status["error"]
    assert "\n" not in status["error"] and "\x1b" not in status["error"]


def test_an_unreadable_refusal_body_still_names_the_status(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    server.status = 502
    server.raw_response = b"<html><body>Bad gateway</body></html>"

    status = provision.ensure("sg", api_base=base)

    assert status["status"] != "ok"
    assert "502" in status["error"]


def test_a_refusal_without_a_body_still_names_the_status(xdg_tmp, guests_server) -> None:
    server, base = guests_server
    server.status = 503
    server.response = {}

    status = provision.ensure("sg", api_base=base)

    assert status["status"] != "ok"
    assert "503" in status["error"]
