#!/usr/bin/env python3
"""GMC Relay: the counter's upload, into Home Assistant, on to gmcmap.com.

A GQ GMC Geiger counter with WiFi uploads by fetching, on port 80,

    GET /log2.asp?AID=<user id>&GID=<counter id>&CPM=<n>&ACPM=<n>&uSV=<n>

Point the counter's "Website" at this host and it sends the same request
here. The relay:

  1. reads CPM, ACPM and uSV (any letter case), refusing a request whose
     GID is not in `counter_ids`, and every request while that list is empty;
  2. keeps the reading in its history (SQLite under /data) for the panel's
     graphs;
  3. posts {"cpm", "acpm", "usv", "gid", "aid", "path", "received"} as JSON
     to Home Assistant's webhook `webhook_id`, straight to core on the
     add-on network, where a trigger-based template sensor turns it into
     state;
  4. when `forward` is on, replays the exact request to `forward_url` and
     answers the counter with gmcmap's own reply, so the public map keeps
     updating and the counter sees the reply it expects; otherwise answers
     "OK.ERR0." itself, gmcmap's success string.

GET /healthz answers "ok" for the watchdog; GET /status.json counts the
readings passed on and shows the last, without its AID and GID. Anything else is 404. A second
listener serves the panel and its data to Home Assistant's ingress gateway,
and to nothing else. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import math
import os
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("gmc-relay")
OK_REPLY = b"OK.ERR0."
UPLOAD_PATHS = ("/log2.asp", "/log2.php")
# The Supervisor's ingress gateway; the status listener answers no other address.
INGRESS_GATEWAY = "172.30.32.2"
PANEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
# Core's CA certificate, looked for in the add-on's config folder; absent, the system store is used.
CORE_CA_FILE = "core-ca.pem"
# What gmcmap.com needs to take an upload for a counter: not served on port 80.
COUNTER_KEYS = ("aid", "gid")
# Seconds a connection may sit between reads or writes before the relay closes it. The counter
# opens a second connection after each upload and never writes to it; without this each one held
# a descriptor and a thread until the process ran out of descriptors (Errno 24) after ~18 hours.
REQUEST_TIMEOUT = 10.0

RANGES = {
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "1y": 365 * 86400,
    "all": 0,
}
# Bucket widths for the trend line, smallest first; a range gets the first
# that keeps it at or under MAX_POINTS.
STEPS = (60, 300, 600, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)
MAX_POINTS = 400


def bucket_for(window: int) -> int:
    for step in STEPS:
        if window / step <= MAX_POINTS:
            return step
    return STEPS[-1]


# --------------------------------------------------------------------- options
def load_options(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc if isinstance(doc, dict) else {}


# --------------------------------------------------------------------- reading
def parse_upload(query: str) -> dict | None:
    """The counter's numbers from the query string, or None if it is not one.

    Keys are matched without regard to case: firmware writes uSV, forum
    posts show usv and USV.
    """
    raw = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
    if "cpm" not in raw:
        return None
    reading: dict = {"aid": raw.get("aid", ""), "gid": raw.get("gid", "")}
    for key in ("cpm", "acpm", "usv"):
        value = raw.get(key, "")
        try:
            number = float(value) if value != "" else None
        except ValueError:
            number = None
        reading[key] = number if number is None or math.isfinite(number) else None
    if reading["cpm"] is None:
        return None
    return reading


# --------------------------------------------------------------------- history
def parse_time(value: str) -> int | None:
    """Epoch seconds from an epoch number or an ISO 8601 time; naive means UTC."""
    value = value.strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _num(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


class History:
    """Every accepted reading, one row per counter and second."""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS readings ("
            "gid TEXT NOT NULL, ts INTEGER NOT NULL, cpm REAL NOT NULL, acpm REAL, usv REAL, "
            "PRIMARY KEY (gid, ts)) WITHOUT ROWID"
        )
        # gid -> [readings, newest ts]; the status poll reads this, not the table.
        self.counters: dict[str, list[int]] = {
            gid: [n, last]
            for gid, n, last in self.db.execute(
                "SELECT gid, count(*), max(ts) FROM readings GROUP BY gid"
            )
        }

    def add(self, gid: str, ts: int, cpm: float, acpm: float | None, usv: float | None) -> bool:
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO readings VALUES (?, ?, ?, ?, ?)", (gid, ts, cpm, acpm, usv)
            )
            if cur.rowcount != 1:
                return False
            entry = self.counters.setdefault(gid, [0, ts])
            entry[0] += 1
            entry[1] = max(entry[1], ts)
            return True

    def import_csv(self, path: str, default_gid: str = "") -> tuple[int, int, int]:
        """Rows of time,cpm[,acpm][,usv][,gid] with a header; (added, already there, bad)."""
        added = dup = bad = 0
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                ts = parse_time(row.get("time") or "")
                cpm = _num(row.get("cpm"))
                if ts is None or cpm is None:
                    bad += 1
                    continue
                gid = (row.get("gid") or default_gid).strip()
                if self.add(gid, ts, cpm, _num(row.get("acpm")), _num(row.get("usv"))):
                    added += 1
                else:
                    dup += 1
        return added, dup, bad

    def default_gid(self) -> str | None:
        if not self.counters:
            return None
        return max(self.counters.items(), key=lambda kv: kv[1][1])[0]

    def latest(self, gid: str, limit: int = 60) -> list[list]:
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, cpm, acpm, usv FROM readings WHERE gid = ? ORDER BY ts DESC LIMIT ?",
                (gid, limit),
            ).fetchall()
        return [list(r) for r in reversed(rows)]

    def window(self, gid: str, name: str, now: int) -> tuple[int, int]:
        end = now + 1
        seconds = RANGES[name]
        if seconds:
            return end - seconds, end
        with self.lock:
            (first,) = self.db.execute(
                "SELECT min(ts) FROM readings WHERE gid = ?", (gid,)
            ).fetchone()
        return (first if first is not None else end - 86400), end

    def series(self, gid: str, name: str, now: int | None = None) -> dict:
        """The trend, hourly sums, the count histogram and summary for one range."""
        now = int(time.time()) if now is None else now
        start, end = self.window(gid, name, now)
        step = bucket_for(max(end - start, 1))
        args = {"g": gid, "s": start, "e": end, "b": step}
        where = "WHERE gid = :g AND ts >= :s AND ts < :e"
        with self.lock:
            points = self.db.execute(
                "SELECT (ts / :b) * :b AS t, count(*), avg(cpm), min(cpm), max(cpm), "
                f"avg(usv), min(usv), max(usv) FROM readings {where} GROUP BY t ORDER BY t",
                args,
            ).fetchall()
            hourly = self.db.execute(
                "SELECT (ts / 3600) * 3600 AS t, count(*), sum(cpm), count(usv), sum(usv) "
                f"FROM readings {where} GROUP BY t ORDER BY t",
                args,
            ).fetchall()
            hist = self.db.execute(
                "SELECT CAST(round(cpm) AS INTEGER) AS k, count(*) "
                f"FROM readings {where} GROUP BY k ORDER BY k",
                args,
            ).fetchall()
            n, cpm_mean, usv_mean, first, last = self.db.execute(
                f"SELECT count(*), avg(cpm), avg(usv), min(ts), max(ts) FROM readings {where}",
                args,
            ).fetchone()
            peak = self.db.execute(
                f"SELECT ts, cpm, usv FROM readings {where} ORDER BY cpm DESC, ts DESC LIMIT 1",
                args,
            ).fetchone()
            prev = self.db.execute(
                "SELECT count(*), avg(cpm), avg(usv) FROM readings "
                "WHERE gid = :g AND ts >= :s - (:e - :s) AND ts < :s",
                args,
            ).fetchone()
        hours = (last - first) / 3600 if n else 0
        return {
            "range": name,
            "gid": gid,
            "start": start,
            "end": end,
            "step": step,
            "points": [list(p) for p in points],
            "hourly": [list(h) for h in hourly],
            "hist": [list(h) for h in hist],
            "stats": {
                "n": n,
                "cpm_mean": cpm_mean,
                "usv_mean": usv_mean,
                "first": first,
                "last": last,
                "dose_usv": usv_mean * hours if usv_mean is not None else None,
                "peak": list(peak) if peak else None,
                "prev": {"n": prev[0], "cpm_mean": prev[1], "usv_mean": prev[2]},
            },
        }

    def hourly(self, gid: str, days: int, now: int | None = None) -> list[list]:
        now = int(time.time()) if now is None else now
        with self.lock:
            rows = self.db.execute(
                "SELECT (ts / 3600) * 3600 AS t, count(*), sum(cpm), count(usv), sum(usv) "
                "FROM readings WHERE gid = ? AND ts >= ? GROUP BY t ORDER BY t",
                (gid, now - days * 86400),
            ).fetchall()
        return [list(r) for r in rows]


class Relay:
    def __init__(self, options: dict, history: History | None = None, config_dir: str = ""):
        self.webhook_id = str(options.get("webhook_id") or "")
        self.counter_ids = {str(c) for c in (options.get("counter_ids") or []) if str(c)}
        self.forward = bool(options.get("forward", True))
        self.forward_url = str(options.get("forward_url") or "https://www.gmcmap.com").rstrip("/")
        self.core_url = str(options.get("core_url") or "https://homeassistant:8123").rstrip("/")
        self.core_verify_tls = bool(options.get("core_verify_tls", True))
        self.core_ca_file = os.path.join(config_dir, CORE_CA_FILE) if config_dir else ""
        self.history = history
        self.warned_no_counters = False
        self.received = 0
        self.last: dict | None = None
        # (status, epoch) of the newest webhook post and gmcmap forward
        self.webhook_result: tuple[int, int] | None = None
        self.forward_result: tuple[int, int] | None = None

    # -- Home Assistant -------------------------------------------------------
    def core_context(self) -> ssl.SSLContext | None:
        """Core's certificate is checked against `core-ca.pem` in the config folder when it is
        there, else the system store; `core_verify_tls` off skips the check."""
        if not self.core_url.startswith("https"):
            return None
        if not self.core_verify_tls:
            return ssl._create_unverified_context()
        ca = self.core_ca_file if self.core_ca_file and os.path.isfile(self.core_ca_file) else None
        return ssl.create_default_context(cafile=ca)

    def core_tls(self) -> str:
        if not self.core_url.startswith("https"):
            return "plain http"
        if not self.core_verify_tls:
            return "not verified"
        if self.core_ca_file and os.path.isfile(self.core_ca_file):
            return f"verified against {self.core_ca_file}"
        return "verified against the system store"

    def post_webhook(self, payload: dict) -> tuple[int, str]:
        """Straight to core. A webhook needs no token, and the Supervisor's
        core proxy answers 200 to a webhook POST without delivering it, so the
        proxy is not used."""
        if not self.webhook_id:
            return 0, "webhook_id is not set"
        url = f"{self.core_url}/api/webhook/{self.webhook_id}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            ctx = self.core_context()  # an unreadable CA file is an ssl.SSLError, an OSError
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace")[:200]
        except (urllib.error.URLError, OSError) as err:
            return 0, str(err)

    # -- gmcmap ---------------------------------------------------------------
    def forward_upstream(self, path: str, query: str) -> tuple[int, bytes]:
        url = f"{self.forward_url}{path}?{query}" if query else f"{self.forward_url}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "gmc-relay"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read()[:4096]
        except urllib.error.HTTPError as err:
            return err.code, err.read()[:4096]
        except (urllib.error.URLError, OSError) as err:
            return 0, str(err).encode()

    # -- one upload -----------------------------------------------------------
    def record(self, reading: dict, ts: int) -> None:
        """A failed write is logged; the reading still goes to Home Assistant."""
        if self.history is None:
            return
        try:
            self.history.add(reading["gid"], ts, reading["cpm"], reading["acpm"], reading["usv"])
        except sqlite3.Error as err:
            LOG.error("history write failed: %s", err)

    def handle_upload(self, path: str, query: str) -> tuple[int, bytes]:
        """Returns the status and body to send back to the counter."""
        reading = parse_upload(query)
        if reading is None:
            LOG.warning("upload without a CPM value: %s?%s", path, query[:120])
            return 400, b"missing CPM"
        if not self.counter_ids:
            if not self.warned_no_counters:
                LOG.error(
                    "counter_ids is empty: uploads are refused until it lists the counter's GID"
                )
                self.warned_no_counters = True
            return 403, b"no counter configured"
        if reading["gid"] not in self.counter_ids:
            LOG.warning("upload from unknown counter GID %r refused", reading["gid"])
            return 403, b"unknown counter"
        now = datetime.now(UTC)
        self.record(reading, int(now.timestamp()))
        payload = {
            **reading,
            "path": path,
            "received": now.isoformat(timespec="seconds"),
        }
        status, detail = self.post_webhook(payload)
        self.webhook_result = (status, int(now.timestamp()))
        if 200 <= status < 300:
            self.received += 1
            self.last = payload
            LOG.info(
                "CPM %s ACPM %s uSv/h %s -> webhook %d",
                reading["cpm"],
                reading["acpm"],
                reading["usv"],
                status,
            )
        else:
            LOG.error("webhook refused the reading: %s %s", status, detail)
        if not self.forward:
            return 200, OK_REPLY
        up_status, body = self.forward_upstream(path, query)
        self.forward_result = (up_status, int(time.time()))
        if up_status == 200:
            LOG.debug("forwarded to %s: %s", self.forward_url, body[:60])
            return 200, body or OK_REPLY
        LOG.warning("forward to %s failed: %s %s", self.forward_url, up_status, body[:100])
        # The reading is in Home Assistant; the counter need not retry.
        return 200, OK_REPLY

    # -- the panel ------------------------------------------------------------
    def public_status(self) -> dict:
        """Port 80's /status.json, open to the LAN: the last reading without AID and GID."""
        last = {k: v for k, v in self.last.items() if k not in COUNTER_KEYS} if self.last else None
        return {"received": self.received, "last": last, "forward": self.forward}

    def status(self) -> dict:
        doc: dict = {
            "received": self.received,
            "last": self.last,
            "forward": self.forward,
            "forward_url": self.forward_url if self.forward else None,
            "webhook_set": bool(self.webhook_id),
            "webhook_result": self.webhook_result,
            "forward_result": self.forward_result,
            "now": int(time.time()),
            "counters": {},
            "gid": None,
            "recent": [],
        }
        if self.history is not None:
            gid = self.history.default_gid()
            doc["counters"] = {
                g: {"n": v[0], "last": v[1]} for g, v in self.history.counters.items()
            }
            doc["gid"] = gid
            if gid is not None:
                doc["recent"] = self.history.latest(gid)
        return doc


