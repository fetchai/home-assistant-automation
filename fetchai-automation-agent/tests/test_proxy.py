"""proxy.py — the ingress proxy serves only the Supervisor, and carries the
watchdog's /health to the agent (2.24.2: before it, any container on the hassio
network reached every agent endpoint through 172.30.33.1:8098 with no login)."""
import http.server
import importlib.util
import pathlib
import re
import threading
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("proxy", ROOT / "proxy.py")
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)


def _serve(handler):
    server = proxy.ThreadedHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _request(port, path, method="GET", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def running(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<!DOCTYPE html>panel")
    monkeypatch.setattr(proxy, "PANEL_DIR", str(tmp_path))
    server = _serve(proxy.Handler)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


class TestWhoMayAsk:
    def test_the_supervisor_and_loopback_only(self):
        assert proxy.client_allowed("172.30.32.2")
        assert proxy.client_allowed("127.0.0.1")

    @pytest.mark.parametrize("address", ["172.30.33.2", "172.30.32.1", "192.168.64.9",
                                         "172.30.32.20", "", None])
    def test_anyone_else_is_refused(self, address):
        """Another add-on, the host's bridge address, a LAN client, a
        near-miss of the Supervisor's address, nothing at all."""
        assert not proxy.client_allowed(address)


class TestTheServer:
    def test_an_allowed_peer_gets_the_panel(self, running):
        status, body = _request(running, "/")

        assert status == 200
        assert b"panel" in body

    def test_a_refused_peer_gets_403_for_a_page(self, running, monkeypatch):
        monkeypatch.setattr(proxy, "ALLOWED_CLIENTS", {"172.30.32.2"})

        assert _request(running, "/")[0] == 403

    def test_and_for_every_agent_endpoint_before_anything_is_forwarded(self, running,
                                                                        monkeypatch):
        agent = _serve(_Agent)
        _Agent.hits = []
        monkeypatch.setattr(proxy, "AGENT_PORT", agent.server_address[1])
        monkeypatch.setattr(proxy, "ALLOWED_CLIENTS", {"172.30.32.2"})
        try:
            for path in ("/rest/pairing/code", "/rest/approve", "/rest/post"):
                assert _request(running, path, "POST", b"{}")[0] == 403
            assert _request(running, "/health")[0] == 403
        finally:
            agent.shutdown()
            agent.server_close()

        assert _Agent.hits == []


class _Agent(http.server.BaseHTTPRequestHandler):
    """A stand-in for the agent on 8099 that records every request it gets."""
    hits = []

    def do_POST(self):
        self.hits.append(("POST", self.path))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self.hits.append(("GET", self.path))
        body = b'{"status": "ok", "heartbeat_age_s": 3.0}'
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestHealth:
    def test_the_agents_answer_is_passed_through(self, running, monkeypatch):
        agent = _serve(_Agent)
        monkeypatch.setattr(proxy, "AGENT_PORT", agent.server_address[1])
        try:
            status, body = _request(running, "/health")
        finally:
            agent.shutdown()
            agent.server_close()

        assert status == 200
        assert b'"status": "ok"' in body

    def test_through_the_ingress_prefix_as_well(self, running, monkeypatch):
        agent = _serve(_Agent)
        monkeypatch.setattr(proxy, "AGENT_PORT", agent.server_address[1])
        try:
            status, _ = _request(running, "/api/hassio_ingress/abc/health")
        finally:
            agent.shutdown()
            agent.server_close()

        assert status == 200

    def test_an_agent_that_does_not_answer_is_a_503(self, running, monkeypatch):
        """The watchdog's miss: <300 is alive to the Supervisor."""
        monkeypatch.setattr(proxy, "AGENT_PORT", 1)

        assert _request(running, "/health")[0] == 503

    def test_the_timeout_is_inside_the_supervisors_ten_seconds(self):
        assert proxy.HEALTH_TIMEOUT_S < 10


class TestTheManifest:
    """config.yaml and proxy.py must agree on the one port the Supervisor uses."""

    def _field(self, name):
        text = (ROOT / "config.yaml").read_text()
        return re.search(rf"^{name}:\s*\"?([^\"\n]+)\"?\s*$", text, re.M).group(1)

    def test_ingress_is_the_proxy(self):
        assert int(self._field("ingress_port")) == proxy.PROXY_PORT

    def test_the_watchdog_asks_the_proxy_not_the_agent(self):
        assert self._field("watchdog") == f"http://[HOST]:[PORT:{proxy.PROXY_PORT}]/health"

    def test_the_manifest_publishes_no_port(self):
        """Nothing reaches the host's network: the only way in stays ingress."""
        assert not re.search(r"^ports:", (ROOT / "config.yaml").read_text(), re.M)
