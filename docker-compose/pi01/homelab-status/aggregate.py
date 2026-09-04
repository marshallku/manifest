#!/usr/bin/env python3
"""Collapse the homelab's monitoring state into one small JSON document.

The consumer is an ESP32-S3 with a 400x300 monochrome display, so this is
shaped for a device that has no clock, a fixed-size parser and no appetite for
paging through 58 monitors. Everything is pre-aggregated here: group rollups,
only the monitors that are actually down, and a handful of host gauges.

Two sources, deliberately unequal:

  - Uptime Kuma's SQLite database, read *locally* on pi01. This is the half
    that has to keep working when prd01 and the whole k3s cluster are gone,
    which is the only moment the display earns its place on the shelf.
  - Prometheus, which lives in-cluster on prd01. It dies with prd01, so its
    absence is reported (`hosts.ok = false`) rather than allowed to fail the
    whole document.

The database is opened read-only over its live WAL — no copy. A copy would be
~34 MB per refresh, and pi01 keeps its state on an SD card whose write volume
is budgeted (see ../README.md); at a 30 s refresh that is ~100 GB/day of pure
wear for data already sitting in the page cache.

Nothing here writes to disk, and no dependency outside the standard library is
used, so the container is a bare `python:*-alpine` with this file mounted.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KUMA_DB = os.environ.get("KUMA_DB", "/kuma/kuma.db")
PROM_URL = os.environ.get("PROM_URL", "http://192.168.219.100:30090")
REFRESH_SEC = int(os.environ.get("REFRESH_SEC", "30"))
PROM_TIMEOUT = float(os.environ.get("PROM_TIMEOUT", "4"))
LISTEN = os.environ.get("LISTEN", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
STALE_AFTER_SEC = int(os.environ.get("STALE_AFTER_SEC", "180"))

# Shared secret the display sends as `Authorization: Bearer <token>`.
#
# This document is a subset of what Uptime Kuma keeps behind a login: monitor
# names spell out internal hosts, ports and service inventory. Serving that
# unauthenticated would make the summary weaker than the thing it summarises,
# so the endpoint requires the token whenever one is configured.
#
# Empty is refused at startup unless STATUS_ALLOW_ANONYMOUS=1 says so out loud;
# see check_auth_config(). Failing closed matters because .env.example ships
# this blank, and a forgotten value would otherwise serve the document to the
# whole LAN with nothing but a log line to say so.
AUTH_TOKEN = os.environ.get("STATUS_TOKEN", "")
ALLOW_ANONYMOUS = os.environ.get("STATUS_ALLOW_ANONYMOUS", "") == "1"
MIN_TOKEN_LEN = 16

# Screen budget. The display fits roughly this many rows of each kind; trimming
# here rather than on the device keeps the firmware's fixed-size buffers honest.
MAX_GROUPS = 10
MAX_DOWN = 6
MAX_HOSTS = 6
MAX_NAME_LEN = 30

# The firmware draws with embedded-graphics' built-in ASCII fonts, so Kuma's
# Korean group names cannot be rendered as-is. Map them to short ASCII labels;
# anything unmapped falls back to an ASCII-stripped, uppercased version of the
# original and is still displayed rather than dropped.
GROUP_LABELS = {
    "호스트": "HOSTS",
    "인프라 코어": "CORE",
    "관측 스택": "OBSERV",
    "GitOps · 시크릿": "GITOPS",
    "공개 서비스": "PUBLIC",
    "내부 오리진 (prd01)": "ORIGIN",
    "k8s 앱 (NodePort)": "K8S APPS",
    "데이터스토어": "DATA",
}
UNGROUPED_LABEL = "OTHER"

# Uptime Kuma status codes.
ST_DOWN, ST_UP, ST_PENDING, ST_MAINTENANCE = 0, 1, 2, 3


def ascii_label(text: str, limit: int = MAX_NAME_LEN) -> str:
    """Reduce an arbitrary monitor/group name to something the firmware can draw."""
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] if cleaned else "?"


def parse_kuma_time(value: str) -> float | None:
    """Kuma stores naive UTC timestamps as 'YYYY-MM-DD HH:MM:SS.mmm'.

    The container runs with TZ=Asia/Seoul, which affects only how the web UI
    renders these — the stored values are UTC, so they are read as UTC here.
    """
    if not value:
        return None
    try:
        stamp = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc).timestamp()


def read_kuma() -> dict:
    """Group rollups plus the monitors currently not up, straight from SQLite."""
    uri = f"file:{urllib.parse.quote(KUMA_DB)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")

        monitors = {
            row[0]: {"name": row[1], "type": row[2], "parent": row[3]}
            for row in conn.execute(
                "SELECT id, name, type, parent FROM monitor WHERE active = 1"
            )
        }
        group_names = {
            mid: m["name"] for mid, m in monitors.items() if m["type"] == "group"
        }

        # Latest beat per monitor. The max(id) subquery rides the monitor_id
        # index and stays cheap even with a 26 MB history table.
        latest = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT monitor_id, status FROM heartbeat "
                "WHERE id IN (SELECT max(id) FROM heartbeat GROUP BY monitor_id)"
            )
        }

        # Last state *change* per monitor, which is where a down-since comes
        # from. Covered by monitor_important_time_index.
        changed = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT monitor_id, status, time FROM heartbeat "
                "WHERE important = 1 AND id IN ("
                "  SELECT max(id) FROM heartbeat WHERE important = 1 GROUP BY monitor_id"
                ")"
            )
        }

        # Groups keep the order they were created in, matching the Kuma UI.
        # Sorting them by health instead would reshuffle the panel every time
        # something broke, which is exactly when a familiar layout is worth
        # most.
        group_order = {mid: i for i, mid in enumerate(sorted(group_names))}

        groups: dict[str, dict] = {}
        down: list[dict] = []
        now = time.time()
        total = up = unsettled = 0

        for mid, mon in sorted(monitors.items()):
            if mon["type"] == "group":
                continue
            status = latest.get(mid)
            label = GROUP_LABELS.get(
                group_names.get(mon["parent"], ""),
                ascii_label(group_names.get(mon["parent"], UNGROUPED_LABEL), 10).upper(),
            )
            bucket = groups.setdefault(
                label,
                {
                    "label": label,
                    "up": 0,
                    "total": 0,
                    "_order": group_order.get(mon["parent"], len(group_order)),
                },
            )

            # PENDING is a monitor mid-retry and MAINTENANCE is deliberate.
            # Neither is evidence of an outage, and leaving them in the
            # denominator is what would make the headline flap during every
            # retry window — so they leave the ratio entirely and are counted
            # separately instead. Reporting that count is the point: silently
            # shrinking the denominator would be its own kind of lie.
            if status not in (ST_UP, ST_DOWN):
                unsettled += 1
                continue

            bucket["total"] += 1
            total += 1

            if status == ST_UP:
                bucket["up"] += 1
                up += 1
            else:
                secs = None
                change = changed.get(mid)
                if change and change[0] == ST_DOWN:
                    since = parse_kuma_time(change[1])
                    if since is not None:
                        secs = max(0, int(now - since))
                down.append({"name": ascii_label(mon["name"]), "secs": secs})

        ordered = sorted(groups.values(), key=lambda g: g["_order"])
        for group in ordered:
            del group["_order"]
        # Longest-running outages first: they are the ones that have stopped
        # generating notifications and so are the ones actually worth a pixel.
        down.sort(key=lambda d: (d["secs"] is None, -(d["secs"] or 0)))

        # A group whose every monitor is unsettled has no ratio to draw.
        ordered = [g for g in ordered if g["total"] > 0]

        return {
            "ok": True,
            "up": up,
            "total": total,
            "unsettled": unsettled,
            "groups": ordered[:MAX_GROUPS],
            "down": down[:MAX_DOWN],
            "down_more": max(0, len(down) - MAX_DOWN),
        }
    finally:
        conn.close()


def prom_query(query: str) -> dict[str, float]:
    """Run one instant query and flatten it to {node: value}."""
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=PROM_TIMEOUT) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus returned {payload.get('status')}")
    out: dict[str, float] = {}
    for result in payload["data"]["result"]:
        node = result["metric"].get("node")
        if not node:
            continue
        try:
            out[node] = float(result["value"][1])
        except (TypeError, ValueError):
            continue
    return out


QUERIES = {
    "cpu": '100 - (avg by(node)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "mem": "(1 - avg by(node)(node_memory_MemAvailable_bytes)"
    " / avg by(node)(node_memory_MemTotal_bytes)) * 100",
    "disk": '(1 - avg by(node)(node_filesystem_avail_bytes{mountpoint="/"})'
    ' / avg by(node)(node_filesystem_size_bytes{mountpoint="/"})) * 100',
    "load": "avg by(node)(node_load1)",
    "up_d": "avg by(node)((time() - node_boot_time_seconds) / 86400)",
}


def read_prometheus() -> dict:
    """Per-node gauges. Absent nodes are omitted rather than reported as zero."""
    series = {name: prom_query(query) for name, query in QUERIES.items()}
    nodes = sorted(series["cpu"])
    out = []
    for node in nodes[:MAX_HOSTS]:
        entry = {"name": ascii_label(node, 10)}
        for name in QUERIES:
            value = series[name].get(node)
            entry[name] = None if value is None else round(value, 1)
        out.append(entry)
    return {"ok": True, "nodes": out}


def build() -> dict:
    """Assemble the document, degrading each source independently."""
    try:
        kuma = read_kuma()
    except Exception as exc:  # noqa: BLE001 - a failure here must still serve
        kuma = {
            "ok": False,
            "error": ascii_label(f"{type(exc).__name__}: {exc}", 60),
            "up": 0,
            "total": 0,
            "unsettled": 0,
            "groups": [],
            "down": [],
            "down_more": 0,
        }

    try:
        hosts = read_prometheus()
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as exc:
        hosts = {
            "ok": False,
            "error": ascii_label(f"{type(exc).__name__}: {exc}", 60),
            "nodes": [],
        }

    return {"v": 1, "generated": int(time.time()), "kuma": kuma, "hosts": hosts}


# Served until the first refresh lands. The device parses into fixed-size
# structs, so every key it reads has to be present from the very first
# response — a short document would fail its parser rather than degrade.
EMPTY_DOCUMENT = {
    "v": 1,
    "kuma": {
        "ok": False,
        "up": 0,
        "total": 0,
        "unsettled": 0,
        "groups": [],
        "down": [],
        "down_more": 0,
    },
    "hosts": {"ok": False, "nodes": []},
}


class Cache:
    """Last successfully built document, refreshed on a timer.

    The display has no clock, so it cannot work out how old the data is from a
    timestamp alone. `age` is therefore stamped at *request* time, which also
    makes a wedged refresh thread visible on the device instead of silent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict | None = None

    def set(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload

    def render(self) -> bytes:
        with self._lock:
            payload = self._payload
        if payload is None:
            # Shape-stable even before the first refresh: the device parses
            # into fixed-size structs and a missing `kuma`/`hosts` object is a
            # hard parse failure there, not a soft one.
            body = dict(
                EMPTY_DOCUMENT, generated=int(time.time()), age=0, stale=True, ready=False
            )
        else:
            age = max(0, int(time.time()) - payload["generated"])
            body = dict(payload, age=age, stale=age > STALE_AFTER_SEC, ready=True)
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


CACHE = Cache()


def refresh_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            CACHE.set(build())
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            print(f"[aggregate] refresh failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1.0, REFRESH_SEC - (time.monotonic() - started)))


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A client that hangs up is routine, not an incident.

        `socketserver` prints a full traceback for any exception in a request
        thread, including the ordinary case of a peer resetting the connection.
        Real errors still get one.
        """
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError)):
            return
        print(
            f"[aggregate] request from {client_address[0]} failed: "
            f"{type(error).__name__}: {error}",
            flush=True,
        )


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so the client gets a 1.1 response, but every connection is closed
    # after one exchange. The only client is the display, which makes a single
    # request per connection and then resets it; leaving keep-alive on meant the
    # server sat in `readline` until that reset and logged a full traceback for
    # every poll — a stack trace every 30 s, forever.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/", "/homelab.json"):
            self.send_error(404)
            return
        if not self.authorised():
            # No WWW-Authenticate header: the only client is firmware with a
            # baked-in token, and there is no interactive challenge to make.
            self.send_error(401, "unauthorized")
            return
        body = CACHE.render()
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            # Client vanished mid-write. Nothing to do and nothing to say.
            pass

    def authorised(self) -> bool:
        """Constant-time bearer check. Always true when no token is configured."""
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(presented.strip(), AUTH_TOKEN)

    def log_message(self, *args) -> None:
        """Quiet by default — a device polling every 30 s would fill the log."""


def check_auth_config() -> None:
    """Refuse to serve unauthenticated unless that was asked for explicitly."""
    if AUTH_TOKEN:
        if len(AUTH_TOKEN) < MIN_TOKEN_LEN:
            print(
                f"[aggregate] STATUS_TOKEN is shorter than {MIN_TOKEN_LEN} characters; "
                "generate one with `openssl rand -hex 24`",
                flush=True,
            )
            raise SystemExit(1)
        return

    if ALLOW_ANONYMOUS:
        print(
            "[aggregate] STATUS_ALLOW_ANONYMOUS=1 — serving without authentication. "
            "The document names internal hosts and ports; do not leave this on.",
            flush=True,
        )
        return

    print(
        "[aggregate] STATUS_TOKEN is not set. Refusing to start rather than serve "
        "internal host and service names to the LAN unauthenticated. Set it in .env "
        "(see .env.example), or set STATUS_ALLOW_ANONYMOUS=1 to accept that.",
        flush=True,
    )
    raise SystemExit(1)


def main() -> None:
    check_auth_config()
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = Server((LISTEN, PORT), Handler)
    guard = "token required" if AUTH_TOKEN else "UNAUTHENTICATED"
    print(
        f"[aggregate] serving http://{LISTEN}:{PORT}/homelab.json ({guard})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
