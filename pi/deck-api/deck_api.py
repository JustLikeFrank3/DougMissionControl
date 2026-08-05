#!/usr/bin/env python3
"""deck-api — one entry point for every Flight Deck boot trigger.

Runs ON THE PI, installed to /opt/flightdeck by pi/setup-deck.sh and served
by the deck-api systemd unit. It is a *wrapper*: it never reimplements the
boot logic, it shells out to the same /usr/local/bin/flightsim-boot.sh that
fauxmo has always called, and it learns what that script is doing by
following its journald output. flightsim-boot.sh is unmodified.

    GET  /api/state              current state as JSON
    GET  /api/events             the same, streamed (SSE) on every change
    POST /api/boot               {"target":"windows|linux","intent":"..."}
    POST /api/launch             target already up — fire greeting + profile
    POST /api/abort              kill an in-flight run
    POST /api/hook/greeting      Windows reports the greeting spoke (phase 6/7)
    GET  /                       the deck UI

State detection reuses exactly the probes the orchestrator already relies on
-- no new exporters are installed by this project:

    :9106 answers -> Windows        (that separate Grafana project's exporter)
    :9105 answers -> Linux
    :9107/status  -> Windows, and more reliable than :9106, which has died
                     on its own more than once
    icmp          -> the NIC has power, nothing more

Requests from loopback (the kiosk browser) need no token. Requests from
anywhere else need ?token= matching DECK_TOKEN in /etc/flightsim/boot.env,
same trust model as windows/boot-agent.ps1: fine behind a home router, not
something to expose to the internet.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── configuration ─────────────────────────────────────────────────────────

BOOT_ENV = Path("/etc/flightsim/boot.env")
ORCHESTRATOR = "/usr/local/bin/flightsim-boot.sh"
UI_DIR = Path(os.environ.get("DECK_UI_DIR", "/opt/flightdeck/deck-ui"))
# tmpfs: the Pi boots off a thumb drive and this file is rewritten on every
# state change. Nothing here needs to survive a reboot.
RUN_DIR = Path("/run/flightdeck")

LOCK_FILE = "/tmp/flightsim-boot.lock"

# Phase 1 is set when a trigger arrives here; 2-5 are derived from the
# orchestrator's own log lines and our probes; 6-7 need the Windows greeting
# to call back, which is not wired in v0.1 — they stay pending.
PHASES = [
    "IDLE",
    "TRIGGER",
    "PROBE",
    "KICK",
    "PING",
    "OS UP",
    "LOGON",
    "LAUNCHED",
]
PHASE_OS_UP = 5
PHASE_MAX_V01 = PHASE_OS_UP  # what v0.1 can observe without touching Windows


def read_env(path: Path) -> dict:
    """Parse the shell KEY=value file the orchestrator already reads."""
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        try:
            parts = shlex.split(val, comments=True)
        except ValueError:
            parts = [val.strip()]
        out[key] = parts[0] if parts else ""
    return out


ENV = read_env(BOOT_ENV)


def cfg(key: str, default: str) -> str:
    return os.environ.get(key) or ENV.get(key) or default


WS_LAN = cfg("WS_LAN", "192.168.1.50")
WIN_PORT = int(cfg("WIN_PORT", "9106"))
LINUX_PORT = int(cfg("LINUX_PORT", "9105"))
AGENT_PORT = int(cfg("WIN_AGENT_PORT", "9107"))
AGENT_TOKEN = cfg("WIN_AGENT_TOKEN", "")
DECK_TOKEN = cfg("DECK_TOKEN", "")
LISTEN_PORT = int(cfg("DECK_PORT", "8088"))
POLL_IDLE = float(cfg("DECK_POLL_IDLE", "5"))
POLL_BUSY = float(cfg("DECK_POLL_BUSY", "2"))

# Launch profiles the UI offers. Keys are the FLIGHT_INTENT values
# windows/jarvis-greeting.ps1 already understands.
PROFILES = {
    "sim": {"target": "windows", "label": "FLIGHT SIM", "sub": "MSFS 2024"},
    "squadrons": {"target": "windows", "label": "SQUADRONS", "sub": "STAR WARS"},
    "plain": {"target": "windows", "label": "WINDOWS", "sub": "greeting only"},
    "code": {"target": "linux", "label": "LINUX", "sub": "VS Code"},
}

# ── state ─────────────────────────────────────────────────────────────────


class Deck:
    """Everything the UI renders, behind one lock and a version counter."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self.version = 0
        self.state = {
            "workstation": {"os": "unknown", "since": None, "ip": WS_LAN,
                            "agent": False},
            "boot": {"in_flight": False, "target": None, "intent": None,
                     "phase": 0, "phase_name": "IDLE", "started": None,
                     "elapsed": 0, "result": None, "observable_max": PHASE_MAX_V01},
            "last_boot": None,
            "pi": {"temp_c": None, "load": None, "mem_pct": None, "uptime": None},
            "events": [],
            "profiles": PROFILES,
            "server_time": time.time(),
        }

    # -- mutation -----------------------------------------------------------

    def mutate(self, fn) -> None:
        with self._cv:
            fn(self.state)
            self.state["server_time"] = time.time()
            b = self.state["boot"]
            if b["in_flight"] and b["started"]:
                b["elapsed"] = round(time.time() - b["started"], 1)
            self.version += 1
            self._cv.notify_all()
        self._persist()

    def _persist(self) -> None:
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            tmp = RUN_DIR / "state.json.tmp"
            tmp.write_text(json.dumps(self.snapshot()))
            tmp.replace(RUN_DIR / "state.json")
        except OSError:
            pass  # a missing /run is not worth failing a boot over

    def snapshot(self) -> dict:
        with self._cv:
            return json.loads(json.dumps(self.state))

    def wait(self, seen: int, timeout: float):
        """Block until the state moves past `seen`. Returns (version, snap)."""
        with self._cv:
            if self.version == seen:
                self._cv.wait(timeout)
            return self.version, json.loads(json.dumps(self.state))

    # -- events -------------------------------------------------------------

    def event(self, source: str, text: str, level: str = "info") -> None:
        entry = {"ts": time.time(), "source": source, "text": text, "level": level}

        def apply(s):
            s["events"].insert(0, entry)
            del s["events"][40:]

        self.mutate(apply)

    def set_phase(self, phase: int, note: str | None = None) -> None:
        def apply(s):
            b = s["boot"]
            if not b["in_flight"]:
                return
            if phase > b["phase"]:
                b["phase"] = phase
                b["phase_name"] = PHASES[phase]

        self.mutate(apply)
        if note:
            self.event("boot", note)

    def begin(self, target: str, intent: str) -> None:
        def apply(s):
            s["boot"].update({
                "in_flight": True, "target": target, "intent": intent,
                "phase": 1, "phase_name": PHASES[1], "started": time.time(),
                "elapsed": 0, "result": None,
            })

        self.mutate(apply)

    def finish(self, result: str, note: str) -> None:
        def apply(s):
            b = s["boot"]
            if not b["in_flight"]:
                return
            b["in_flight"] = False
            b["result"] = result
            b["elapsed"] = round(time.time() - b["started"], 1) if b["started"] else 0
            s["last_boot"] = {
                "target": b["target"], "intent": b["intent"], "result": result,
                "seconds": b["elapsed"], "finished": time.time(),
                "reached_phase": b["phase"],
            }

        self.mutate(apply)
        self.event("boot", note, "ok" if result == "ok" else "warn")