def make_handler(relay: Relay):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gmc-relay"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

        def log_message(self, fmt, *args):
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, status: int, body: bytes, ctype: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/healthz":
                self._send(200, b"ok\n")
            elif parts.path == "/status.json":
                self._send(200, json.dumps(relay.public_status()).encode(), "application/json")
            elif parts.path.lower() in UPLOAD_PATHS:
                status, body = relay.handle_upload(parts.path, parts.query)
                self._send(status, body)
            else:
                self._send(404, b"not found\n")

    return Handler


def load_panel() -> bytes:
    with open(PANEL_FILE, "rb") as fh:
        return fh.read()


def make_status_handler(relay: Relay, allowed: tuple[str, ...] = (INGRESS_GATEWAY,)):
    panel = load_panel()

    class StatusHandler(BaseHTTPRequestHandler):
        server_version = "gmc-relay"
        sys_version = ""
        timeout = REQUEST_TIMEOUT

        def log_message(self, fmt, *args):
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug("ingress %s " + fmt, self.address_string(), *args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, doc) -> None:
            self._send(200, json.dumps(doc, separators=(",", ":")).encode(), "application/json")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if self.client_address[0] not in allowed:
                self._send(403, b"forbidden\n", "text/plain")
                return
            parts = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            path = parts.path
            if path in ("/", ""):
                self._send(200, panel, "text/html; charset=utf-8")
            elif path == "/status.json":
                doc = {"received": relay.received, "last": relay.last, "forward": relay.forward}
                self._json(doc)
            elif path == "/api/status":
                self._json(relay.status())
            elif path in ("/api/history", "/api/hourly"):
                self._history(path, query)
            else:
                self._send(404, b"not found\n", "text/plain")

        def _history(self, path: str, query: dict) -> None:
            history = relay.history
            if history is None:
                self._send(404, b"no history\n", "text/plain")
                return
            gid = query.get("gid")
            if gid is None:
                gid = history.default_gid()
            if gid is None or gid not in history.counters:
                self._json({"gid": gid, "empty": True})
                return
            if path == "/api/hourly":
                try:
                    days = min(max(int(query.get("days", "371")), 1), 3660)
                except ValueError:
                    self._send(400, b"days\n", "text/plain")
                    return
                self._json({"gid": gid, "hourly": history.hourly(gid, days)})
                return
            name = query.get("range", "24h")
            if name not in RANGES:
                self._send(400, b"range\n", "text/plain")
                return
            self._json(history.series(gid, name))

    return StatusHandler


