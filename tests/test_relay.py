"""The relay between a fake counter, a fake Home Assistant core and a fake gmcmap."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gmc_relay")
spec = importlib.util.spec_from_file_location("server", os.path.join(ROOT, "server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

WEBHOOK = "abc123"
UPLOAD = "AID=01234&GID=01234567890&CPM=21&ACPM=15.02&uSV=0.13"


class FakeCore(BaseHTTPRequestHandler):
    posts: ClassVar[list[dict]] = []
    refuse = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        ok = self.path == f"/api/webhook/{WEBHOOK}" and not FakeCore.refuse
        if ok:
            FakeCore.posts.append(json.loads(body))
        self.send_response(200 if ok else 401)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeGmcmap(BaseHTTPRequestHandler):
    gets: ClassVar[list[str]] = []
    down = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeGmcmap.gets.append(self.path)
        body = b"Error! Geiger Counter not found.ERR2." if FakeGmcmap.down else b"OK.ERR0."
        self.send_response(500 if FakeGmcmap.down else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def spin(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def stack():
    core = spin(FakeCore)
    up = spin(FakeGmcmap)
    relay = server.Relay(
        {
            "webhook_id": WEBHOOK,
            "core_url": f"http://127.0.0.1:{core.server_port}",
            "counter_ids": ["01234567890"],
            "forward": True,
            "forward_url": f"http://127.0.0.1:{up.server_port}",
        }
    )
    srv = server.serve(relay, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", relay
    for s in (srv, core, up):
        s.shutdown()


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_parse_upload_is_case_insensitive_and_numeric():
    r = server.parse_upload("aid=1&gid=2&cpm=21&acpm=15.02&USV=0.13")
    assert r == {"aid": "1", "gid": "2", "cpm": 21.0, "acpm": 15.02, "usv": 0.13}
    assert server.parse_upload("AID=1&GID=2") is None
    assert server.parse_upload("CPM=abc") is None
    assert server.parse_upload("CPM=5")["acpm"] is None


def test_upload_reaches_home_assistant_and_gmcmap(stack):
    base, relay = stack
    FakeCore.posts.clear()
    FakeGmcmap.gets.clear()
    status, body = get(f"{base}/log2.asp?{UPLOAD}")
    assert status == 200 and body == b"OK.ERR0."
    assert len(FakeCore.posts) == 1
    p = FakeCore.posts[0]
    assert p["cpm"] == 21 and p["acpm"] == 15.02 and p["usv"] == 0.13
    assert p["gid"] == "01234567890" and p["path"] == "/log2.asp" and p["received"]
    assert FakeGmcmap.gets == [f"/log2.asp?{UPLOAD}"]
    assert relay.received == 1 and relay.last["cpm"] == 21


def test_php_path_and_status_json(stack):
    base, relay = stack
    status, _ = get(f"{base}/log2.php?{UPLOAD}")
    assert status == 200
    doc = json.loads(get(f"{base}/status.json")[1])
    assert doc["received"] == relay.received and doc["last"]["path"] == "/log2.php"


def test_unknown_counter_is_refused_and_not_forwarded(stack):
    base, _ = stack
    FakeGmcmap.gets.clear()
    n = len(FakeCore.posts)
    status, _ = get(f"{base}/log2.asp?AID=1&GID=999&CPM=21")
    assert status == 403 and len(FakeCore.posts) == n and FakeGmcmap.gets == []


def test_upload_without_cpm_is_a_400(stack):
    base, _ = stack
    status, _ = get(f"{base}/log2.asp?AID=1&GID=01234567890")
    assert status == 400


def test_gmcmap_down_still_answers_the_counter_ok(stack):
    base, _ = stack
    FakeGmcmap.down = True
    try:
        status, body = get(f"{base}/log2.asp?{UPLOAD}")
        assert status == 200 and body == b"OK.ERR0."
    finally:
        FakeGmcmap.down = False


def test_webhook_refusal_is_logged_not_fatal(stack, caplog):
    base, relay = stack
    FakeCore.refuse = True
    n = relay.received
    try:
        status, _ = get(f"{base}/log2.asp?{UPLOAD}")
        assert status == 200 and relay.received == n
        assert any("webhook refused" in r.message for r in caplog.records)
    finally:
        FakeCore.refuse = False


def test_forward_off_answers_ok_itself():
    relay = server.Relay({"webhook_id": "", "forward": False})
    status, body = relay.handle_upload("/log2.asp", UPLOAD)
    assert status == 200 and body == b"OK.ERR0."


def test_healthz_and_404(stack):
    base, _ = stack
    assert get(f"{base}/healthz")[1] == b"ok\n"
    assert get(f"{base}/anything")[0] == 404


def status_server(relay, allowed):
    srv = server.serve_status(relay, 0, host="127.0.0.1", allowed=allowed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_status_page_shows_the_last_reading_escaped():
    relay = server.Relay({"webhook_id": "x", "forward": False})
    relay.received = 3
    relay.last = {
        "cpm": 21.0,
        "acpm": 15.02,
        "usv": 0.13,
        "gid": "<b>7</b>",
        "received": "2026-10-09T05:00:00+00:00",
    }
    srv, base = status_server(relay, ("127.0.0.1",))
    try:
        status, body = get(f"{base}/")
        page = body.decode()
        assert status == 200 and "<td>3</td>" in page and "<td>21.0</td>" in page
        assert "&lt;b&gt;7&lt;/b&gt;" in page and "<b>7</b>" not in page
        assert '<meta http-equiv="refresh" content="30">' in page
        assert json.loads(get(f"{base}/status.json")[1])["received"] == 3
        assert get(f"{base}/log2.asp?CPM=1")[0] == 404
    finally:
        srv.shutdown()


def test_status_listener_answers_only_the_ingress_gateway():
    relay = server.Relay({"webhook_id": "x"})
    srv, base = status_server(relay, (server.INGRESS_GATEWAY,))
    try:
        assert get(f"{base}/")[0] == 403
        assert get(f"{base}/status.json")[0] == 403
    finally:
        srv.shutdown()
