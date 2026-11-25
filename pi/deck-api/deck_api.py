#!/usr/bin/env python3
"""deck-api — one entry point for every Flight Deck boot trigger.

Runs ON THE PI, installed to /opt/flightdeck by pi/setup-deck.sh and served
by the deck-api systemd unit. It is a *wrapper*: it never reimplements the
boot logic, it shells out to the same /usr/local/bin/flightsim-boot.sh that
fauxmo has always called, and it learns what that script is doing by
following its journald output. flightsim-boot.sh is unmodified.

    GET  /api/state              current state as JSON
    GET  /api/fleet              the machines, what each is for, whether it
                                 answers and what it reports: a reading, for
                                 something that may look and not touch. Opens
                                 to DECK_READ_TOKEN as well, which opens
                                 nothing else.
    GET  /api/events             the same, streamed (SSE) on every change
    POST /api/boot               {"target":"windows|linux","intent":"..."}
    POST /api/launch             target already up — fire greeting + profile
    POST /api/abort              kill an in-flight run
    POST /api/hook/greeting      Windows reports the greeting spoke (phase 6/7)
    GET  /api/douglas            Douglas's metrics, voice state and transcript
    POST /api/douglas/ptt        {"action":"start|stop|cancel"} push-to-talk
    POST /api/douglas/say        {"text":"...","via":"deck|phone"} typed turn;
                                 "image":"<base64>" adds a picture to look at
    POST /api/douglas/hear       {"audio":"<base64 16-bit mono PCM>","rate":16000}
                                 a turn recorded on the phone's own microphone
    GET  /api/douglas/picture    ?id=<a transcript line's "image">: a picture he drew
    GET  /api/douglas/speech     ?id=<the turn's id>: his reply as a WAV, for
                                 the phone to play
    GET  /api/douglas/transcript ?before=<ts>&limit=<n>: older lines of his
                                 conversation, for a screen scrolled up
    GET  /api/douglas/memory     ?op=list|get|events|stats: what he remembers,
                                 and the record of what he did
    POST /api/douglas/memory     {"op":"correct|add|delete|restore|purge", ...}
                                 the owner puts a memory right or takes it away
    GET  /                       the deck UI (the 2560x720 panel)
    GET  /m                      the same deck for a phone, over the tailnet

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

import hmac
import json
import math
import os
import re
import shlex
import shutil
import signal
import sys
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

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
# Empty = the Windows exporter on whichever route Windows answered on
# (win_metrics_url); set it only to point somewhere else entirely.
WIN_METRICS_URL = cfg("WIN_METRICS_URL", "")
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
# The Windows boot's own address on that same cable (windows/setup.ps1
# -DirectIP). The workstation's LAN port has no cable and never will, so
# without this Windows is reachable only through a router port it is not
# plugged into. Its own address, not LINUX_PROBE_IP: that one answering is
# how this Pi knows Linux is up. Empty = Windows by WS_LAN only.
WIN_DIRECT_IP = cfg("WIN_DIRECT_IP", "")
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
# Douglas — the AI. A headless GEEKOM A9 Mega that does nothing but host the
# assistants, reached by its Tailscale MagicDNS name so the same address works
# from this Pi, the phone and DougVoice alike (this Pi must be on the tailnet
# with MagicDNS accepted, or set the full douglas.<tailnet>.ts.net here).
# Ports are the services it is built to run: llama.cpp's OpenAI-compatible
# server, the Open WebUI chat front end, and Wyoming speech-to-text and
# text-to-speech for DougVoice. Each is observed as an open door, nothing more.
DOUGLAS_HOST = cfg("DOUGLAS_HOST", "douglas")
DOUGLAS_LLM_PORT = int(cfg("DOUGLAS_LLM_PORT", "8081"))
DOUGLAS_CHAT_PORT = int(cfg("DOUGLAS_CHAT_PORT", "8080"))
DOUGLAS_STT_PORT = int(cfg("DOUGLAS_STT_PORT", "10300"))
DOUGLAS_TTS_PORT = int(cfg("DOUGLAS_TTS_PORT", "10200"))
# DougVoice — Douglas's ears and mouth: a Pi Zero 2 W under a Whisplay HAT.
# It hears and speaks; every thought happens on Douglas. Liveness, ssh, and
# the Wyoming satellite port it listens for Douglas on.
VOICE_HOST = cfg("VOICE_HOST", "dougvoice")
VOICE_SAT_PORT = int(cfg("VOICE_SAT_PORT", "10700"))
# douglas/douglas-agent.py on Douglas: one JSON for the DOUGLAS surface
# (host, GPU and model-server metrics, voice state, recent transcript) and
# the push-to-talk relay. Token-gated both ways — the transcript is private.
DOUGLAS_AGENT_PORT = int(cfg("DOUGLAS_AGENT_PORT", "9111"))
DOUGLAS_AGENT_TOKEN = cfg("DOUGLAS_AGENT_TOKEN", "")
DOUGLAS_PICTURE_MAX = 11_000_000      # base64 characters: about 8 MB of picture
DOUGLAS_AUDIO_MAX = 4_500_000         # base64 characters: a recording of half a minute
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
# A second token, for something that should see the fleet and touch nothing
# (Douglas). It opens GET /api/fleet and no other route: not the state the
# panel is drawn from, not Douglas's conversation or memory, and none of the
# routes that boot, shut down or change anything. Set to the deck's own
# token it would be the deck's own token, so then it counts as unset.
DECK_READ_TOKEN = cfg("DECK_READ_TOKEN", "")
if DECK_READ_TOKEN and DECK_READ_TOKEN == DECK_TOKEN:
    print("deck-api: DECK_READ_TOKEN is the same as DECK_TOKEN - ignored, nothing "
          "opens to it until it is a token of its own", file=sys.stderr, flush=True)
    DECK_READ_TOKEN = ""
# This Pi's own services worth knowing the health of, by unit name.
FLEET_UNITS = tuple(cfg("FLEET_UNITS", "deck-api fauxmo k3s tailscaled").split())
# Where the phone page is on HTTPS, if pi/mobile-https.sh has put it there:
# https://<this machine>.<tailnet>.ts.net. The plain page offers it, because
# only an HTTPS page is lent a phone's microphone.
DECK_HTTPS_URL = cfg("DECK_HTTPS_URL", "").rstrip("/")
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
# douglas is the AI's own surface: its metrics, the conversation, and a
# push-to-talk that asks DougVoice to listen (this panel has no microphone).
SURFACES = ("deck", "evals", "nav", "sim", "displays", "audio", "douglas", "idle")
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
            # `since_seen`: whether that change was seen happening. The first
            # look after this program starts is a change too (from not
            # knowing), and "running Linux since then" would be a lie.
            "workstation": {"os": "unknown", "since": None, "since_seen": False,
                            "last_alive": None, "ip": WS_LAN, "agent": False},
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
            # Douglas: liveness, its service doors, and what the model
            # server says it is doing. `model.state` is absent while Douglas
            # itself cannot be reached — absent is not "stopped".
            "douglas": {"up": False, "ssh": False, "chat": False,
                        "stt": False, "tts": False,
                        "model": {"state": "absent", "name": None},
                        "host": DOUGLAS_HOST, "last_alive": None},
            "voice": {"up": False, "ssh": False, "satellite": False,
                      "host": VOICE_HOST, "last_alive": None},
            # Which copy of this file is actually running. `git pull` updates
            # the checkout; only setup-deck.sh updates /opt/flightdeck, and
            # confusing the two costs an afternoon.
            "version": BUILD,
            "https_url": DECK_HTTPS_URL or None,
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


def douglas_model() -> dict:
    """What Douglas's llama.cpp server says, right now.

    llama.cpp's /health answers 503 while the weights are still loading and
    200 once it serves, so loading is observed rather than guessed. The model
    name is the server's own /v1/models id, trimmed to a readable stem."""
    base = f"http://{DOUGLAS_HOST}:{DOUGLAS_LLM_PORT}"
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as r:
            ok = 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        return {"state": "loading" if e.code == 503 else "stopped", "name": None}
    except (urllib.error.URLError, OSError, ValueError):
        return {"state": "stopped", "name": None}
    if not ok:
        return {"state": "stopped", "name": None}
    name = None
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=3) as r:
            data = json.loads(r.read(65536)).get("data") or []
        if data and data[0].get("id"):
            name = os.path.basename(str(data[0]["id"]))
            if name.lower().endswith(".gguf"):
                name = name[:-5]
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        pass
    return {"state": "running", "name": name}