def serve(relay: Relay, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(relay))
    server.daemon_threads = True
    return server


def serve_status(
    relay: Relay, port: int, host: str = "0.0.0.0", allowed: tuple[str, ...] = (INGRESS_GATEWAY,)
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_status_handler(relay, allowed))
    server.daemon_threads = True
    return server


def import_pending(history: History, config_dir: str, default_gid: str) -> None:
    """A history file dropped into the add-on's config folder is read once, then renamed."""
    path = os.path.join(config_dir, "import.csv")
    if not os.path.isfile(path):
        return
    try:
        added, dup, bad = history.import_csv(path, default_gid)
    except (OSError, UnicodeDecodeError, csv.Error) as err:
        LOG.error("import of %s failed: %s", path, err)
        return
    os.replace(path, path + ".done")
    LOG.info("imported %s: %d added, %d already there, %d unreadable", path, added, dup, bad)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--options", required=True)
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--ingress-port", type=int, default=0, help="status page for ingress; 0 is off")
    ap.add_argument("--history", default="", help="SQLite file for the panel's graphs")
    ap.add_argument("--config-dir", default="", help="folder searched for import.csv at start")
    args = ap.parse_args(argv)
    options = load_options(args.options)
    logging.basicConfig(
        level=getattr(logging, str(options.get("log_level", "info")).upper(), logging.INFO),
        format="[gmc-relay] %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    history = History(args.history) if args.history else None
    relay = Relay(options, history, args.config_dir)
    if history is not None:
        if args.config_dir:
            ids = sorted(relay.counter_ids)
            import_pending(history, args.config_dir, ids[0] if len(ids) == 1 else "")
        total = sum(v[0] for v in history.counters.values())
        LOG.info(
            "history %s: %d readings from %d counter(s)", args.history, total, len(history.counters)
        )
    LOG.info(
        "webhook %s, core %s (TLS %s), counters %s, forward %s",
        "set" if relay.webhook_id else "NOT SET",
        relay.core_url,
        relay.core_tls(),
        sorted(relay.counter_ids) or "NONE, uploads refused",
        relay.forward_url if relay.forward else "off",
    )
    server = serve(relay, args.port)
    LOG.info("listening on :%d", args.port)
    if args.ingress_port:
        status = serve_status(relay, args.ingress_port)
        threading.Thread(target=status.serve_forever, daemon=True).start()
        LOG.info("panel for ingress on :%d", args.ingress_port)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
