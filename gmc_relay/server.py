#!/usr/bin/env python3
"""GMC Relay: the counter's upload, into Home Assistant, on to gmcmap.com.

A GQ GMC Geiger counter with WiFi uploads by fetching, on port 80,

    GET /log2.asp?AID=<user id>&GID=<counter id>&CPM=<n>&ACPM=<n>&uSV=<n>

Point the counter's "Website" at this host and it sends the same request
here. The relay:

  1. reads CPM, ACPM and uSV (any letter case), refusing a request whose
     GID is not in `counter_ids` when that list is set;
  2. posts {"cpm", "acpm", "usv", "gid", "aid", "path", "received"} as JSON
     to Home Assistant's webhook `webhook_id`, straight to core on the
     add-on network, where a trigger-based template sensor turns it into
     state;
  3. when `forward` is on, replays the exact request to `forward_url` and
     answers the counter with gmcmap's own reply, so the public map keeps
     updating and the counter sees the reply it expects; otherwise answers
     "OK.ERR0." itself, gmcmap's success string.

GET /healthz answers "ok" for the watchdog; GET /status.json counts the
readings passed on and shows the last. Anything else is 404. A second
listener serves the same status as a page to Home Assistant's ingress
gateway, and to nothing else. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import logging
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("gmc-relay")
OK_REPLY = b"OK.ERR0."
UPLOAD_PATHS = ("/log2.asp", "/log2.php")
# The Supervisor's ingress gateway; the status listener answers no other address.
INGRESS_GATEWAY = "172.30.32.2"


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
            reading[key] = float(value) if value != "" else None
        except ValueError:
            reading[key] = None
    if reading["cpm"] is None:
        return None
    return reading


class Relay:
    def __init__(self, options: dict):
        self.webhook_id = str(options.get("webhook_id") or "")
        self.counter_ids = {str(c) for c in (options.get("counter_ids") or []) if str(c)}
        self.forward = bool(options.get("forward", True))
        self.forward_url = str(options.get("forward_url") or "https://www.gmcmap.com").rstrip("/")
        self.core_url = str(options.get("core_url") or "https://homeassistant:8123").rstrip("/")
        self.received = 0
        self.last: dict | None = None

    # -- Home Assistant -------------------------------------------------------
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
        # Core's certificate is the house's own; the name is not checkable.
        ctx = ssl._create_unverified_context() if url.startswith("https") else None
        try:
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
    def handle_upload(self, path: str, query: str) -> tuple[int, bytes]:
        """Returns the status and body to send back to the counter."""
        reading = parse_upload(query)
        if reading is None:
            LOG.warning("upload without a CPM value: %s?%s", path, query[:120])
            return 400, b"missing CPM"
        if self.counter_ids and reading["gid"] not in self.counter_ids:
            LOG.warning("upload from unknown counter GID %r refused", reading["gid"])
            return 403, b"unknown counter"
        payload = {
            **reading,
            "path": path,
            "received": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        status, detail = self.post_webhook(payload)
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
        if up_status == 200:
            LOG.debug("forwarded to %s: %s", self.forward_url, body[:60])
            return 200, body or OK_REPLY
        LOG.warning("forward to %s failed: %s %s", self.forward_url, up_status, body[:100])
        # The reading is in Home Assistant; the counter need not retry.
        return 200, OK_REPLY


def make_handler(relay: Relay):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gmc-relay"
        sys_version = ""

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
                doc = {"received": relay.received, "last": relay.last, "forward": relay.forward}
                self._send(200, json.dumps(doc).encode(), "application/json")
            elif parts.path.lower() in UPLOAD_PATHS:
                status, body = relay.handle_upload(parts.path, parts.query)
                self._send(status, body)
            else:
                self._send(404, b"not found\n")

    return Handler


def status_page(relay: Relay) -> bytes:
    """The relay's state for the sidebar panel: dark, reloading every 30 s, no script."""
    last = relay.last or {}
    rows = [
        ("Readings passed on", str(relay.received)),
        ("Last reading", last.get("received") or "none yet"),
        ("CPM", last.get("cpm")),
        ("ACPM", last.get("acpm")),
        ("µSv/h", last.get("usv")),
        ("Counter", last.get("gid")),
        ("Forward", relay.forward_url if relay.forward else "off"),
        ("Webhook", "set" if relay.webhook_id else "NOT SET"),
    ]
    cells = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape('' if v is None else str(v))}</td></tr>"
        for k, v in rows
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="30"><title>GMC Relay</title><style>'
        "body{background:#111;color:#ddd;font:15px system-ui,sans-serif;margin:16px}"
        "th{text-align:left;padding:4px 16px 4px 0;color:#999;font-weight:normal}"
        "td{padding:4px 0}</style></head><body><h1>GMC Relay</h1>"
        f"<table>{cells}</table></body></html>"
    ).encode()


def make_status_handler(relay: Relay, allowed: tuple[str, ...] = (INGRESS_GATEWAY,)):
    class StatusHandler(BaseHTTPRequestHandler):
        server_version = "gmc-relay"
        sys_version = ""

        def log_message(self, fmt, *args):
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug("ingress %s " + fmt, self.address_string(), *args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if self.client_address[0] not in allowed:
                self._send(403, b"forbidden\n", "text/plain")
                return
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", ""):
                self._send(200, status_page(relay), "text/html; charset=utf-8")
            elif path == "/status.json":
                doc = {"received": relay.received, "last": relay.last, "forward": relay.forward}
                self._send(200, json.dumps(doc).encode(), "application/json")
            else:
                self._send(404, b"not found\n", "text/plain")

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--options", required=True)
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--ingress-port", type=int, default=0, help="status page for ingress; 0 is off")
    args = ap.parse_args(argv)
    options = load_options(args.options)
    logging.basicConfig(
        level=getattr(logging, str(options.get("log_level", "info")).upper(), logging.INFO),
        format="[gmc-relay] %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    relay = Relay(options)
    LOG.info(
        "webhook %s, core %s, counters %s, forward %s",
        "set" if relay.webhook_id else "NOT SET",
        relay.core_url,
        sorted(relay.counter_ids) or "any",
        relay.forward_url if relay.forward else "off",
    )
    server = serve(relay, args.port)
    LOG.info("listening on :%d", args.port)
    if args.ingress_port:
        status = serve_status(relay, args.ingress_port)
        threading.Thread(target=status.serve_forever, daemon=True).start()
        LOG.info("status page for ingress on :%d", args.ingress_port)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
