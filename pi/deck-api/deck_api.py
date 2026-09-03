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
from urllib.parse import parse_qs, quote, urlparse

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
PHASE_LOGON = 6      # greeting's callback at Windows logon
PHASE_LAUNCHED = 7   # greeting's callback after starting the intent's app
PHASE_MAX_V01 = PHASE_OS_UP  # all a linux-target run can observe
# How long a Windows run waits at OS UP for the greeting to call back before
# closing anyway. Auto-logon plus the greeting takes well under a minute; five
# means the callback path is broken, and the run still closes "ok" — a missing
# greeting is a missing observation, not a failed boot.
CALLBACK_GRACE_S = 300


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
# Liveness probes — "does anything answer HTTP under this OS". These may point
# at something with no metrics at all; on this deployment :9108 simply returns
# the string "linux".
WIN_PORT = int(cfg("WIN_PORT", "9106"))
LINUX_PORT = int(cfg("LINUX_PORT", "9105"))

# Metrics endpoints — a different thing, and often a different address. The
# Linux exporter answers over the point-to-point link rather than the LAN, and
# exporters serve exposition format at /metrics, not at /.
WIN_METRICS_URL = cfg("WIN_METRICS_URL", f"http://{WS_LAN}:9106/metrics")
LINUX_METRICS_URL = cfg("LINUX_METRICS_URL", f"http://{WS_LAN}:9105/metrics")
AGENT_PORT = int(cfg("WIN_AGENT_PORT", "9107"))
AGENT_TOKEN = cfg("WIN_AGENT_TOKEN", "")
# windows/sim-agent — the SimConnect owner, on its own port and its own token.
# Windows-only by construction, and gone whenever MSFS is not running. Both are
# normal operation, so neither is reported as a fault.
# The Linux boot agent, so the panel can stop that OS as well as start it.
# deck-api knew only the Windows one until there was a reason to.
# The point-to-point link, which only exists under the workstation's Linux
# boot and which no DHCP lease can move. The orchestrator has always used it as
# a self-contained "Linux is up" signal; deck-api knew only WS_LAN, so the night
# the workstation's LAN address changed under it the panel called a running
# Linux box "off" while this link sat there answering. Derived from LINUX_SSH
# exactly as flightsim-boot.sh derives it, so boot.env stays the one place.
LINUX_PROBE_IP = cfg("LINUX_PROBE_IP", "") or cfg("LINUX_SSH", "").rpartition("@")[2]
LINUX_AGENT_PORT = int(cfg("LINUX_AGENT_PORT", "9108"))
LINUX_AGENT_TOKEN = cfg("LINUX_AGENT_TOKEN", "")
SIM_PORT = int(cfg("SIM_AGENT_PORT", "9109"))
SIM_TOKEN = cfg("SIM_AGENT_TOKEN", "")
# The Mac mini. Liveness only — it runs no exporter, so the card claims
# nothing beyond what a ping and two TCP handshakes can observe.
MAC_LAN = cfg("MAC_LAN", "192.168.68.68")
# The MacBook. Same liveness-only contract as the mini, but addressed by
# mDNS name, not IP: a laptop's DHCP lease moves with every couch and café,
# and a pinned address would call it "off" from the far side of the room.
# "off" is also what a closed lid reports — that is the truth of a laptop,
# not a fault, so nothing alarms on it.
MOBILE_HOST = cfg("MOBILE_HOST", "DougMobile.local")
# Which inputs to offer on a monitor that will NOT declare its own. Empty -
# the default - means offer everything.
#
# This used to default to the HP 32f's list, because that panel refused the DDC
# capabilities request and the operator had to state the fact somewhere. The
# background capabilities probe has since fixed that at the source: the HPs now
# declare vga/hdmi1/hdmi2 for themselves, so the only monitors still falling
# back here are the ones nobody knows anything about.
#
# For those, one desk-wide list is worse than nothing. A 49" Samsung that
# answers no capabilities request and misreports its own input as hdmi2 while
# sitting on DisplayPort got offered VGA, HDMI 1 and HDMI 2 and no way back to
# DP - a button that does nothing costs a tap, and a missing button costs a
# walk to the monitor's OSD.
MON_DEFAULT_INPUTS = [s for s in
                      cfg("MON_DEFAULT_INPUTS", "").split(",") if s.strip()]
# Now-playing under Linux, when someone builds the endpoint: same /media shape
# on this port. Absent is fine — the widget reads "no source".
LINUX_MEDIA_PORT = int(cfg("LINUX_MEDIA_PORT", "9110"))
LINUX_MEDIA_TOKEN = cfg("LINUX_MEDIA_TOKEN", "")
DECK_TOKEN = cfg("DECK_TOKEN", "")
LISTEN_PORT = int(cfg("DECK_PORT", "8088"))
POLL_IDLE = float(cfg("DECK_POLL_IDLE", "5"))
POLL_BUSY = float(cfg("DECK_POLL_BUSY", "2"))

# The pre-existing jobContext wallboard. Flight Deck displays it and owns
# none of it — no dashboards are provisioned, no metrics are stored, and the
# playlist selection below reproduces wallboard-kiosk.sh's semantics rather
# than inventing new ones. See docs/pi-wallboard-findings.md.
# 127.0.0.1, not "localhost": on Debian localhost resolves to ::1 first, and
# k3s's ServiceLB publishes Grafana's hostPort on IPv4 only — so urllib
# connected to ::1 and got nothing while curl's happy-eyeballs fell back.
GRAFANA_URL = cfg("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/")
PLAYLIST_PREFIX = cfg("PLAYLIST_PREFIX", "jcmcp-wallboard-")
# Only boards meant for a kiosk get a nav button. Set to "" to list them all.
EVALS_DASH_PREFIX = cfg("EVALS_DASH_PREFIX", "kiosk-")
DASH_CACHE_SECS = float(cfg("EVALS_DASH_CACHE", "60"))
# Nav-label aliases ("uid=LABEL;uid=LABEL") for boards whose uid no longer
# says what they show. kiosk-ollama's uid is load-bearing on the jobContext
# side (playlists reference it) but the board now shows whatever local model
# the workstation serves. An alias only relabels a board Grafana actually
# listed — a vanished uid still vanishes from the nav.
_DASH_ALIASES = {k.strip(): v.strip() for k, _, v in
                 (p.partition("=") for p in
                  cfg("EVALS_DASH_LABELS", "kiosk-ollama=LOCAL MODEL").split(";"))
                 if k.strip() and v.strip()}

