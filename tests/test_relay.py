"""The relay between a fake counter, a fake Home Assistant core and a fake gmcmap."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
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
    # json.dumps writes these as Infinity and NaN, which no browser parses
    assert server.parse_upload("CPM=inf") is None and server.parse_upload("CPM=nan") is None
    assert server.parse_upload("CPM=5&ACPM=-inf&uSV=nan")["acpm"] is None


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
    relay = server.Relay({"webhook_id": "", "forward": False, "counter_ids": ["01234567890"]})
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


@pytest.mark.parametrize("listener", ["port 80", "ingress"])
@pytest.mark.parametrize("sent", [b"", b"GET /healthz HTTP/1.0\r\n"], ids=["nothing", "no end"])
def test_a_connection_that_stops_sending_is_closed(monkeypatch, listener, sent):
    """The counter opens a second connection after each upload and never writes to it."""
    monkeypatch.setattr(server, "REQUEST_TIMEOUT", 0.5, raising=False)
    relay = server.Relay({"webhook_id": "x", "forward": False})
    if listener == "port 80":
        srv = server.serve(relay, 0, host="127.0.0.1")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    else:
        srv, _ = status_server(relay, ("127.0.0.1",))
    try:
        with socket.create_connection(("127.0.0.1", srv.server_port), timeout=5) as conn:
            conn.sendall(sent)
            start = time.monotonic()
            assert conn.recv(1) == b""  # the relay closed its end
            assert time.monotonic() - start < 3
    finally:
        srv.shutdown()


def test_panel_is_static_and_fetches_relative_to_the_ingress_path():
    relay = server.Relay({"webhook_id": "x", "forward": False})
    srv, base = status_server(relay, ("127.0.0.1",))
    try:
        status, body = get(f"{base}/")
        page = body.decode()
        assert status == 200 and "<script>" in page
        # ingress serves the panel under /api/hassio_ingress/<token>/
        assert 'getJSON("api/status")' in page and 'getJSON("api/history?range=' in page
        assert 'fetch("/' not in page and 'getJSON("/' not in page
        # data reaches the DOM as text only
        assert "innerHTML" not in page
        assert json.loads(get(f"{base}/status.json")[1])["received"] == 0
        assert get(f"{base}/log2.asp?CPM=1")[0] == 404
    finally:
        srv.shutdown()


def test_status_listener_answers_only_the_ingress_gateway(tmp_path):
    relay = server.Relay({"webhook_id": "x"}, server.History(str(tmp_path / "h.db")))
    srv, base = status_server(relay, (server.INGRESS_GATEWAY,))
    try:
        for path in ("/", "/status.json", "/api/status", "/api/history", "/api/hourly"):
            assert get(f"{base}{path}")[0] == 403, path
    finally:
        srv.shutdown()


# --------------------------------------------------------------------- history
T0 = 1_790_000_000 // 86400 * 86400  # a UTC midnight


def history_with(tmp_path, rows, gid="g1"):
    h = server.History(str(tmp_path / "h.db"))
    for ts, cpm, usv in rows:
        assert h.add(gid, ts, cpm, None, usv)
    return h


def test_bucket_keeps_each_range_under_the_point_limit():
    assert server.bucket_for(86400) == 300
    assert server.bucket_for(7 * 86400) == 1800
    assert server.bucket_for(365 * 86400) == 86400
    for name, seconds in server.RANGES.items():
        if seconds:
            assert seconds / server.bucket_for(seconds) <= server.MAX_POINTS, name


def test_history_add_dedupes_and_counts(tmp_path):
    h = history_with(tmp_path, [(T0, 10, 0.06)])
    assert not h.add("g1", T0, 99, None, None)
    assert h.add("g2", T0 + 5, 12, None, None)
    assert h.counters == {"g1": [1, T0], "g2": [1, T0 + 5]}
    assert h.default_gid() == "g2"
    reopened = server.History(str(tmp_path / "h.db"))
    assert reopened.counters == h.counters


def test_series_buckets_mean_min_max_and_summary(tmp_path):
    now = T0 + 86400 - 10
    rows = [(T0 + 60, 10, 0.06), (T0 + 120, 20, 0.12), (T0 + 7200, 30, 0.18)]
    h = history_with(tmp_path, [*rows, (T0 - 3600, 5, 0.03)])
    s = h.series("g1", "24h", now)
    assert s["step"] == 300 and s["start"] == now + 1 - 86400
    pts = {p[0]: p for p in s["points"]}
    assert pts[T0] == pytest.approx([T0, 2, 15.0, 10, 20, 0.09, 0.06, 0.12])
    assert pts[T0 + 7200][1:5] == [1, 30.0, 30, 30]
    st = s["stats"]
    assert st["n"] == 3 and st["cpm_mean"] == 20.0 and st["peak"] == [T0 + 7200, 30.0, 0.18]
    assert st["first"] == T0 + 60 and st["last"] == T0 + 7200
    assert st["dose_usv"] == pytest.approx(0.12 * (7140 / 3600))
    # the reading an hour before midnight is in the 24 h before this window
    assert st["prev"] == {"n": 1, "cpm_mean": 5.0, "usv_mean": 0.03}
    assert s["hourly"][0] == [T0, 2, 30.0, 2, pytest.approx(0.18)]
    assert s["hist"] == [[10, 1], [20, 1], [30, 1]]


def test_series_all_starts_at_the_first_reading_and_filters_by_counter(tmp_path):
    h = history_with(tmp_path, [(T0, 10, None), (T0 + 400 * 86400, 12, None)])
    h.add("other", T0 - 900 * 86400, 99, None, None)
    s = h.series("g1", "all", T0 + 400 * 86400 + 5)
    assert s["start"] == T0 and s["stats"]["n"] == 2 and s["stats"]["peak"][1] == 12
    assert s["stats"]["usv_mean"] is None and s["stats"]["dose_usv"] is None
    assert len(s["points"]) <= server.MAX_POINTS


def test_histogram_rounds_counts(tmp_path):
    h = history_with(tmp_path, [(T0 + i, v, None) for i, v in enumerate((9.6, 10.4, 10.0, 11.5))])
    assert h.series("g1", "24h", T0 + 100)["hist"] == [[10, 3], [12, 1]]


def test_import_csv_reads_epoch_and_iso_and_counts_bad_rows(tmp_path):
    h = server.History(str(tmp_path / "h.db"))
    path = tmp_path / "import.csv"
    path.write_text(
        "time,cpm,acpm,usv,gid\n"
        f"{T0},10,15.5,0.06,\n"
        "2026-10-01T05:00:00+00:00,11,,0.07,g9\n"
        "2026-10-01T05:01:00,12,,,\n"
        "yesterday,10,,,\n"
        f"{T0 + 1},,,,\n"
        f"{T0 + 2},nan,,,\n"
        f"{T0},10,15.5,0.06,\n",
        encoding="utf-8",
    )
    assert h.import_csv(str(path), "g1") == (3, 1, 3)
    iso = int(datetime(2026, 10, 1, 5, tzinfo=UTC).timestamp())
    assert h.latest("g9") == [[iso, 11.0, None, 0.07]]
    assert h.latest("g1") == [[T0, 10.0, 15.5, 0.06], [iso + 60, 12.0, None, None]]


def test_import_pending_renames_the_file_once_read(tmp_path):
    h = server.History(str(tmp_path / "h.db"))
    (tmp_path / "import.csv").write_text(f"time,cpm\n{T0},10\n", encoding="utf-8")
    server.import_pending(h, str(tmp_path), "g1")
    assert not (tmp_path / "import.csv").exists() and (tmp_path / "import.csv.done").exists()
    assert h.counters == {"g1": [1, T0]}
    server.import_pending(h, str(tmp_path), "g1")  # nothing left to read
    assert h.counters == {"g1": [1, T0]}


def test_upload_is_kept_even_when_the_webhook_refuses(tmp_path):
    h = server.History(str(tmp_path / "h.db"))
    relay = server.Relay({"webhook_id": "", "forward": False, "counter_ids": ["7"]}, h)
    assert relay.handle_upload("/log2.asp", "GID=7&CPM=21&ACPM=15&uSV=0.13")[0] == 200
    assert relay.received == 0 and relay.webhook_result[0] == 0
    assert h.counters["7"][0] == 1
    assert h.latest("7")[0][1:] == [21.0, 15.0, 0.13]
    relay.handle_upload("/log2.asp", "GID=8&CPM=21")
    relay.handle_upload("/log2.asp", "GID=7&CPM=inf")
    relay.handle_upload("/log2.asp", "GID=7")
    assert h.counters == {"7": [1, h.counters["7"][1]]}


def test_a_broken_history_does_not_stop_the_relay(tmp_path):
    h = server.History(str(tmp_path / "h.db"))
    h.db.close()
    relay = server.Relay({"webhook_id": "", "forward": False, "counter_ids": ["7"]}, h)
    assert relay.handle_upload("/log2.asp", "GID=7&CPM=21") == (200, b"OK.ERR0.")


def test_panel_api_serves_status_history_and_refuses_bad_input(tmp_path):
    now = int(time.time())
    h = history_with(tmp_path, [(now - 120, 10, 0.06), (now - 60, 14, 0.08)])
    relay = server.Relay({"webhook_id": "x", "forward": False}, h)
    srv, base = status_server(relay, ("127.0.0.1",))
    try:
        st = json.loads(get(f"{base}/api/status")[1])
        assert st["gid"] == "g1" and st["recent"][-1] == [now - 60, 14.0, None, 0.08]
        assert st["counters"] == {"g1": {"n": 2, "last": now - 60}} and st["webhook_set"]
        doc = json.loads(get(f"{base}/api/history?range=24h")[1])
        assert doc["gid"] == "g1" and doc["stats"]["n"] == 2
        assert json.loads(get(f"{base}/api/history?range=7d&gid=g1")[1])["range"] == "7d"
        assert json.loads(get(f"{base}/api/history?gid=nobody")[1]) == {
            "gid": "nobody",
            "empty": True,
        }
        assert len(json.loads(get(f"{base}/api/hourly?days=2")[1])["hourly"]) >= 1
        assert get(f"{base}/api/history?range=5y")[0] == 400
        assert get(f"{base}/api/hourly?days=x")[0] == 400
    finally:
        srv.shutdown()


# --------------------------------------------------------------------- counters, status, TLS
def test_an_empty_counter_list_refuses_every_upload_and_says_so_once(tmp_path, caplog):
    FakeGmcmap.gets.clear()
    core = spin(FakeCore)
    h = server.History(str(tmp_path / "h.db"))
    relay = server.Relay(
        {
            "webhook_id": WEBHOOK,
            "core_url": f"http://127.0.0.1:{core.server_port}",
            "forward": True,
            "forward_url": "http://127.0.0.1:9",
        },
        h,
    )
    n = len(FakeCore.posts)
    try:
        for _ in range(3):
            assert relay.handle_upload("/log2.asp", UPLOAD) == (403, b"no counter configured")
    finally:
        core.shutdown()
    assert len(FakeCore.posts) == n and FakeGmcmap.gets == [] and h.counters == {}
    assert relay.received == 0 and relay.forward_result is None
    assert sum("counter_ids is empty" in r.message for r in caplog.records) == 1


def test_port_80_status_leaves_out_the_counter_keys_and_ingress_keeps_them(stack):
    base, relay = stack
    assert get(f"{base}/log2.asp?{UPLOAD}")[0] == 200
    doc = json.loads(get(f"{base}/status.json")[1])
    assert doc["last"]["cpm"] == 21 and "aid" not in doc["last"] and "gid" not in doc["last"]
    assert b"01234567890" not in get(f"{base}/status.json")[1]
    srv, ingress = status_server(relay, ("127.0.0.1",))
    try:
        assert json.loads(get(f"{ingress}/status.json")[1])["last"]["gid"] == "01234567890"
    finally:
        srv.shutdown()


@pytest.fixture(scope="module")
def tls_core(tmp_path_factory):
    """Core on https with a certificate from a throwaway CA, which names 127.0.0.1."""
    d = tmp_path_factory.mktemp("tls")

    def ossl(*args):
        subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=d)

    ca_ext = (
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    ossl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "2",
        "-subj",
        "/CN=test ca",
        *ca_ext,
    )
    ossl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "core.key",
        "-out",
        "core.csr",
        "-subj",
        "/CN=core",
    )
    (d / "san.cnf").write_text("subjectAltName=IP:127.0.0.1\n")
    ossl(
        "x509",
        "-req",
        "-in",
        "core.csr",
        "-CA",
        "ca.pem",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        "core.pem",
        "-days",
        "2",
        "-extfile",
        "san.cnf",
    )
    srv = HTTPServer(("127.0.0.1", 0), FakeCore)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "core.pem", d / "core.key")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{srv.server_port}", d
    srv.shutdown()


def tls_relay(url, config_dir="", **options):
    return server.Relay(
        {
            "webhook_id": WEBHOOK,
            "core_url": url,
            "counter_ids": ["01234567890"],
            "forward": False,
            **options,
        },
        None,
        config_dir,
    )


def test_core_certificate_is_verified_by_default(tls_core, tmp_path):
    url, _ = tls_core
    n = len(FakeCore.posts)
    relay = tls_relay(url, str(tmp_path))  # no core-ca.pem: the system store does not know the CA
    assert relay.handle_upload("/log2.asp", UPLOAD) == (200, b"OK.ERR0.")
    assert relay.webhook_result[0] == 0 and len(FakeCore.posts) == n
    assert tls_relay(url).core_context().verify_mode == ssl.CERT_REQUIRED


def test_core_certificate_is_checked_against_the_ca_in_the_config_folder(tls_core, tmp_path):
    url, d = tls_core
    (tmp_path / server.CORE_CA_FILE).write_bytes((d / "ca.pem").read_bytes())
    n = len(FakeCore.posts)
    relay = tls_relay(url, str(tmp_path))
    relay.handle_upload("/log2.asp", UPLOAD)
    assert relay.webhook_result[0] == 200 and len(FakeCore.posts) == n + 1
    other = tls_relay(
        url.replace("127.0.0.1", "localhost"), str(tmp_path)
    )  # the certificate does not name it
    other.handle_upload("/log2.asp", UPLOAD)
    assert other.webhook_result[0] == 0


def test_an_unreadable_core_ca_refuses_the_webhook_and_still_answers_the_counter(tmp_path):
    (tmp_path / server.CORE_CA_FILE).write_text("not a certificate\n")
    relay = tls_relay("https://127.0.0.1:9", str(tmp_path))
    assert relay.handle_upload("/log2.asp", UPLOAD) == (200, b"OK.ERR0.")
    assert relay.webhook_result[0] == 0


def test_core_verify_tls_off_skips_the_check(tls_core):
    url, _ = tls_core
    relay = tls_relay(url, core_verify_tls=False)
    relay.handle_upload("/log2.asp", UPLOAD)
    assert relay.webhook_result[0] == 200
