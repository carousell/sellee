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