def _stamp(path) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        return "unknown"


def _ui_stamp() -> str:
    """Newest mtime across the UI bundle.

    The kiosk browser keeps running across a deck-api restart, so installing
    new HTML/CSS/JS changes nothing on screen until the page is reloaded.
    Publishing this lets the page notice and reload itself.
    """
    try:
        newest = max(p.stat().st_mtime for p in UI_DIR.rglob('*') if p.is_file())
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(newest))
    except (OSError, ValueError):
        return "unknown"


try:
    _src = Path(__file__).resolve()
    BUILD = {"file": str(_src), "mtime": _stamp(_src), "ui": _ui_stamp()}
except OSError:
    BUILD = {"file": __file__, "mtime": "unknown", "ui": "unknown"}

# media came back as "audio", and this time it has something to show. It was
# dropped when it was a transport with three buttons on it, which the DECK rail
# does better in a tenth of the space; the rail widget stays for exactly that
# reason. What earns a surface is the spectrum — 2560x720 is a preposterous
# shape for a dashboard and the right one for a spectrum analyser, and 48 bands
# in a 600 px column is a garnish rather than a thing worth looking at.
# squadrons came and went: without a telemetry API the surface was a blind
# macro deck, and its SendInput keystrokes never reached the game anyway.
# nav rides the sim agent's /state (lat/lon/gs/track readouts) via /api/sim —
# no new endpoint, the pass-through already carries it.
SURFACES = ("deck", "evals", "nav", "sim", "displays", "audio", "idle")
# idle is the resting state: a clock and the boot tiles, nothing that updates
# faster than once a second. The panel spends most of its life with nothing
# happening, and its previous default - a dense wallboard for a stack that has
# since moved to cloud LLMs - meant maximum pixels at minimum relevance.
DEFAULT_SURFACE = cfg("DEFAULT_SURFACE", "idle")
if DEFAULT_SURFACE not in SURFACES:
    DEFAULT_SURFACE = "idle"

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
            # `since`: when the OS state last CHANGED. `last_alive`: when a
            # probe last got an answer. Two different clocks — the UI once
            # rendered `since` as "last seen", which reads "last seen 0 s" on a
            # host that just went dark. The moment it went dark is not the last
            # time it was seen.
            "workstation": {"os": "unknown", "since": None, "last_alive": None,
                            "ip": WS_LAN, "agent": False},
            "boot": {"in_flight": False, "target": None, "intent": None,
                     "phase": 0, "phase_name": "IDLE", "started": None,
                     "elapsed": 0, "result": None, "observable_max": PHASE_MAX_V01,
                     "os_up_at": None},
            "last_boot": None,
            "pi": {"temp_c": None, "load": None, "mem_pct": None,
                   "uptime": None, "cpu_pct": None},
            # Latest sample only — no history is kept anywhere on the Pi.
            "telemetry": {"ts": None, "source": None, "ws": {}, "pi": {}},
            # Coarse sim link, for the nav strip, on the ordinary poll cadence.
            # The SIM surface itself reads /api/sim far faster — see sim_state().
            "sim": {"link": False, "session": False, "aircraft": None,
                    "checked": None},
            # The NAV flight plan: ordered {name, lat, lon}. Held here so a
            # kiosk reload does not eat it; a deck-api restart does, which is
            # acceptable for something rebuilt in a few taps.
            "nav_plan": [],
            # The Mac mini: ping + service-port observations, nothing deeper.
            "mini": {"up": False, "ssh": False, "screen": False,
                     "ip": MAC_LAN, "last_alive": None},
            # The MacBook: same observations, addressed by name — a laptop
            # has no address worth pinning.
            "mobile": {"up": False, "ssh": False, "screen": False,
                       "host": MOBILE_HOST, "last_alive": None},
            # Which copy of this file is actually running. `git pull` updates
            # the checkout; only setup-deck.sh updates /opt/flightdeck, and
            # confusing the two costs an afternoon.
            "version": BUILD,
            "surface": {"active": DEFAULT_SURFACE, "default": DEFAULT_SURFACE,
                        "previous": None, "episode": None,
                        "manual_in_episode": False, "all": list(SURFACES)},
            # The wallboard, as Flight Deck sees it from outside.
            # `view` is "auto" (the rotating playlist, as the wallboard has
            # always behaved) or a dashboard uid the operator pinned.
            "evals": {"grafana": False, "mode": None, "url": None,
                      "checked": None, "playlist": None,
                      "view": "auto", "view_url": None, "dashboards": []},
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

    # -- surfaces -----------------------------------------------------------
    #
    # Automatic switching is scoped to an EPISODE (a boot, a sim session),
    # not to a cooldown timer. A timer either expires mid-boot and yanks the
    # screen away, or outlives the event and stops working. Episode scoping
    # makes "stop switching" last exactly as long as the thing it was told to
    # stop switching for.

    def set_surface(self, name: str, manual: bool) -> None:
        if name not in SURFACES:
            return

        def apply(s):
            sf = s["surface"]
            if manual:
                sf["active"] = name
                if sf["episode"]:
                    sf["manual_in_episode"] = True
            elif not (sf["episode"] and sf["manual_in_episode"]):
                sf["active"] = name

        self.mutate(apply)
        if manual:
            self.event("deck", f"surface: {name} (manual)")

    def open_episode(self, kind: str, surface: str) -> None:
        def apply(s):
            sf = s["surface"]
            if sf["episode"] is None:
                sf["previous"] = sf["active"]
            sf["episode"] = kind
            sf["manual_in_episode"] = False

        self.mutate(apply)
        self.set_surface(surface, manual=False)
        self.event("deck", f"auto-switched to {surface} ({kind} episode)")

    def close_episode(self, kind: str) -> None:
        target = None

        def apply(s):
            nonlocal target
            sf = s["surface"]
            if sf["episode"] != kind:
                return
            if not sf["manual_in_episode"]:
                target = sf["previous"] or sf["default"]
            sf["episode"] = None
            sf["manual_in_episode"] = False
            sf["previous"] = None

        self.mutate(apply)
        if target:
            self.set_surface(target, manual=False)

    def begin(self, target: str, intent: str) -> None:
        # What this run can actually observe: linux ends at OS UP (nothing
        # calls back from that boot); windows gets LOGON from the greeting,
        # and LAUNCHED too when the intent starts an app.
        if target == "windows":
            omax = PHASE_LAUNCHED if intent in ("sim", "squadrons") else PHASE_LOGON
        else:
            omax = PHASE_OS_UP

        def apply(s):
            s["boot"].update({
                "in_flight": True, "target": target, "intent": intent,
                "phase": 1, "phase_name": PHASES[1], "started": time.time(),
                "elapsed": 0, "result": None, "observable_max": omax,
                "os_up_at": None,
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
        # Boot over — back to whatever was showing, unless the operator
        # chose something during the episode.
        self.close_episode("boot")


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


def os_up_reached(note: str) -> None:
    """The target OS answered. Linux runs close here; Windows runs hold the
    door open for the greeting's LOGON/LAUNCHED callbacks, with the poller
    closing them out after CALLBACK_GRACE_S if the callbacks never come."""
    b = DECK.state["boot"]
    if not b["in_flight"]:
        return
    DECK.set_phase(PHASE_OS_UP)
    if b["target"] == "windows" and b.get("observable_max", 5) > PHASE_OS_UP:
        def apply(s):
            if s["boot"]["in_flight"] and s["boot"]["os_up_at"] is None:
                s["boot"]["os_up_at"] = time.time()
        DECK.mutate(apply)
    else:
        DECK.finish("ok", note)


def greeting_phase(body: dict) -> tuple[int, dict]:
    """The greeting's callback: the only writer phases 6-7 ever have.

    Authenticated with the boot-agent token, which the greeting can read off
    its own disk — same trust domain as the machine doing the logging on.
    """
    token = str(body.get("token") or "")
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        return 403, {"ok": False, "reason": "bad token"}
    phase = str(body.get("phase") or "")
    b = DECK.state["boot"]
    if not b["in_flight"]:
        return 200, {"ok": True, "note": "no run in flight — recorded nothing"}
    if phase == "logon":
        DECK.set_phase(PHASE_LOGON, "greeting: logged on")
        if b.get("observable_max", 5) <= PHASE_LOGON:
            DECK.finish("ok", "logged on — nothing to launch")
        return 200, {"ok": True}
    if phase == "launched":
        DECK.set_phase(PHASE_LAUNCHED)
        DECK.finish("ok", f"launched: {b.get('intent') or 'app'}")
        return 200, {"ok": True}
    return 400, {"ok": False, "reason": "phase must be logon or launched"}


def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_mini() -> dict:
    """The Mac mini's card: liveness and which doors are open, nothing more.

    macOS uses a randomized WiFi MAC, so the DHCP address can in principle
    drift — MAC_LAN in boot.env is the one knob to turn if it does.
    """
    up = pingable(MAC_LAN)
    return {"up": up,
            "ssh": up and tcp_open(MAC_LAN, 22),
            "screen": up and tcp_open(MAC_LAN, 5900),
            "ip": MAC_LAN,
            "last_alive": time.time() if up else
                DECK.state.get("mini", {}).get("last_alive")}


def fetch_mobile() -> dict:
    """The MacBook's card, on the mini's contract: liveness and open doors.

    Probed by mDNS name (see MOBILE_HOST) because the address moves. A sleeping
    laptop's Bonjour records may outlive it on the LAN's sleep proxy, but the
    proxy answers mDNS, not ICMP — so ping stays an honest liveness signal.
    """
    up = pingable(MOBILE_HOST)
    return {"up": up,
            "ssh": up and tcp_open(MOBILE_HOST, 22),
            "screen": up and tcp_open(MOBILE_HOST, 5900),
            "host": MOBILE_HOST,
            "last_alive": time.time() if up else
                DECK.state.get("mobile", {}).get("last_alive")}


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


# ── live telemetry ────────────────────────────────────────────────────────
#
# Sampled from the exporters that ALREADY exist (:9105 under Linux, :9106
# under Windows) — this project installs none of its own. Values are passed
# through to the panel as the latest reading and nothing more: deck-api keeps
# no history, writes no series, and stores nothing on disk. The rolling
# window behind the sparklines lives in the browser's memory and dies with
# the page. Flight Deck is a control surface, not an observability platform.

PROM_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN)$")

# Exporter metric names differ between the two exporters and have changed
# before, so match on shape rather than pinning exact names.
TELEMETRY_KEYS = [
    ("gpu_temp_c",       [r"gpu.*temp", r"temp.*gpu"]),
    ("gpu_util_pct",     [r"gpu.*util", r"util.*gpu"]),
    ("vram_used_bytes",  [r"gpu_memory_used", r"vram.*used"]),
    ("vram_total_bytes", [r"gpu_memory_total", r"vram.*total"]),
    ("cpu_pct",          [r"cpu.*(util|percent|pct)"]),
    ("mem_pct",          [r"mem.*(percent|pct)"]),
]


def parse_prom_text(text: str) -> dict:
    """Prometheus exposition format -> {metric_name: value}.

    Labels are dropped and the first sample of a name wins. That is lossy in
    general and exactly right here: the panel wants one live number per box,
    not a series.
    """
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = PROM_LINE.match(line)
        if not m:
            continue
        name, _labels, val = m.groups()
        if name in out:
            continue
        try:
            out[name] = float(val)
        except ValueError:
            pass
    return out


def canonicalise(raw: dict) -> dict:
    """Map whatever the exporter called things onto the panel's few keys."""
    found = {}
    for key, patterns in TELEMETRY_KEYS:
        for pat in patterns:
            hit = next((n for n in raw if re.search(pat, n, re.I)), None)
            if hit is not None:
                found[key] = raw[hit]
                break
    used, total = found.get("vram_used_bytes"), found.get("vram_total_bytes")
    if used is not None and total:
        found["vram_pct"] = round(100 * used / total, 1)
    return found


_bad_metrics: set[str] = set()


def fetch_telemetry(os_now: str) -> tuple[dict, str | None]:
    """One scrape of the exporter belonging to whichever OS is up.

    The METRICS endpoint is not the liveness probe. WIN_PORT / LINUX_PORT are
    what the orchestrator pings to decide which OS is running, and they may be
    anything that answers HTTP under one OS only — on this deployment :9108
    just returns the string "linux". Metrics live at /metrics, and possibly on
    a different address entirely: the Linux exporter is reachable over the
    point-to-point link rather than the LAN. Hence separate configuration.
    """
    if os_now == "windows":
        # The sim agent first. GPU telemetry used to come only from a Prometheus
        # exporter belonging to another project, and each of the three times it
        # died it took the DECK gauges with it while the machine underneath was
        # perfectly healthy. This agent is ours, runs whenever Windows does, and
        # reads the same nvidia-smi. The exporter stays as the fallback, so a
        # deployment without this agent behaves exactly as it always has.
        if SIM_TOKEN:
            own = _scrape(_sim_url("/metrics"))
            if own:
                return own, f"windows {WS_LAN}:{SIM_PORT}/metrics"
        url, src = WIN_METRICS_URL, "windows"
    elif os_now == "linux":
        url, src = LINUX_METRICS_URL, "linux"
    else:
        return {}, None  # no OS up: a real gap, and the panel shows it as one
    if not url:
        return {}, None
    values = _scrape(url)
    return (values, f"{src} {url}") if values else ({}, None)


def _scrape(url: str) -> dict:
    """One exposition-format endpoint, canonicalised. Empty on any failure.

    Complains once per URL rather than every cycle: a wrong endpoint that says
    so at the panel's poll rate buries the log it is trying to appear in. The
    sim agent's own /metrics answers a comment and no samples when the box has
    no NVIDIA card, which lands here as an ordinary empty scrape - correctly,
    because a machine with no GPU has no GPU telemetry, and that is not a fault
    worth a warning.
    """
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            body = r.read(512_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return {}

    # A directory listing or an error page is not exposition format. Say so
    # once rather than silently reporting an empty scrape forever.
    if body.lstrip()[:1] == "<":
        if url not in _bad_metrics:
            _bad_metrics.add(url)
            DECK.event("deck-api", f"{url} returned HTML, not Prometheus text", "warn")
        return {}

    values = canonicalise(parse_prom_text(body))
    # Only the CONFIGURED exporter is worth complaining about. The sim agent
    # legitimately returns nothing on a box without nvidia-smi, and warning
    # about that would fire forever on a perfectly healthy machine.
    if not values and url not in _bad_metrics and "/metrics" in url \
            and url in (WIN_METRICS_URL, LINUX_METRICS_URL):
        _bad_metrics.add(url)
        DECK.event("deck-api", f"{url} exposes no GPU metrics this panel recognises",
                   "warn")
    return values


def _sim_url(path: str) -> str:
    return f"http://{WS_LAN}:{SIM_PORT}{path}?token={quote(SIM_TOKEN)}"


def audio_spectrum() -> dict:
    """The workstation's output spectrum, for the panel's visualiser.

    Measured where the sound actually is. The panel is on the Pi and the music
    plays on the workstation, so the Edge has no signal of its own to look at -
    and a visualiser driven by anything other than the audio would be the same
    kind of lie the SIM surface refuses to tell about the gear.

    A short timeout on purpose: this is polled fast, and a visualiser that
    stalls the panel waiting for bars is worse than one that drops a frame.
    """
    # Branch on the booted OS, exactly as /media and /monitor do. This asked
    # the sim agent unconditionally for a while, so under Linux it interrogated
    # a Windows service that was not running and the visualiser was simply dead
    # on that boot - while the track name beside it, which does branch, kept
    # working and made it look like a bug rather than a missing half.
    os_now = DECK.state["workstation"]["os"]
    if os_now == "windows" and SIM_TOKEN:
        url = _sim_url("/audio")
    elif os_now == "linux" and LINUX_MEDIA_TOKEN:
        url = (f"http://{WS_LAN}:{LINUX_MEDIA_PORT}/audio"
               f"?token={quote(LINUX_MEDIA_TOKEN)}")
    else:
        return {"active": False, "reason": "no agent answering on the booted OS"}
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            return json.loads(r.read(32_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return {"active": False, "reason": "no link to the workstation"}


def fetch_sim_link(os_now: str) -> dict:
    """Is the sim agent there, and does it hold a live SimConnect session?

    Cheap enough for the ordinary poll: one /health call, only under Windows.
    The agent lives and dies with that boot, so its absence is not an error and
    is never reported as one — the strip just reads no link.
    """
    blank = {"link": False, "session": False, "aircraft": None,
             "checked": time.time()}
    if os_now != "windows" or not SIM_TOKEN:
        return blank
    try:
        with urllib.request.urlopen(_sim_url("/health"), timeout=2) as r:
            health = json.loads(r.read(64_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return blank
    sim = health.get("sim") or {}
    return {"link": True,
            # connected means a live SimConnect session, not "a process is up"
            "session": bool(sim.get("connected")),
            "aircraft": sim.get("aircraft") or None,
            "checked": time.time()}


def sim_state() -> dict:
    """One pass-through of the agent's /state, for /api/sim.

    Proxied rather than fetched by the browser directly: the panel would
    otherwise need the agent's token in page JavaScript and a CORS grant on a
    token-guarded LAN endpoint. Neither is worth it when deck-api is already
    talking to the workstation.

    Nothing is reshaped here. The agent publishes normalized Flight Deck state
    and no SimVar name, unit or event id reaches this file — that is the whole
    point of the split, and re-deriving anything here would undo it.
    """
    if not SIM_TOKEN:
        return {"link": False, "session": False,
                "reason": "SIM_AGENT_TOKEN unset in /etc/flightsim/boot.env"}
    try:
        with urllib.request.urlopen(_sim_url("/state"), timeout=2) as r:
            return {"link": True, "session": True,
                    "state": json.loads(r.read(256_000).decode("utf-8", "replace"))}
    except urllib.error.HTTPError as e:
        # Must precede URLError — HTTPError is a subclass. 503 is the agent
        # saying it is up but holds no session: MSFS is not running.
        if e.code == 503:
            return {"link": True, "session": False, "reason": "no simulator session"}
        if e.code == 403:
            return {"link": True, "session": False, "reason": "sim agent rejected the token"}
        return {"link": True, "session": False, "reason": f"sim agent returned {e.code}"}
    except (urllib.error.URLError, OSError, ValueError):
        return {"link": False, "session": False, "reason": "no link to the workstation"}


def _ws_json(url: str, payload: dict | None = None, timeout: float = 3) -> dict | None:
    """One JSON round-trip to the workstation; None on any failure."""
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"} if payload is not None else {},
            method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(256_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _media_url(path: str, os_now: str) -> str | None:
    """One media endpoint on whichever agent the booted OS is running.

    Exists because only ONE of the three media paths branched. /media did, so
    the widget showed the track under Linux; /media/command and /media/art did
    not, so the transport buttons posted at a Windows service that was not
    running and the album art never arrived. Both failed silently, next to a
    working track name, which reads as broken buttons rather than a missing
    half. Same drift the spectrum had: teach the read path about both boots and
    leave the rest dialling Windows.
    """
    if os_now == "windows" and SIM_TOKEN:
        return _sim_url(path)
    if os_now == "linux" and LINUX_MEDIA_TOKEN:
        return (f"http://{WS_LAN}:{LINUX_MEDIA_PORT}{path}"
                f"?token={quote(LINUX_MEDIA_TOKEN)}")
    return None


def media_state(os_now: str) -> dict:
    """Now-playing from whichever OS is up. Same normalized shape either way;
    the widget renders the source's absence as "no source", never an error."""
    url = _media_url("/media", os_now)
    if url:
        got = _ws_json(url)
        if got is not None:
            return {"source": os_now, **got}
    return {"source": None, "active": False}


def media_art(os_now: str) -> bytes | None:
    url = _media_url("/media/art", os_now)
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.read(2_000_000)
    except (urllib.error.URLError, OSError):
        return None


_geo_cache: dict[str, list] = {}
_geo_last = 0.0


def geocode(q: str) -> list:
    """Place search via OSM Nominatim, proxied for the panel.

    Proxied rather than fetched by the browser because Nominatim's usage
    policy wants a real identifying User-Agent, which browser fetch cannot
    set. Cached per query, and paced to their 1 req/s ask — searches are
    human-typed on a touch keyboard, so the pacing is invisible.
    """
    global _geo_last
    q = q.strip()[:80]
    if len(q) < 2:
        return []
    key = q.lower()
    if key in _geo_cache:
        return _geo_cache[key]
    wait = 1.1 - (time.time() - _geo_last)
    if wait > 0:
        time.sleep(wait)
    _geo_last = time.time()
    url = ("https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&q="
           + quote(q))
    req = urllib.request.Request(url, headers={
        "User-Agent": "flightdeck-deck-api/0.1 (personal wall panel; single user)"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = json.loads(r.read(256_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    out = []
    for hit in raw if isinstance(raw, list) else []:
        try:
            out.append({
                # First comma-part is the place itself; the tail is the
                # address chain, kept short for a 430 px rail.
                "name": str(hit.get("display_name", "")).split(",")[0][:40],
                "detail": ", ".join(str(hit.get("display_name", "")).split(",")[1:3]).strip()[:60],
                "lat": float(hit["lat"]), "lon": float(hit["lon"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    _geo_cache[key] = out
    return out


def _clean_waypoints(wpts) -> list | None:
    if not isinstance(wpts, list) or len(wpts) > 12:
        return None
    clean = []
    for w in wpts:
        try:
            lat, lon = float(w["lat"]), float(w["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
            waypoint = {"name": str(w.get("name", "WPT"))[:40] or "WPT",
                        "lat": lat, "lon": lon}
            if w.get("altitude_ft") is not None:
                altitude = float(w["altitude_ft"])
                if not (0 <= altitude <= 60000):
                    raise ValueError
                waypoint["altitude_ft"] = altitude
            clean.append(waypoint)
        except (KeyError, TypeError, ValueError):
            return None
    return clean


def set_nav_plan(body: dict) -> tuple[int, dict]:
    clean = _clean_waypoints(body.get("waypoints"))
    if clean is None:
        return 400, {"ok": False, "reason": "waypoints must be a valid list of at most 12"}

    def apply(s):
        s["nav_plan"] = clean
    DECK.mutate(apply)
    return 200, {"ok": True, "count": len(clean)}


# Saved plans persist to disk, unlike the live plan: a route worth naming is
# worth keeping across restarts and reboots.
PLANS_FILE = Path(cfg("DECK_PLANS_FILE", "/var/lib/flightdeck/plans.json"))


def _load_plans() -> dict:
    try:
        data = json.loads(PLANS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store_plans(plans: dict) -> bool:
    try:
        PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PLANS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(plans))
        tmp.replace(PLANS_FILE)
        return True
    except OSError:
        return False


def nav_plans_op(body: dict) -> tuple[int, dict]:
    action = str(body.get("action") or "")
    name = str(body.get("name") or "").strip()[:24]
    plans = _load_plans()

    if action == "save":
        if not name:
            return 400, {"ok": False, "reason": "a plan needs a name"}
        current = DECK.state["nav_plan"]
        if not current:
            return 400, {"ok": False, "reason": "nothing to save — the plan is empty"}
        if name not in plans and len(plans) >= 20:
            return 400, {"ok": False, "reason": "20 plans is plenty; delete one first"}
        plans[name] = current
        if not _store_plans(plans):
            return 500, {"ok": False, "reason": f"cannot write {PLANS_FILE}"}
        return 200, {"ok": True}

    if action == "load":
        if name not in plans:
            return 404, {"ok": False, "reason": "no such plan"}
        clean = _clean_waypoints(plans[name]) or []

        def apply(s):
            s["nav_plan"] = clean
        DECK.mutate(apply)
        return 200, {"ok": True, "count": len(clean)}

    if action == "delete":
        if plans.pop(name, None) is None:
            return 404, {"ok": False, "reason": "no such plan"}
        if not _store_plans(plans):
            return 500, {"ok": False, "reason": f"cannot write {PLANS_FILE}"}
        return 200, {"ok": True}

    return 400, {"ok": False, "reason": "action must be save, load or delete"}


def _monitor_url(method: str, payload: dict | None = None) -> dict | None:
    """Monitor DDC through whichever agent the booted OS is running.

    Windows answers via the sim agent, Linux via media-agent.py, and both
    publish the identical shape — so SCREENS never learns which OS is
    holding the video cables. The workstation is always in one or the other,
    which is exactly why both need covering.
    """
    os_now = DECK.state["workstation"]["os"]
    if os_now == "windows" and SIM_TOKEN:
        url = _sim_url("/monitor")
    elif os_now == "linux" and LINUX_MEDIA_TOKEN:
        url = (f"http://{WS_LAN}:{LINUX_MEDIA_PORT}/monitor"
               f"?token={quote(LINUX_MEDIA_TOKEN)}")
    else:
        return None
    # DDC is slow on both sides: ddcutil probes I2C buses, dxva2 waits on the
    # panel. 20 s beats the default 3 s, which would have timed out honest work.
    return _ws_json(url, payload, timeout=20)


def sim_command(body: dict) -> tuple[int, dict]:
    """Forward one control command to the agent, verbatim.

    `accepted` means the agent transmitted the event to the simulator. It
    never means the aircraft did anything: that only ever shows up as
    observed state moving, which is the only thing the panel renders. A
    command MSFS declines — over the gear speed limit, on the ground, wrong
    aircraft — comes back accepted and then simply never moves anything.
    """
    if not SIM_TOKEN:
        return 503, {"accepted": False,
                     "reason": "SIM_AGENT_TOKEN unset in /etc/flightsim/boot.env"}
    payload = json.dumps({
        "cmd_id": str(body.get("cmd_id") or ""),
        "control": str(body.get("control") or ""),
        "action": str(body.get("action") or ""),
        "value": body.get("value"),
    }).encode()
    req = urllib.request.Request(
        _sim_url("/command"), data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return 200, json.loads(r.read(64_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {"accepted": False, "reason": f"sim agent returned {e.code}"}
    except (urllib.error.URLError, OSError, ValueError):
        return 502, {"accepted": False, "reason": "no link to the workstation"}


_prev_cpu: tuple[int, int] | None = None


def pi_cpu_pct() -> float | None:
    """Busy percentage since the previous call, from /proc/stat deltas."""
    global _prev_cpu
    try:
        fields = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
        vals = [int(x) for x in fields]
    except (OSError, ValueError, IndexError):
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    prev, _prev_cpu = _prev_cpu, (total, idle)
    if not prev:
        return None
    d_total, d_idle = total - prev[0], idle - prev[1]
    if d_total <= 0:
        return None
    return round(100 * (1 - d_idle / d_total), 1)


# ── the wallboard, observed from outside ──────────────────────────────────


def resolve_playlist(mode: str) -> tuple[str | None, str | None]:
    """Look the playlist up BY NAME and return (url, playlist_name).

    Never bake a uid: wallboard-kiosk.sh's own comment records that Grafana
    re-mints playlist uids, so a hardcoded one "broke every time the playlist
    was recreated". That behaviour is reproduced here, not reinvented.
    """
    name = f"{PLAYLIST_PREFIX}{mode}"
    try:
        with urllib.request.urlopen(f"{GRAFANA_URL}/api/playlists", timeout=5) as r:
            playlists = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None, name
    for p in playlists:
        if p.get("name") == name and p.get("uid"):
            return f"{GRAFANA_URL}/playlists/play/{p['uid']}?kiosk", name
    return None, name


def playlist_mode(os_now: str, current: str | None) -> str:
    """Which OS playlist to show.

    wallboard-kiosk.sh treats an unresolvable probe as "unknown" and pointedly
    does NOT swap on it, so a network blip cannot flap the display. Here the
    equivalent of unknown is any state that is not definitely one OS or the
    other — mid-boot, powered off, not yet probed.
    """
    if os_now == "windows":
        return "windows"
    if os_now == "linux":
        return "linux"
    return current or "linux"


_dash_cache: tuple[float, list] = (0.0, [])


def list_dashboards() -> list:
    """The boards Grafana currently has, for the EVALS sub-navigation.

    Resolved at runtime by uid, for the same reason playlists are: these are
    jobContext's dashboards and it may add, rename or recreate them at any
    time. Flight Deck provisions none of them and hardcodes none of them.
    """
    global _dash_cache
    age, cached = _dash_cache
    if cached and (time.time() - age) < DASH_CACHE_SECS:
        return cached
    try:
        with urllib.request.urlopen(
                f"{GRAFANA_URL}/api/search?type=dash-db&limit=100", timeout=5) as r:
            items = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return cached  # keep the last good list rather than emptying the nav

    out = []
    for it in items:
        uid, title = it.get("uid"), it.get("title") or ""
        if not uid or (EVALS_DASH_PREFIX and not uid.startswith(EVALS_DASH_PREFIX)):
            continue
        # "kiosk-provenance" -> "PROVENANCE". The real titles are far too long
        # for a nav button ("Provenance — Generation Truth Gate").
        label = (_DASH_ALIASES.get(uid)
                 or uid[len(EVALS_DASH_PREFIX):].replace("-", " ").strip().upper()
                 or uid.upper())
        out.append({"uid": uid, "title": title, "label": label})
    out.sort(key=lambda d: d["label"])
    _dash_cache = (time.time(), out)
    return out


def refresh_evals(os_now: str) -> None:
    healthy = http_ok(f"{GRAFANA_URL}/api/health", timeout=4)
    current = DECK.state["evals"].get("mode")
    mode = playlist_mode(os_now, current)
    url, name = (resolve_playlist(mode) if healthy else (None, None))
    boards = list_dashboards() if healthy else None

    def apply(s):
        e = s["evals"]
        e["grafana"] = healthy
        e["checked"] = time.time()
        if not healthy:
            return
        e["mode"] = mode
        e["playlist"] = name
        # Keep the last good URL rather than blanking the iframe on a
        # single failed lookup.
        if url:
            e["url"] = url
        if boards is not None:
            e["dashboards"] = boards

        # A pinned board stays pinned across an OS flip — same "manual wins"
        # rule the surfaces use. Only AUTO follows the playlist.
        if e["view"] == "auto":
            e["view_url"] = e["url"]
        else:
            e["view_url"] = f"{GRAFANA_URL}/d/{e['view']}?kiosk"

    DECK.mutate(apply)


def read_pi_metrics() -> dict:
    """Local /proc reads. Not an exporter, and nothing is scraped or stored."""
    m = {"temp_c": None, "load": None, "mem_pct": None, "uptime": None,
         "cpu_pct": pi_cpu_pct()}
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


def linux_seen() -> bool:
    """Linux, by any route this Pi has.

    Three, because the first two both depend on the workstation's LAN address
    being what boot.env says it is - and a machine that is powered off cannot
    defend its DHCP lease, so that assumption has already failed once. The
    point-to-point link is immune: it exists only under the Linux boot and no
    router hands out its addresses.
    """
    if http_ok(f"http://{WS_LAN}:{LINUX_PORT}/"):
        return True
    if tcp_ok(WS_LAN, LINUX_AGENT_PORT):
        return True
    if not LINUX_PROBE_IP:
        return False
    return tcp_ok(LINUX_PROBE_IP, 22) or pingable(LINUX_PROBE_IP)


def poll_once(prev_os):
    """One probe cycle. Returns the OS it saw, for the next cycle to diff."""
    agent = tcp_ok(WS_LAN, AGENT_PORT)
    win = agent or http_ok(f"http://{WS_LAN}:{WIN_PORT}/")
    lin = linux_seen() if not win else False
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
    ws_telemetry, source = fetch_telemetry(os_now)
    sim_link = fetch_sim_link(os_now)
    mini = fetch_mini()
    mobile = fetch_mobile()

    def apply(s):
        w = s["workstation"]
        if changed:
            w["since"] = time.time()
        if alive:
            w["last_alive"] = time.time()
        w["os"] = os_now
        w["agent"] = agent
        s["sim"] = sim_link
        s["mini"] = mini
        s["mobile"] = mobile
        s["pi"] = read_pi_metrics()
        # Latest reading only. The browser holds the rolling window.
        s["telemetry"] = {"ts": time.time(), "source": source,
                          "ws": ws_telemetry, "pi": s["pi"]}

    DECK.mutate(apply)

    if changed and prev_os is not None:
        DECK.event("probe", f"workstation: {prev_os} -> {os_now}")

    refresh_evals(os_now)

    boot = DECK.state["boot"]
    if boot["in_flight"]:
        target = boot["target"]
        reached = (target == "windows" and win) or (target == "linux" and lin)
        # A Windows run holding the door for greeting callbacks that never
        # came: close it "ok" — a missing greeting is a missing observation,
        # not a failed boot.
        if boot.get("os_up_at") and time.time() - boot["os_up_at"] > CALLBACK_GRACE_S:
            DECK.finish("ok", "closed at OS UP — no greeting callback")
        elif reached:
            os_up_reached(f"{target} is up")
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
        # Voice, or someone running the script by hand — DECK either way.
        DECK.open_episode("boot", "deck")

    for pattern, phase, note in LOG_PHASES:
        if pattern.search(msg):
            DECK.set_phase(phase, note)
            break

    if "is up — deck online" in msg:
        os_up_reached(f"{target or 'target'} is up — deck online")
    elif "already up — nothing to do" in msg:
        DECK.finish("ok", "already up — nothing to do")
    elif "already up — launching" in msg:
        # Windows was already up and the greeting task was kicked — its
        # callbacks are coming, so hold the door like any other windows run.
        os_up_reached("already up — launch requested")
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
    # A boot shows DECK regardless of where it came from.
    DECK.open_episode("boot", "deck")
    return True, "started"


def fire_shutdown() -> tuple[bool, str]:
    """Power the workstation off, through whichever OS is holding it.

    Refused while a boot is in flight: the two would race, and there is
    already a control for stopping a boot. ABORT stops a launch; this stops a
    machine, and conflating them would make the more destructive one reachable
    by accident.
    """
    os_now = DECK.state["workstation"]["os"]
    if os_now == "off":
        return False, "the workstation is already off"
    if DECK.state["boot"]["in_flight"]:
        return False, "a boot is in flight - abort it first"
    if os_now == "windows":
        if not AGENT_TOKEN:
            return False, "WIN_AGENT_TOKEN unset in /etc/flightsim/boot.env"
        url = f"http://{WS_LAN}:{AGENT_PORT}/shutdown?token={AGENT_TOKEN}"
    elif os_now == "linux":
        if not LINUX_AGENT_TOKEN:
            return False, "LINUX_AGENT_TOKEN unset in /etc/flightsim/boot.env"
        url = f"http://{WS_LAN}:{LINUX_AGENT_PORT}/shutdown?token={LINUX_AGENT_TOKEN}"
    else:
        return False, f"nothing to shut down: workstation is {os_now}"
    if http_ok(url, timeout=5):
        DECK.event("deck", f"shutdown requested via the {os_now} agent")
        return True, "powering off"
    return False, "the boot agent did not answer"


def fire_launch() -> tuple[bool, str]:
    if not AGENT_TOKEN:
        return False, "WIN_AGENT_TOKEN unset in /etc/flightsim/boot.env"
    url = f"http://{WS_LAN}:{AGENT_PORT}/launch?token={AGENT_TOKEN}"
    if http_ok(url, timeout=5):
        DECK.event("deck", "launch requested via boot-agent")
        return True, "launching"
    return False, "boot-agent did not answer"


def llama_state() -> dict:
    """The local model, as observed right now — never remembered.

    Windows answers through the boot agent's /llama/status, which can tell
    "loading" (process up, /health not yet 200) from "stopped". Linux has no
    agent route for this yet, so the Pi probes :8081/health itself — running
    or stopped, no loading distinction, and no toggle. `toggle` says whether
    POST /api/llama can act, so the panel never renders a control that can
    only 502. When nothing that could host the model is up, the state is
    absent — not "stopped", per the absent-is-not-false rule.
    """
    os_now = DECK.state["workstation"]["os"]
    if os_now == "windows":
        try:
            with urllib.request.urlopen(
                    f"http://{WS_LAN}:{AGENT_PORT}/llama/status", timeout=4) as r:
                word = r.read(32).decode("ascii", "replace").strip()
            if word in ("running", "loading", "stopped"):
                return {"os": os_now, "state": word, "toggle": True}
        except OSError:
            pass
        return {"os": os_now, "state": "unknown", "toggle": False}
    if os_now == "linux":
        up = http_ok(f"http://{WS_LAN}:8081/health", timeout=3)
        return {"os": os_now, "state": "running" if up else "stopped",
                "toggle": False}
    return {"os": os_now, "state": "absent", "toggle": False}


def fire_llama(action: str) -> tuple[bool, str]:
    if action not in ("start", "stop"):
        return False, "action must be start or stop"
    # Windows only for now: the Linux boot's llama is a systemd unit and its
    # agent has no llama route yet. Mirrored by `toggle` in llama_state().
    if DECK.state["workstation"]["os"] != "windows":
        return False, "the llama toggle needs Windows up"
    if not AGENT_TOKEN:
        return False, "WIN_AGENT_TOKEN unset in /etc/flightsim/boot.env"
    url = f"http://{WS_LAN}:{AGENT_PORT}/llama/{action}?token={AGENT_TOKEN}"
    if http_ok(url, timeout=5):
        DECK.event("deck", f"local model {action} via boot-agent")
        return True, f"{action} requested"
    return False, "the boot agent did not answer"


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

        # Deliberately outside the SSE state. The SIM surface polls this several
        # times a second while it is on screen, and gear travel takes about five
        # seconds end to end — far too fast for the 2-5 s state poll to catch.
        # Routing it through DECK.mutate instead would bump the version, wake
        # every SSE client and rewrite state.json to the SD card at the same
        # rate, which is not a trade worth making for one surface.
        if path == "/api/sim":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, sim_state())

        if path == "/api/audio":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, audio_spectrum())

        if path == "/api/monitor":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            got = _monitor_url("GET")
            return self._json(200, {"available": bool(got),
                                    "default_inputs": MON_DEFAULT_INPUTS,
                                    "reason": None if got else
                                    "no agent answering on the booted OS",
                                    **(got or {})})

        if path == "/api/nav/geocode":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, {"results": geocode((query.get("q") or [""])[0])})

        if path == "/api/nav/plans":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            plans = _load_plans()
            return self._json(200, {"plans": [
                {"name": n, "count": len(w)} for n, w in sorted(plans.items())
            ]})

        # Same fast-poll family as /api/sim: polled only while the DECK
        # surface shows the LOCAL MODEL control, off the SSE state.
        if path == "/api/llama":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, llama_state())

        # Media and squadrons ride the same fast-poll pattern as /api/sim and
        # stay off the SSE state for the same reasons.
        if path == "/api/media":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, media_state(DECK.state["workstation"]["os"]))

        if path == "/api/media/art":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            art = media_art(DECK.state["workstation"]["os"])
            if art is None:
                return self._json(404, {"error": "no art"})
            return self._send(200, art, "image/jpeg")


        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})

        return self._static(path)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)

        # The greeting's phase callback authenticates itself with the
        # boot-agent token in its body — it has never held DECK_TOKEN, so it
        # cannot pass the blanket gate below and must be routed first.
        if url.path == "/api/phase":
            code, out = greeting_phase(self._body())
            return self._json(code, out)

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

        if path == "/api/shutdown":
            ok, msg = fire_shutdown()
            return self._json(200 if ok else 409, {"ok": ok, "message": msg})

        if path == "/api/launch":
            ok, msg = fire_launch()
            return self._json(200 if ok else 502, {"ok": ok, "message": msg})

        if path == "/api/llama":
            ok, msg = fire_llama(str(body.get("action") or ""))
            return self._json(200 if ok else 502, {"ok": ok, "message": msg})

        if path == "/api/sim/command":
            code, out = sim_command(body)
            return self._json(code, out)

        if path == "/api/monitor":
            payload = {"input": str(body.get("input") or "")}
            if isinstance(body.get("index"), int):
                payload["index"] = body["index"]
            got = _monitor_url("POST", payload)
            return self._json(200 if got else 502,
                              got or {"ok": False, "reason": "no agent answering"})

        if path == "/api/nav/plan":
            code, out = set_nav_plan(body)
            return self._json(code, out)

        if path == "/api/nav/plans":
            code, out = nav_plans_op(body)
            return self._json(code, out)

        if path == "/api/media/command":
            url = _media_url("/media/command", DECK.state["workstation"]["os"])
            got = _ws_json(url, {"action": str(body.get("action") or "")}) \
                if url else None
            return self._json(200 if got else 502, got or {"sent": False})


        if path == "/api/abort":
            ok, msg = fire_abort()
            return self._json(200, {"ok": ok, "message": msg})

        if path == "/api/evals/view":
            view = str(body.get("view") or "auto")
            known = {d["uid"] for d in DECK.state["evals"].get("dashboards", [])}
            if view != "auto" and view not in known:
                return self._json(400, {"ok": False,
                                        "message": f"unknown dashboard {view!r}"})

            def apply(s, _v=view):
                e = s["evals"]
                e["view"] = _v
                e["view_url"] = (e["url"] if _v == "auto"
                                 else f"{GRAFANA_URL}/d/{_v}?kiosk")

            DECK.mutate(apply)
            DECK.event("deck", f"evals view: {view}")
            return self._json(200, {"ok": True, "view": view,
                                    "url": DECK.state["evals"]["view_url"]})

        if path == "/api/surface":
            name = str(body.get("surface") or "")
            if name not in SURFACES:
                return self._json(400, {"ok": False,
                                        "message": f"surface must be one of {list(SURFACES)}"})
            # Manual always wins, and suppresses automatic switching for the
            # rest of the current episode.
            DECK.set_surface(name, manual=True)
            return self._json(200, {"ok": True, "surface": name})

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

    DECK.event("deck-api",
               f"listening on :{LISTEN_PORT} — {BUILD['file']} ({BUILD['mtime']})")
    server.serve_forever()


if __name__ == "__main__":
    main()