DECK = Deck()

# ── probes ────────────────────────────────────────────────────────────────


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


_no_ping = threading.Event()


def pingable(host: str) -> bool:
    """ICMP liveness. Absence of the binary must not be fatal — without this
    guard a missing iputils-ping kills the poller thread on its first pass and
    the panel freezes on stale state with nothing to show for it."""
    try:
        return subprocess.run(
            ["ping", "-c1", "-W1", host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        if not _no_ping.is_set():
            _no_ping.set()
            DECK.event("deck-api",
                       "ping unavailable — 'off' and 'booting' cannot be told apart",
                       "warn")
        return False


def boot_lock_held() -> bool:
    """True while a detached orchestrator run holds its flock."""
    try:
        out = subprocess.run(
            ["flock", "-n", LOCK_FILE, "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
        return out.returncode != 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_pi_metrics() -> dict:
    """Local /proc reads. Not an exporter, and nothing is scraped or stored."""
    m = {"temp_c": None, "load": None, "mem_pct": None, "uptime": None}
    try:
        m["temp_c"] = round(
            int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        pass
    try:
        m["load"] = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0])
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        if total:
            m["mem_pct"] = round(100 * (1 - avail / total), 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        m["uptime"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return m


def poller() -> None:
    """Derive live OS state, and phases 4-5 when the orchestrator is quiet."""
    prev_os = None
    while True:
        try:
            prev_os = poll_once(prev_os)
        except Exception as exc:  # noqa: BLE001 - a dead poller is a dead panel
            DECK.event("deck-api", f"probe cycle failed: {exc}", "warn")
        time.sleep(POLL_BUSY if DECK.state["boot"]["in_flight"] else POLL_IDLE)


def poll_once(prev_os):
    """One probe cycle. Returns the OS it saw, for the next cycle to diff."""
    agent = tcp_ok(WS_LAN, AGENT_PORT)
    win = agent or http_ok(f"http://{WS_LAN}:{WIN_PORT}/")
    lin = http_ok(f"http://{WS_LAN}:{LINUX_PORT}/") if not win else False
    alive = win or lin or pingable(WS_LAN)

    if win:
        os_now = "windows"
    elif lin:
        os_now = "linux"
    elif alive:
        os_now = "booting"
    else:
        os_now = "off"

    changed = os_now != prev_os

    def apply(s):
        w = s["workstation"]
        if changed:
            w["since"] = time.time()
        w["os"] = os_now
        w["agent"] = agent
        s["pi"] = read_pi_metrics()

    DECK.mutate(apply)

    if changed and prev_os is not None:
        DECK.event("probe", f"workstation: {prev_os} -> {os_now}")

    boot = DECK.state["boot"]
    if boot["in_flight"]:
        target = boot["target"]
        reached = (target == "windows" and win) or (target == "linux" and lin)
        if reached:
            DECK.set_phase(PHASE_OS_UP)
            # The orchestrator logs "deck online" itself; if journald is
            # unreadable this is the only place the run gets closed out.
            if not journal_alive():
                DECK.finish("ok", f"{target} is up")
        elif alive:
            DECK.set_phase(4)
        elif not boot_lock_held() and time.time() - (boot["started"] or 0) > 20:
            # The run ended and we never saw its closing log line.
            DECK.finish("failed", "orchestrator exited without reaching target")

    return os_now


# ── following the orchestrator ────────────────────────────────────────────

_journal_ok = threading.Event()


def journal_alive() -> bool:
    return _journal_ok.is_set()


# flightsim-boot.sh's log() emits "[<target>] <message>".
LOG_RE = re.compile(r"^\[(windows|linux)\]\s*(.*)$")

# Ordered: first match wins.
LOG_PHASES = [
    (re.compile(r"trigger received"), 2, None),
    (re.compile(r"sending WOL"), 3, "WOL magic packet sent"),
    (re.compile(r"requesting reboot into"), 3, "reboot requested"),
    (re.compile(r"answers ping but no exporter"), 4, "host answers ping"),
]


def journal_follower() -> None:
    """Read the orchestrator's own logs. It is not modified to support this."""
    cmd = ["journalctl", "-t", "flightsim-boot", "-f", "-n", "0", "-o", "cat"]
    while True:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        except (OSError, subprocess.SubprocessError):
            _journal_ok.clear()
            DECK.event("deck-api", "journalctl unavailable — phases from probes only",
                       "warn")
            return

        _journal_ok.set()
        assert proc.stdout is not None
        for line in proc.stdout:
            handle_log_line(line.strip())
        _journal_ok.clear()
        time.sleep(3)  # journalctl died; back off and re-attach


def handle_log_line(line: str) -> None:
    if not line:
        return
    m = LOG_RE.match(line)
    target, msg = (m.group(1), m.group(2)) if m else (None, line)

    # A boot we did not start (someone ran the script by hand, or fauxmo is
    # still pointed at it directly). Adopt it so the panel stays honest.
    if "trigger received" in msg and not DECK.state["boot"]["in_flight"]:
        DECK.begin(target or "windows", "unknown")
        DECK.event("orchestrator", "adopted a boot we did not start")

    for pattern, phase, note in LOG_PHASES:
        if pattern.search(msg):
            DECK.set_phase(phase, note)
            break

    if "is up — deck online" in msg:
        DECK.set_phase(PHASE_OS_UP)
        DECK.finish("ok", f"{target or 'target'} is up — deck online")
    elif "already up — nothing to do" in msg:
        DECK.finish("ok", "already up — nothing to do")
    elif "already up — launching" in msg:
        DECK.set_phase(PHASE_OS_UP)
        DECK.finish("ok", "already up — launch requested")
    elif msg.startswith("gave up after"):
        DECK.finish("failed", msg)
    elif "already running" in msg:
        DECK.event("orchestrator", "duplicate trigger ignored", "warn")
    elif msg.startswith("WARN:"):
        DECK.event("orchestrator", msg, "warn")


# ── actions ───────────────────────────────────────────────────────────────


def fire_boot(target: str, intent: str) -> tuple[bool, str]:
    if target not in ("windows", "linux"):
        return False, "target must be windows or linux"
    if not Path(ORCHESTRATOR).is_file():
        return False, f"{ORCHESTRATOR} not installed"
    if DECK.state["boot"]["in_flight"]:
        return False, "a boot is already in flight"

    env = dict(os.environ, FLIGHT_INTENT=intent)
    try:
        # "bg" makes the script re-exec detached and return at once, exactly
        # as it does for fauxmo.
        subprocess.run([ORCHESTRATOR, target, "bg"], env=env, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not start orchestrator: {exc}"

    DECK.begin(target, intent)
    DECK.event("deck", f"trigger: {intent} -> {target}")
    return True, "started"


def fire_launch() -> tuple[bool, str]:
    if not AGENT_TOKEN:
        return False, "WIN_AGENT_TOKEN unset in /etc/flightsim/boot.env"
    url = f"http://{WS_LAN}:{AGENT_PORT}/launch?token={AGENT_TOKEN}"
    if http_ok(url, timeout=5):
        DECK.event("deck", "launch requested via boot-agent")
        return True, "launching"
    return False, "boot-agent did not answer"


def fire_abort() -> tuple[bool, str]:
    killed = subprocess.run(
        ["pkill", "-f", ORCHESTRATOR],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    DECK.finish("aborted", "aborted by operator")
    # Worth saying plainly: a magic packet already on the wire cannot be recalled.
    DECK.event("deck", "abort — a WOL packet already sent cannot be recalled", "warn")
    return True, "aborted" if killed else "no run was active"


# ── http ──────────────────────────────────────────────────────────────────

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deck-api/0.1"

    def log_message(self, fmt, *args):  # noqa: A003 - quieter journal
        pass

    # -- helpers ------------------------------------------------------------

    def _authorised(self, query: dict) -> bool:
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        if not DECK_TOKEN:
            return False
        return (query.get("token") or [""])[0] == DECK_TOKEN

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return {}

    # -- routes -------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path

        if path == "/api/state":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, DECK.snapshot())

        if path == "/api/events":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._sse()

        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})

        return self._static(path)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorised(query):
            return self._json(403, {"error": "forbidden"})

        body = self._body()
        path = url.path

        if path == "/api/boot":
            intent = str(body.get("intent") or "plain")
            target = str(body.get("target")
                         or PROFILES.get(intent, {}).get("target") or "windows")
            ok, msg = fire_boot(target, intent)
            return self._json(200 if ok else 409, {"ok": ok, "message": msg})

        if path == "/api/launch":
            ok, msg = fire_launch()
            return self._json(200 if ok else 502, {"ok": ok, "message": msg})

        if path == "/api/abort":
            ok, msg = fire_abort()
            return self._json(200, {"ok": ok, "message": msg})

        if path == "/api/hook/greeting":
            # Not wired in v0.1 — jarvis-greeting.ps1 is unmodified. The route
            # exists so the Windows side can be added later without an API
            # change, and so phases 6-7 light up the moment it is.
            phase = 7 if body.get("launched") else 6
            DECK.set_phase(phase, "greeting reported from Windows")
            return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found"})

    # -- SSE ----------------------------------------------------------------

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # No Content-Length and no chunking: the body is close-delimited, so
        # the connection must not be advertised as reusable.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        seen = -1
        try:
            while True:
                seen, snap = DECK.wait(seen, timeout=15)
                payload = json.dumps(snap)
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # kiosk reloaded or navigated away

    # -- static -------------------------------------------------------------

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (UI_DIR / rel).resolve()
        try:
            target.relative_to(UI_DIR.resolve())
        except ValueError:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def main() -> None:
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # systemd's RuntimeDirectory= normally creates this. Running by hand
        # as an ordinary user is still useful, just without the state file.
        pass

    for fn in (poller, journal_follower):
        threading.Thread(target=fn, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.daemon_threads = True

    def bye(*_):
        # shutdown() blocks until serve_forever() returns, and the signal
        # handler runs in the thread that IS serve_forever() — calling it
        # here directly deadlocks until systemd's stop timeout kills us.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    DECK.event("deck-api", f"listening on :{LISTEN_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