# How many notices are open on Douglas, for the panel's top bar: asked for
# with the probe, at most three times a minute. Only the count: the notices
# themselves are read by the owner's screens through the memory's route.
NOTICES_EVERY_S = 20.0
_notices_kept: dict = {"at": -1e9, "value": None}


def douglas_notices() -> dict | None:
    """How many notices the watch on Douglas has open, how many warnings and
    worse among them nobody has marked as seen, and whether one of those is
    critical. None when that could not be read, which is not none open."""
    if time.monotonic() - _notices_kept["at"] < NOTICES_EVERY_S:
        return _notices_kept["value"]
    value = None
    if DOUGLAS_AGENT_TOKEN:
        got = _ws_json(f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/memory?token={DOUGLAS_AGENT_TOKEN}"
                       "&op=notices&limit=50", timeout=3)
        if isinstance(got, dict) and got.get("ok"):
            rows = [n for n in got.get("open") or [] if isinstance(n, dict)]
            new = [n for n in rows if not n.get("acked") and n.get("severity") in ("warning", "critical")]
            value = {"open": len(rows), "new": len(new),
                     "worst": ("critical" if any(n.get("severity") == "critical" for n in new)
                               else "warning" if new else None)}
    _notices_kept.update(at=time.monotonic(), value=value)
    return value


def fetch_douglas() -> dict:
    """Douglas's card: is the AI box up, which of its doors are open, and
    what the model server reports. Always-on hardware, so down is a fault
    worth showing — unlike the laptop's AWAY."""
    up = pingable(DOUGLAS_HOST)
    return {"up": up,
            "ssh": up and tcp_open(DOUGLAS_HOST, 22),
            "chat": up and tcp_open(DOUGLAS_HOST, DOUGLAS_CHAT_PORT),
            "stt": up and tcp_open(DOUGLAS_HOST, DOUGLAS_STT_PORT),
            "tts": up and tcp_open(DOUGLAS_HOST, DOUGLAS_TTS_PORT),
            "model": douglas_model() if up else {"state": "absent", "name": None},
            "notices": douglas_notices() if up else None,
            "host": DOUGLAS_HOST,
            "last_alive": time.time() if up else
                DECK.state.get("douglas", {}).get("last_alive")}


# Douglas watches the other machines and himself, and keeps what he notices
# on his own disk. He cannot notice that he is gone, and a notice kept on a
# machine that is down reaches nobody. So the Pi says that much itself, in
# the events it already shows: when Douglas or his ears stop answering, and
# when they are back. After three looks in a row, so that one lost ping is
# not a machine gone; nothing is kept beyond the event.
_quiet: dict[str, dict] = {}


def gone_or_back(name: str, up: bool) -> None:
    k = _quiet.setdefault(name, {"misses": 0, "gone": False, "seen": False})
    if up:
        if k["gone"]:
            DECK.event("probe", f"{name}: answering again")
        k.update(misses=0, gone=False, seen=True)
    elif k["seen"] and not k["gone"]:             # only of a machine that has been seen up since this started
        k["misses"] += 1
        if k["misses"] >= 3:
            k["gone"] = True
            DECK.event("probe", f"{name}: not answering", "warn")


def who_is_gone(douglas: dict, voice: dict) -> None:
    """One look at who should answer: Douglas, his ears, and the watch on
    Douglas. The watch is his agent: with it gone and the machine up, no
    notice is raised and none can be read, and nothing else would say so.
    Of a machine that is down, nothing more is said than that."""
    gone_or_back("DOUGLAS", douglas["up"])
    if douglas["up"] and DOUGLAS_AGENT_TOKEN:
        gone_or_back("the watch on DOUGLAS", tcp_open(DOUGLAS_HOST, DOUGLAS_AGENT_PORT))
    gone_or_back("DOUGVOICE", voice["up"])


def fetch_voice() -> dict:
    """DougVoice's card: liveness, ssh, and whether its satellite is
    listening for Douglas."""
    up = pingable(VOICE_HOST)
    return {"up": up,
            "ssh": up and tcp_open(VOICE_HOST, 22),
            "satellite": up and tcp_open(VOICE_HOST, VOICE_SAT_PORT),
            "host": VOICE_HOST,
            "last_alive": time.time() if up else
                DECK.state.get("voice", {}).get("last_alive")}


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
                return own, f"windows {win_host()}:{SIM_PORT}/metrics"
        url, src = win_metrics_url(), "windows"
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
            and url in (win_metrics_url(), LINUX_METRICS_URL):
        _bad_metrics.add(url)
        DECK.event("deck-api", f"{url} exposes no GPU metrics this panel recognises",
                   "warn")
    return values


def win_metrics_url() -> str:
    return WIN_METRICS_URL or f"http://{win_host()}:9106/metrics"


def _sim_url(path: str) -> str:
    return f"http://{win_host()}:{SIM_PORT}{path}?token={quote(SIM_TOKEN)}"


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
        url = (f"http://{linux_host()}:{LINUX_MEDIA_PORT}/audio"
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
        return (f"http://{linux_host()}:{LINUX_MEDIA_PORT}{path}"
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
        url = (f"http://{linux_host()}:{LINUX_MEDIA_PORT}/monitor"
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


# Where the Linux boot's agents are dialled: the address it last answered a
# probe on. That is the LAN address from boot.env wherever nothing has gone
# wrong, and the point-to-point link when only that answers.
#
# linux_seen() learned the second route the night the LAN address moved, and
# the panel went back to saying "linux". Nothing else learned it: the screens,
# the track, the bars and the off button went on dialling WS_LAN. So with the
# workstation's LAN cable out, the panel showed a running Linux box and NO
# LINK on every surface that has to ask that box something, which at the desk
# read as a fault in the graphics driver.
_linux_at = [WS_LAN]


def linux_host() -> str:
    """The address to dial the Linux boot's agents on."""
    return _linux_at[0]


def linux_seen() -> bool:
    """Linux, by any route this Pi has.

    Three, because the first two both depend on the workstation's LAN address
    being what boot.env says it is - and a machine that is powered off cannot
    defend its DHCP lease, so that assumption has already failed once. The
    point-to-point link is immune: it exists only under the Linux boot and no
    router hands out its addresses.

    The route that answered is the one its agents are dialled on until the
    next probe (linux_host). The LAN address is asked first every time, so it
    is back in use the cycle after it answers again.
    """
    if http_ok(f"http://{WS_LAN}:{LINUX_PORT}/") or tcp_ok(WS_LAN, LINUX_AGENT_PORT):
        _linux_at[0] = WS_LAN
        return True
    if not LINUX_PROBE_IP:
        return False
    if tcp_ok(LINUX_PROBE_IP, 22) or pingable(LINUX_PROBE_IP):
        _linux_at[0] = LINUX_PROBE_IP
        return True
    return False


# Where the Windows boot's agents are dialled, on the same terms as
# linux_host(): the LAN address while that answers, the direct cable
# (WIN_DIRECT_IP) when only that does. The LAN port has no cable, so on this
# deployment that is the direct cable every cycle.
_win_at = [WS_LAN]


def win_host() -> str:
    """The address to dial the Windows boot's agents on."""
    return _win_at[0]


def windows_seen() -> tuple[bool, bool]:
    """(Windows is up, its boot agent answered), by any route this Pi has.

    The route that answered is the one win_host() returns until the next
    probe. The LAN address is asked first every time.

    The cable gets short timeouts: a live host on it answers in milliseconds,
    and under Linux nothing holds that address, so full ones would add five
    seconds of waiting to every probe cycle."""
    for host, t_tcp, t_http in ((WS_LAN, 2.0, 3.0), (WIN_DIRECT_IP, 0.5, 1.0)):
        if not host:
            continue
        agent = tcp_ok(host, AGENT_PORT, timeout=t_tcp)
        if agent or http_ok(f"http://{host}:{WIN_PORT}/", timeout=t_http):
            _win_at[0] = host
            return True, agent
    return False, False


def poll_once(prev_os):
    """One probe cycle. Returns the OS it saw, for the next cycle to diff."""
    win, agent = windows_seen()
    lin = linux_seen() if not win else False
    alive = (win or lin or pingable(WS_LAN)
             or (bool(WIN_DIRECT_IP) and pingable(WIN_DIRECT_IP)))

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
    douglas = fetch_douglas()
    voice = fetch_voice()
    who_is_gone(douglas, voice)

    def apply(s):
        w = s["workstation"]
        if changed:
            w["since"] = time.time()
            w["since_seen"] = prev_os is not None
        if alive:
            w["last_alive"] = time.time()
        w["os"] = os_now
        w["agent"] = agent
        # The address actually in use, so the panel shows the route it dials.
        w["ip"] = win_host() if win else linux_host() if lin else WS_LAN
        s["sim"] = sim_link
        s["mini"] = mini
        s["mobile"] = mobile
        s["douglas"] = douglas
        s["voice"] = voice
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
        url = f"http://{win_host()}:{AGENT_PORT}/shutdown?token={AGENT_TOKEN}"
    elif os_now == "linux":
        if not LINUX_AGENT_TOKEN:
            return False, "LINUX_AGENT_TOKEN unset in /etc/flightsim/boot.env"
        url = f"http://{linux_host()}:{LINUX_AGENT_PORT}/shutdown?token={LINUX_AGENT_TOKEN}"
    else:
        return False, f"nothing to shut down: workstation is {os_now}"
    if http_ok(url, timeout=5):
        DECK.event("deck", f"shutdown requested via the {os_now} agent")
        return True, "powering off"
    return False, "the boot agent did not answer"


def fire_launch() -> tuple[bool, str]:
    if not AGENT_TOKEN:
        return False, "WIN_AGENT_TOKEN unset in /etc/flightsim/boot.env"
    url = f"http://{win_host()}:{AGENT_PORT}/launch?token={AGENT_TOKEN}"
    if http_ok(url, timeout=5):
        DECK.event("deck", "launch requested via boot-agent")
        return True, "launching"
    return False, "boot-agent did not answer"


def douglas_status() -> dict:
    """The DOUGLAS surface's frame: douglas-agent's /status, passed through.

    `agent: false` when it cannot be read — the surface then shows what the
    fleet probe already knows and says plainly that the detail is missing,
    rather than drawing zeros that look like an idle machine."""
    if not DOUGLAS_AGENT_TOKEN:
        return {"agent": False, "why": "DOUGLAS_AGENT_TOKEN unset in boot.env"}
    url = (f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/status"
           f"?token={DOUGLAS_AGENT_TOKEN}")
    got = _ws_json(url, timeout=3)
    if not isinstance(got, dict):
        return {"agent": False, "why": "douglas-agent did not answer"}
    got["agent"] = True
    return got


PICTURE_ID = re.compile(r"p-[0-9a-f]{12}")


def douglas_picture(pid: str) -> tuple[int, bytes | dict, str]:
    """A picture Douglas drew, by the id its line in his transcript carries
    ("image"), for a screen to show: the bytes and their type."""
    if not PICTURE_ID.fullmatch(pid or ""):
        return 400, {"ok": False, "message": "no such picture"}, ""
    if not DOUGLAS_AGENT_TOKEN:
        return 502, {"ok": False, "message": "DOUGLAS_AGENT_TOKEN unset in boot.env"}, ""
    url = (f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/picture"
           f"?token={DOUGLAS_AGENT_TOKEN}&id={quote(pid)}")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            kind, body = r.headers.get_content_type(), r.read(16_000_000)
    except urllib.error.HTTPError as e:
        return (404 if e.code == 404 else 502), {"ok": False, "message": "no such picture"}, ""
    except (urllib.error.URLError, OSError):
        return 502, {"ok": False, "message": "douglas-agent did not answer"}, ""
    if kind not in ("image/jpeg", "image/png"):
        return 502, {"ok": False, "message": "that is not a picture"}, ""
    return 200, body, kind


def douglas_ptt(action: str) -> tuple[bool, str]:
    """Press or release the push-to-talk. The panel has no microphone: this
    asks Douglas to have DougVoice listen, exactly as its own button does."""
    if action not in ("start", "stop", "cancel"):
        return False, "action must be start, stop or cancel"
    if not DOUGLAS_AGENT_TOKEN:
        return False, "DOUGLAS_AGENT_TOKEN unset in boot.env"
    url = (f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/ptt"
           f"?token={DOUGLAS_AGENT_TOKEN}")
    got = _ws_json(url, {"action": action, "via": "deck"}, timeout=4)
    if not isinstance(got, dict):
        return False, "douglas-agent did not answer"
    return bool(got.get("ok")), str(got.get("message") or "")


def douglas_owner(what: str, body: dict) -> tuple[int, dict]:
    """Two things only the owner's screens ask of Douglas, each passed to his
    agent in the one shape it has. `approval`: the answer to a change of his
    that is waiting for a yes, by the number of what was read back.
    `switch`: one of his tools, on or off."""
    if not DOUGLAS_AGENT_TOKEN:
        return 502, {"ok": False, "message": "DOUGLAS_AGENT_TOKEN unset in boot.env"}
    if what == "approval":
        pid, yes = body.get("id"), body.get("yes")
        if not isinstance(pid, str) or not pid or not isinstance(yes, bool):
            return 400, {"ok": False, "message": "an answer is the number of what is waiting, and yes: true or false"}
        ask = {"id": pid[:32], "yes": yes, "via": "phone" if body.get("via") == "phone" else "deck"}
    else:
        tool, on = body.get("tool"), body.get("on")
        if not isinstance(tool, str) or not re.fullmatch(r"[a-z_]{1,24}", tool) or not isinstance(on, bool):
            return 400, {"ok": False, "message": "a switch is a tool, and on: true or false"}
        ask = {"tool": tool, "on": on}
    got = _ws_json(f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/{what}?token={DOUGLAS_AGENT_TOKEN}", ask, timeout=8)
    if not isinstance(got, dict):
        return 502, {"ok": False, "message": "douglas-agent did not answer"}
    return 200, got


# A phone on a poor link sends a question again when the first copy got no
# answer, and sometimes that copy had arrived and only the reply was lost. So
# the page gives each question an id, and one Douglas has already taken is
# acknowledged again without being asked twice.
SAID_KEEP_S = 900
SAID_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_said: dict[str, float] = {}                    # id -> when Douglas took it
_saying: dict[str, tuple[threading.Lock, float]] = {}
_said_guard = threading.Lock()


def douglas_say(text: str, via: str, image: object = None,
                ask_id: object = None) -> tuple[bool, str]:
    """A typed turn for Douglas, from the panel or the phone, with or without
    a picture (base64, as the phone page sends it). Answered asynchronously:
    the reply arrives in /api/douglas's transcript. With an id, the same
    question sent twice reaches Douglas once."""
    text = " ".join(str(text or "").split())
    image = image if isinstance(image, str) else ""
    if not text and not image:
        return False, "nothing to say"
    if len(image) > DOUGLAS_PICTURE_MAX:
        return False, "that picture is too big"
    if not DOUGLAS_AGENT_TOKEN:
        return False, "DOUGLAS_AGENT_TOKEN unset in boot.env"
    url = (f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/say"
           f"?token={DOUGLAS_AGENT_TOKEN}")
    payload = {"text": text[:2000], "via": via if via in ("deck", "phone") else "deck"}
    if image:
        payload["image"] = image

    def send() -> tuple[bool, str]:
        got = _ws_json(url, payload, timeout=20 if image else 5)
        if not isinstance(got, dict):
            return False, "douglas-agent did not answer"
        return bool(got.get("ok")), str(got.get("message") or "")

    return _once(ask_id, send)


def douglas_hear(audio: object, rate: object, via: str,
                 ask_id: object = None) -> tuple[bool, str]:
    """A turn recorded on a phone's own microphone (base64 16-bit mono PCM).
    Douglas transcribes and answers it; the reply lands in the transcript and
    its audio waits on Douglas to be collected with douglas_speech()."""
    audio = audio if isinstance(audio, str) else ""
    if not audio:
        return False, "nothing recorded"
    if len(audio) > DOUGLAS_AUDIO_MAX:
        return False, "that recording is too long"
    if not DOUGLAS_AGENT_TOKEN:
        return False, "DOUGLAS_AGENT_TOKEN unset in boot.env"
    try:
        rate = int(rate or 16000)
    except (TypeError, ValueError):
        rate = 16000
    url = (f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/hear"
           f"?token={DOUGLAS_AGENT_TOKEN}")
    payload = {"audio": audio, "rate": rate, "via": via if via in ("deck", "phone") else "phone",
               "id": ask_id if isinstance(ask_id, str) and SAID_ID.fullmatch(ask_id) else ""}

    def send() -> tuple[bool, str]:
        got = _ws_json(url, payload, timeout=25)
        if not isinstance(got, dict):
            return False, "douglas-agent did not answer"
        return bool(got.get("ok")), str(got.get("message") or "")

    return _once(ask_id, send)


def douglas_speech(ask_id: str) -> tuple[int, bytes | dict]:
    """The reply to a recorded turn, for the phone that sent it to play: a
    WAV once Douglas has it ready, and his word on where it stands until
    then ({"state": "thinking" | "nothing" | "failed" | "unknown"})."""
    if not SAID_ID.fullmatch(ask_id or ""):
        return 400, {"ok": False, "state": "unknown"}
    if not DOUGLAS_AGENT_TOKEN:
        return 502, {"ok": False, "state": "failed",
                     "message": "DOUGLAS_AGENT_TOKEN unset in boot.env"}
    url = (f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/speech"
           f"?token={DOUGLAS_AGENT_TOKEN}&id={quote(ask_id)}")
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            body = r.read(8_000_000)
            if r.headers.get_content_type() == "audio/wav":
                return 200, body
            return r.status, json.loads(body)
    except urllib.error.HTTPError as e:         # he answered: an id he does not know
        try:
            return e.code, json.loads(e.read(4096))
        except (ValueError, OSError):
            return e.code, {"ok": False, "state": "unknown"}
    except (urllib.error.URLError, OSError, ValueError):
        return 502, {"ok": False, "state": "failed", "message": "douglas-agent did not answer"}


def _once(ask_id: object, send) -> tuple[bool, str]:
    """Send a question to Douglas unless one with this id has already been
    taken. Without a proper id every copy is sent."""
    if not (isinstance(ask_id, str) and SAID_ID.fullmatch(ask_id)):
        return send()
    now = time.time()
    with _said_guard:
        for old in [k for k, when in _said.items() if now - when > SAID_KEEP_S]:
            del _said[old]
        for old in [k for k, (_, when) in _saying.items() if now - when > SAID_KEEP_S]:
            del _saying[old]
        lock = _saying.setdefault(ask_id, (threading.Lock(), now))[0]
    # A second copy that arrives while the first is still with Douglas waits
    # here, then finds the first one's outcome rather than racing it.
    with lock:
        if ask_id in _said:
            return True, "already sent"
        ok, msg = send()
        if ok:
            _said[ask_id] = time.time()
        return ok, msg


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
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
}


# What Douglas remembers is kept on Douglas, and his agent has the owner's
# door to it. This passes one question on and hands the answer back. Looking
# and changing are separate lists, so that a GET changes nothing whatever it
# asks; and only these names travel, each as one plain value.
MEMORY_LOOKS = ("list", "get", "events", "stats", "notices")
MEMORY_CHANGES = ("correct", "add", "delete", "restore", "purge", "notice_ack", "notice_prefs")
MEMORY_FIELDS = ("id", "kind", "status", "q", "limit", "offset", "text", "subject", "confidence", "done",
                 "severity", "tell", "aloud", "quiet_from", "quiet_to")


def douglas_memory(op: object, fields: dict, change: bool) -> tuple[int, dict]:
    """One question about what Douglas remembers, from the owner's screen:
    to read it (change=False) or to put it right (change=True)."""
    if not isinstance(op, str) or op not in (MEMORY_CHANGES if change else MEMORY_LOOKS):
        return 400, {"ok": False, "text": "That is not something to ask of his memory this way."}
    if not DOUGLAS_AGENT_TOKEN:
        return 502, {"ok": False, "text": "DOUGLAS_AGENT_TOKEN unset in boot.env"}
    ask = {k: fields[k] for k in MEMORY_FIELDS if isinstance(fields.get(k), (str, int, float))}
    ask["op"] = op
    url = f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/memory?token={DOUGLAS_AGENT_TOKEN}"
    got = _ws_json(url, ask, timeout=8) if change else _ws_json(url + "&" + urlencode(ask), timeout=8)
    if not isinstance(got, dict):
        return 502, {"ok": False, "text": "douglas-agent did not answer"}
    if change and op.startswith("notice"):
        _notices_kept["at"] = -1e9                # seen on a screen: the top bar's count is asked for afresh
    return 200, got


def douglas_transcript(before: object, limit: object) -> tuple[int, dict]:
    """Older lines of his conversation, for a screen scrolled up past what
    /api/douglas carries. Only numbers are passed on to the agent."""
    if not DOUGLAS_AGENT_TOKEN:
        return 502, {"ok": False, "text": "DOUGLAS_AGENT_TOKEN unset in boot.env"}
    ask = {}
    try:
        b = float(before)
        if math.isfinite(b):
            ask["before"] = repr(b)
    except (TypeError, ValueError):
        pass
    try:
        ask["limit"] = str(max(1, min(100, int(limit))))
    except (TypeError, ValueError):
        pass
    url = f"http://{DOUGLAS_HOST}:{DOUGLAS_AGENT_PORT}/transcript?token={DOUGLAS_AGENT_TOKEN}"
    # Read here rather than through _ws_json: a hundred long lines can be
    # more than the quarter megabyte it stops at.
    try:
        with urllib.request.urlopen(url + ("&" + urlencode(ask) if ask else ""), timeout=8) as r:
            got = json.loads(r.read(4_000_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        got = None
    if not isinstance(got, dict):
        return 502, {"ok": False, "text": "douglas-agent did not answer"}
    return 200, got


# ── the fleet, as a reading ───────────────────────────────────────────────
#
# What this Pi knows about the machines in the house, put together for a
# reader that may look and not touch: which machines there are, what each is
# for, whether it answered the last probe and when it last did, and whatever
# it reports about itself. It is made from the state the panel is drawn from,
# so nothing new is probed for it, plus two things read here when asked: how
# full this Pi's disk is and whether its own services are up.
#
# What is left out on purpose: addresses, anything that can be pressed, the
# flight plan, and everything of Douglas's but his machine's health. His
# conversation and his memory are not fleet state, and this route must never
# be a way round to them.

FLEET_ROLES = {
    "node1": ("DOUG PRIME", "Raspberry Pi 4: runs Mission Control and draws the wall panel"),
    "workstation": ("DOUG DUALBOOT", "the dual-boot workstation: Windows for the flight simulator "
                                     "and games, Linux for work"),
    "douglas": ("DOUGLAS", "the inference machine: the language model, the voice pipeline and "
                           "the workshop"),
    "dougvoice": ("DOUGVOICE", "Raspberry Pi Zero: the microphone and speaker in the room"),
    "mini": ("MINIDOUG4", "Mac mini"),
    "mobile": ("DOUGMOBILE", "MacBook: comes and goes"),
}
# What is taken out of an event's text before a reader sees it: where a
# machine is (an address of either kind, a hardware address, a name on the
# tailnet) and anything given as a credential, however it is put.
AN_ADDRESS = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b"                                   # 192.0.2.7
    r"|\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"                          # a hardware address
    r"|(?<![\w:])(?=[0-9a-f:]*[0-9a-f])[0-9a-f:]*::[0-9a-f:]*(?![\w:])"  # fe80::1, 2001:db8::7
    r"|(?<![\w:])(?:[0-9a-f]{1,4}:){5,7}[0-9a-f]{1,4}(?![\w:])"         # the long form (a time has two colons)
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:ts\.net|local|lan|home|internal)\b", re.I)
A_TOKEN = re.compile(
    r"\b([\w-]*(?:token|key|secret|password|passwd|authorization|bearer|credential)[\w-]*)"
    r"(\s*(?:=|:|\bis\b)\s*|\s+)(?:bearer\s+)?[^\s&,;\"']{3,}", re.I)
_units_kept: dict = {"at": 0.0, "value": None, "asked": False}
_units_lock = threading.Lock()
FLEET_KEEP_S = 2.0
_fleet_kept: dict = {"at": 0.0, "value": None}
_fleet_lock = threading.Lock()


def own_units() -> list[dict] | None:
    """Whether this Pi's own services are up, since when, and how often
    systemd has had to start them again. Asked of systemd with a fixed
    command, at most every fifteen seconds; None when it cannot be asked."""
    with _units_lock:
        if _units_kept["asked"] and time.monotonic() - _units_kept["at"] < 15:
            return _units_kept["value"]            # "could not be asked" is kept as long as an answer is
        _units_kept.update(at=time.monotonic(), asked=True, value=None)
        try:
            ran = subprocess.run(
                ["systemctl", "show", *[u + ".service" for u in FLEET_UNITS], "-p",
                 "Id,LoadState,ActiveState,SubState,NRestarts,ActiveEnterTimestampMonotonic,Result,UnitFileState"],
                capture_output=True, text=True, timeout=4)
            up = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None
        out = ran.stdout
        if ran.returncode and not out.strip():
            return None                            # systemd would not say: not known, which is not "none"
        units, now = [], time.time()
        for block in out.strip().split("\n\n"):
            p = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if not p.get("Id") or p.get("LoadState") != "loaded":
                continue                           # not installed here: not a service of this Pi
            try:
                since = now - (up - int(p.get("ActiveEnterTimestampMonotonic") or 0) / 1e6)
                restarts = int(p.get("NRestarts") or 0)
            except ValueError:
                since, restarts = None, None
            units.append({"unit": p["Id"].removesuffix(".service"), "state": p.get("ActiveState"),
                          "sub": p.get("SubState"), "restarts": restarts, "result": p.get("Result"),
                          "enabled": p.get("UnitFileState") or None,
                          "since": round(since) if since and p.get("ActiveState") == "active" else None})
        _units_kept.update(at=time.monotonic(), value=units)
        return units


def own_disk() -> dict | None:
    try:
        d = shutil.disk_usage("/")
    except OSError:
        return None
    return {"path": "/", "total_bytes": d.total, "used_bytes": d.used, "free_bytes": d.free}


def fleet() -> dict:
    """The machines, for a reader. Every reading has when it was taken:
    `seen` is the last time a machine answered a probe, `as_of` the moment
    its numbers were read."""
    # Made at most every two seconds: each making asks Douglas's agent for
    # his machine's account, and a reader asking in a loop should not be
    # able to keep two machines busy with it.
    with _fleet_lock:
        if _fleet_kept["value"] is not None and time.monotonic() - _fleet_kept["at"] < FLEET_KEEP_S:
            return _fleet_kept["value"]
        return _fleet_made()


def _fleet_made() -> dict:
    s, now = DECK.snapshot(), time.time()
    ws, tel, d, v = s["workstation"], s["telemetry"], s["douglas"], s["voice"]
    boot = s["boot"]

    def machine(key: str, up: bool, seen, **more) -> dict:
        name, role = FLEET_ROLES[key]
        return dict({"id": key, "name": name, "role": role, "up": bool(up), "seen": seen}, **more)

    ws_up = ws["os"] in ("windows", "linux")
    machines = [
        machine("node1", True, now, as_of=now,
                reports=dict({k: s["pi"].get(k) for k in ("temp_c", "load", "mem_pct", "cpu_pct")},
                             uptime_s=s["pi"].get("uptime"), disk=own_disk()),
                services=own_units()),
        # How long it has been in this state is known only if the change was
        # seen. Otherwise all there is to say is how long it has been watched.
        machine("workstation", ws_up, ws["last_alive"], state=ws["os"],
                since=ws["since"] if ws.get("since_seen") else None,
                watched_since=None if ws.get("since_seen") else ws["since"],
                as_of=tel["ts"] if ws_up else None, reports=tel["ws"] if ws_up else {},
                booting=({"to": boot["target"], "phase": boot["phase_name"]} if boot["in_flight"] else None)),
        machine("douglas", d["up"], d["last_alive"], model=d.get("model"),
                doors={"chat page": d["chat"], "speech to text": d["stt"], "text to speech": d["tts"]}),
        machine("dougvoice", v["up"], v["last_alive"], doors={"voice satellite": v["satellite"]}),
        machine("mini", s["mini"]["up"], s["mini"]["last_alive"],
                doors={"ssh": s["mini"]["ssh"], "screen sharing": s["mini"]["screen"]}),
        machine("mobile", s["mobile"]["up"], s["mobile"]["last_alive"],
                doors={"ssh": s["mobile"]["ssh"], "screen sharing": s["mobile"]["screen"]}),
    ]
    if d["up"] and DOUGLAS_AGENT_TOKEN:
        # His machine's own account of itself, where it gives one. Only its
        # health: never what was said to him, and never what he remembers.
        got = douglas_status()
        if got.get("agent"):
            machines[2].update(as_of=got.get("ts"), reports={
                k: got[k] for k in ("host", "gpu", "llm", "disks") if got.get(k) is not None},
                services=got.get("units"))
    events = [{"ts": e.get("ts"), "level": e.get("level"), "source": e.get("source"),
               "text": A_TOKEN.sub(r"\1=...", AN_ADDRESS.sub("(an address)", str(e.get("text") or "")))[:200]}
              for e in s["events"][:20]]
    view = {"ts": now, "machines": machines, "events": events,
            "deck": {"version": s["version"].get("mtime"), "ui": s["version"].get("ui"),
                     "panel_showing": s["surface"]["active"]},
            "not_reported": "DOUGVOICE, MINIDOUG4 and DOUGMOBILE say only whether they answer; "
                            "the workstation reports only while an exporter on it is running."}
    _fleet_kept.update(at=time.monotonic(), value=view)
    return view


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deck-api/0.1"

    def log_message(self, fmt, *args):  # noqa: A003 - quieter journal
        pass

    def handle(self):
        # A phone that goes to sleep resets the connections it was keeping
        # open between requests. That is how a visit ends, not an error, and
        # a traceback for each one buries the journal lines that matter.
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    # -- helpers ------------------------------------------------------------

    def _proxied(self) -> str:
        """Who a local proxy says this request is really from, or "". The
        phone page is put on HTTPS by `tailscale serve`, which forwards to
        this port: every one of those requests arrives from 127.0.0.1, and
        says in its headers whose it is."""
        for h in ("X-Forwarded-For", "Tailscale-User-Login", "X-Real-IP", "Forwarded"):
            v = self.headers.get(h)
            if v:
                return v.split(",")[0].strip()[:60] or "a proxy"
        return ""

    def _client(self) -> str:
        return self._proxied() or self.client_address[0]

    def _authorised(self, query: dict) -> bool:
        # The panel on this Pi needs no token: it is the only thing that
        # reaches this port from loopback of its own accord. A request a
        # proxy passed on is somebody else's, and needs one like any other.
        if self.client_address[0] in ("127.0.0.1", "::1") and not self._proxied():
            return True
        if not DECK_TOKEN:
            return False
        return (query.get("token") or [""])[0] == DECK_TOKEN

    def _may_read(self, query: dict) -> bool:
        """For the one route that is a reading and nothing else: whoever may
        use the deck, and whoever holds the read token. That token is looked
        at here and nowhere else, so no other route can open to it."""
        if self._authorised(query):
            return True
        given = (query.get("token") or [""])[0]
        return bool(DECK_READ_TOKEN) and hmac.compare_digest(given.encode(), DECK_READ_TOKEN.encode())

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The other end left before the answer: a phone that went to
            # sleep, a tab that closed. Nobody is there to tell, and a
            # twenty-line traceback in the journal for each one buries the
            # lines that matter.
            self.close_connection = True

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _drop_body(self, most: int = 2_000_000) -> None:
        """Read and discard a request body nobody is going to use."""
        try:
            left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            left = -1
        if not 0 <= left <= most:
            self.close_connection = True
            return
        try:
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        if left:
            self.close_connection = True

    def _note(self, what: str) -> None:
        """One journal line about a request: who, which route, what happened.
        Never the query (it carries the token) and never what was said."""
        print(f"http: {urlparse(self.path).path} from {self._client()}: "
              f"{' '.join(what.split())[:200]}", file=sys.stderr, flush=True)

    def _body(self) -> dict | None:
        """The request's JSON body: {} when there is none or it is not a JSON
        object, None when it did not arrive whole.

        A phone on a poor link can lose the connection part-way through a
        photo. Half a request is not a request: it is never acted on, and the
        journal says so, because otherwise it only looks empty and there is
        nothing to show for it."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
        except OSError as e:
            self._note(f"body did not arrive ({type(e).__name__})")
            return None
        except ValueError:
            return {}
        if len(raw) < n:
            self._note(f"body cut short, {len(raw)} of {n} bytes")
            return None
        try:
            got = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return got if isinstance(got, dict) else {}

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

        # Fast-poll family, like /api/sim and /api/media: read only while the
        # DOUGLAS surface is showing, and kept off the SSE state.
        if path == "/api/douglas":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, douglas_status())

        # The reply to a turn the phone recorded, for the phone to play.
        if path == "/api/douglas/speech":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            code, out = douglas_speech((query.get("id") or [""])[0])
            if isinstance(out, bytes):
                return self._send(200, out, "audio/wav")
            return self._json(code, out)

        if path == "/api/fleet":
            if not self._may_read(query):
                return self._json(403, {"error": "forbidden"})
            return self._json(200, fleet())

        # What he remembers and what he did, for the owner to read.
        if path == "/api/douglas/memory":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            fields = {k: v[0] for k, v in query.items()}
            code, out = douglas_memory(fields.get("op"), fields, change=False)
            return self._json(code, out)

        # Older lines of his conversation, for a screen scrolled up. The
        # owner's token, never the read token: his words are not fleet state.
        if path == "/api/douglas/transcript":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            code, out = douglas_transcript((query.get("before") or [""])[0],
                                           (query.get("limit") or [""])[0])
            return self._json(code, out)

        # A picture he drew, for the line of his it came with.
        if path == "/api/douglas/picture":
            if not self._authorised(query):
                return self._json(403, {"error": "forbidden"})
            code, out, kind = douglas_picture((query.get("id") or [""])[0])
            if isinstance(out, bytes):
                return self._send(200, out, kind)
            return self._json(code, out)

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
        # `retry` tells a caller the failure is the kind that passes: sending
        # the same thing again is worth doing.
        cut_short = {"ok": False, "retry": True, "message": "the request did not arrive whole"}
        if url.path == "/api/phase":
            body = self._body()
            if body is None:
                return self._json(400, cut_short)
            code, out = greeting_phase(body)
            return self._json(code, out)

        if not self._authorised(query):
            # Refused, and its body has to go somewhere first. Left unread on
            # a connection that is kept open - and a proxy in front keeps its
            # connections, and shares them between callers - it would be
            # taken for the start of the next request. A body of ordinary
            # size is read and dropped, so the caller hears its 403; anything
            # bigger, or cut short, ends the connection instead.
            self._drop_body()
            return self._json(403, {"error": "forbidden"})

        body = self._body()
        if body is None:
            return self._json(400, cut_short)
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

        if path == "/api/douglas/say":
            image = body.get("image")
            began = time.monotonic()
            ok, msg = douglas_say(body.get("text"), str(body.get("via") or "deck"), image,
                                  body.get("id"))
            # Sizes and the outcome, not the words. Whether a phone's photo
            # arrived whole, and how long Douglas took to accept it, is what
            # answers "the button went grey and nothing happened".
            kb = len(image) * 3 // 4096 if isinstance(image, str) else 0
            self._note(f"{len(str(body.get('text') or ''))} chars"
                       + (f" and a {kb} KB picture" if kb else "")
                       + (" accepted" if ok and msg != "already sent"
                          else " again: Douglas already has it" if ok else f" refused ({msg})")
                       + f" in {time.monotonic() - began:.1f} s")
            # Douglas not answering is the one refusal that passes by itself.
            retry = not ok and msg == "douglas-agent did not answer"
            return self._json(200 if ok else 502, {"ok": ok, "message": msg, "retry": retry})

        if path == "/api/douglas/hear":
            audio, began = body.get("audio"), time.monotonic()
            ok, msg = douglas_hear(audio, body.get("rate"), str(body.get("via") or "phone"),
                                   body.get("id"))
            try:
                secs = len(audio) * 3 / 4 / (int(body.get("rate") or 16000) * 2)
            except (TypeError, ValueError, ZeroDivisionError):
                secs = 0.0
            self._note(f"{secs:.1f} s recorded"
                       + (" accepted" if ok and msg != "already sent"
                          else " again: Douglas already has it" if ok else f" refused ({msg})")
                       + f" in {time.monotonic() - began:.1f} s")
            retry = not ok and msg == "douglas-agent did not answer"
            return self._json(200 if ok else 502, {"ok": ok, "message": msg, "retry": retry})

        if path == "/api/douglas/memory":
            op = body.get("op")
            code, out = douglas_memory(op, body, change=True)
            # Which change, to which memory, and whether it took. Never the
            # words: what he remembers is not for the journal.
            which = body.get("id")
            known = isinstance(op, str) and op in MEMORY_CHANGES
            self._note((op if known else "not a change")
                       + (f" of {'notice' if known and op.startswith('notice') else 'memory'} {which}"
                          if isinstance(which, int) else "")
                       + ("" if out.get("ok") else " refused"))
            return self._json(code, out)

        if path == "/api/douglas/approval":
            code, out = douglas_owner("approval", body)
            # That an answer was given and which way: never what it was to.
            self._note(("yes" if body.get("yes") is True else "no" if body.get("yes") is False else "not an answer")
                       + ("" if out.get("ok") else " refused"))
            return self._json(code, out)

        if path == "/api/douglas/switch":
            code, out = douglas_owner("switch", body)
            tool = body.get("tool")
            self._note((f"{tool[:24]} " if isinstance(tool, str) and re.fullmatch(r"[a-z_]{1,24}", tool) else "")
                       + ("on" if body.get("on") is True else "off" if body.get("on") is False else "not a switch")
                       + ("" if out.get("ok") else " refused"))
            return self._json(code, out)

        if path == "/api/douglas/ptt":
            action = str(body.get("action") or "")
            ok, msg = douglas_ptt(action)
            self._note(f"{action[:12] or 'no action'}" + ("" if ok else f" refused ({msg})"))
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

        # One line per stream, not per frame: enough to answer "did the kiosk
        # come back after a restart, and did it get frames" from the journal.
        print(f"sse: open {self._client()}:{self.client_address[1]}",
              file=sys.stderr, flush=True)
        seen = -1
        sent = 0
        try:
            while True:
                seen, snap = DECK.wait(seen, timeout=15)
                payload = json.dumps(snap)
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                sent += 1
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # kiosk reloaded or navigated away
        finally:
            print(f"sse: closed {self._client()}:{self.client_address[1]}"
                  f" after {sent} frame(s)", file=sys.stderr, flush=True)

    # -- static -------------------------------------------------------------

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        # The phone page: /m is a directory with its own index. Served like
        # every other static file — no token — because it holds no state;
        # every request it makes carries the token the phone was given.
        if rel in ("m", "m/"):
            rel = "m/index.html"
        target = (UI_DIR / rel).resolve()
        try:
            target.relative_to(UI_DIR.resolve())
        except ValueError:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        # A served index.html IS the self-reload mechanism observably working —
        # the one journal line that says the kiosk actually fetched a new page.
        if rel == "index.html":
            print(f"ui: served index.html to {self.client_address[0]}",
                  file=sys.stderr, flush=True)
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
               f"listening on :{LISTEN_PORT} — {BUILD['file']} ({BUILD['mtime']})"
               f" ui {BUILD['ui']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
