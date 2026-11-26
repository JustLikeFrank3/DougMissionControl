#!/usr/bin/env python3
"""douglas-agent — Douglas's one window for the Flight Deck panel.

Runs ON DOUGLAS (the GEEKOM A9 Mega inference box), installed by
douglas/setup.sh as the douglas-agent systemd unit. Stdlib only.

    GET  /status?token=<t>   one JSON: host, GPU and model-server metrics,
                             disks, services, model files, what is installed,
                             voice-pipeline state, the recent transcript
    POST /ptt?token=<t>      {"action":"start|stop|cancel","via":"deck"}
                             relayed to the voice pipeline's own PTT endpoint
    POST /say?token=<t>      {"text":"...","via":"deck|phone","speak":false,
                              "image":"<base64>"}  (the picture is optional)
                             a typed turn, relayed to the pipeline's /say
    POST /hear?token=<t>     {"audio":"<base64 16-bit mono PCM>","rate":16000,
                              "via":"phone","id":"..."}
                             a turn recorded on a phone's own microphone,
                             relayed to the pipeline's /hear
    GET  /picture?token=<t>&id=<id>
                             a picture Douglas drew, by the id its transcript
                             line carries ("image"); relayed from the pipeline
    GET  /speech?token=<t>&id=<id>
                             the reply to that turn as a WAV, for the phone
                             to play; JSON saying where it stands until then
    GET  /transcript?token=<t>&before=<ts>&limit=<n>
                             older lines of the conversation, for a screen
                             scrolled up past what /status carries
    GET  /memory?token=<t>&op=list|get|events|stats[&kind=&status=&q=&limit=&offset=&id=]
                             what Douglas remembers and the record of what
                             he did, for the owner to read
    POST /memory?token=<t>   {"op":"correct|add|delete|restore|purge", ...}
                             the owner puts a memory right or takes it away
    GET  /health             200 "ok" — liveness, no token, no data

Everything it reports is read at request time from where it already lives:
/proc and /sys for the machine, llama.cpp's /health, /metrics, /props and
/v1/models for the model, and two files the voice pipeline owns for the
conversation. It invents nothing: a value it cannot read is null, never 0,
because "idle" and "unknown" are different facts on the panel.

The machine's part of that (everything but the conversation) is also given
to the programs on this machine that ask for it, at a socket of its own
(douglas-agent-vitals.socket): one reading for each connection and nothing
else. That is how Douglas can say how his own machine is doing without
holding this agent's token, which also opens his conversation and his
memory. Nothing can be asked for there and nothing changed: whatever is
sent to it is not read.

It also keeps watch. Every twenty seconds it holds what it has just read of
this machine, and what Mission Control on the Pi says of the others
(FLEET_URL, with the read token), against a handful of rules: a machine that
should answer and does not, one that is too hot, a service that should be
running and is not or keeps being started again, a disk nearly full, a task
left open for days. What a rule finds is a notice, kept by douglas-memory
(the owner's door): one for a thing for as long as it holds, raised only
once it has held for a while and cleared only once it has been gone for a
while, so that a reading that flickers raises nothing. No model is asked
anything, and nothing is acted on: a notice is a sentence on a screen.

What Douglas remembers is kept by douglas-memory, which has two doors: one
for Douglas, and one for the owner, which is the one this has the key to
(MEMORY_OWNER_SOCKET, a socket this unit's group may open). So the owner's
screen can read, correct and delete what he remembers, and he cannot.

The voice pipeline contract (whatever implements DougVoice's brain):
    VOICE_STATE_FILE   JSON {"state": "idle|listening|thinking|speaking",
                             "satellite": bool, "eyes": bool, "shot": <pixels>,
                             "ts": <epoch>, "last_turn": <epoch>}
                       rewritten on every change and at least every 10 s;
                       older than VOICE_STALE_S reads as no pipeline.
    TRANSCRIPT_FILE    JSONL, one {"ts", "who": "user|douglas", "text",
                       "via": "voice|chat|deck"} per turn, appended.
    VOICE_PTT_URL      accepts the same POST body as /ptt here.

Config comes from the environment (systemd EnvironmentFile=/etc/douglas/agent.env).
Same trust model as the workstation's boot agents: a shared token, fine on a
home LAN and a tailnet, not something to put on the internet.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

PORT = int(os.environ.get("DOUGLAS_AGENT_PORT", "9111"))
TOKEN = os.environ.get("DOUGLAS_AGENT_TOKEN", "")
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8081").rstrip("/")
VOICE_STATE_FILE = os.environ.get("VOICE_STATE_FILE", "/run/douglas/voice.json")
SAY_BODY_MAX = 12 * 1024 * 1024          # a typed turn may carry a picture
HEAR_BODY_MAX = 6 * 1024 * 1024          # a recording from a phone, as base64
SPEECH_MAX = 8 * 1024 * 1024             # a reply's audio on its way back
PICTURE_MAX = 16 * 1024 * 1024           # a picture he drew, on its way to a screen
PICTURE_ID = re.compile(r"p-[0-9a-f]{12}")
VOICE_STALE_S = float(os.environ.get("VOICE_STALE_S", "30"))
TRANSCRIPT_FILE = os.environ.get("TRANSCRIPT_FILE", "/var/lib/douglas/transcript.jsonl")
TRANSCRIPT_TURNS = int(os.environ.get("TRANSCRIPT_TURNS", "40"))
VOICE_PTT_URL = os.environ.get("VOICE_PTT_URL", "")
MEMORY_OWNER_SOCKET = os.environ.get("MEMORY_OWNER_SOCKET", "")
# The door of douglas-switch: where the owner's screens switch his tools on and off.
SWITCH_SOCKET = os.environ.get("SWITCH_SOCKET", "")
MEMORY_BODY_MAX = 64 * 1024              # a correction is a sentence or two
MEMORY_ANSWER_MAX = 240_000              # what the panel's relay will carry on
# What this machine is asked about itself, beyond the meters: the services
# worth knowing the health of, the file systems worth knowing the room on,
# where the model files are and where the programs are installed.
AGENT_UNITS = tuple((os.environ.get("AGENT_UNITS") or
                     "llama-server llama-eyes douglas-voice douglas-memory douglas-work douglas-job "
                     "douglas-tts douglas-draw douglas-agent douglas-chatgpt douglas-mcp open-webui "
                     "wyoming-whisper wyoming-piper").split())
AGENT_DISKS = tuple((os.environ.get("AGENT_DISKS") or "/ /srv/models").split())
MODELS_DIR = os.environ.get("MODELS_DIR", "/srv/models")
PROGRAMS_DIR = os.environ.get("PROGRAMS_DIR", "/opt/douglas")
SYSTEMCTL = os.environ.get("SYSTEMCTL", "/usr/bin/systemctl")
VITALS_SOCKET = os.environ.get("VITALS_SOCKET", "")     # run by hand: the socket is made here
# The watch (see the top of this file). On when there is somewhere to keep a
# notice, and not switched off.
WATCH = os.environ.get("WATCH", "1") != "0"
WATCH_EVERY_S = float(os.environ.get("WATCH_EVERY_S", "20"))
FLEET_URL = os.environ.get("FLEET_URL", "")              # Mission Control's view of the fleet, on the Pi
FLEET_TOKEN = os.environ.get("FLEET_TOKEN", "")          # the read token for it, which opens nothing else
# Machines that should always answer, besides the Pi itself. Not the
# workstation and not the laptops: those being off is an ordinary evening.
WATCH_ALWAYS_ON = tuple((os.environ.get("WATCH_ALWAYS_ON") or "dougvoice").split())
# Too hot, in degrees C: a warning past the first figure, critical past the second.
WATCH_HOT = {k: tuple(float(x) for x in v.split("/")) for k, _, v in (
    p.partition("=") for p in (os.environ.get("WATCH_HOT") or
                               "douglas=92/100 node1=75/82 workstation=85/92").split())}
# A disk nearly full, in per cent free: a warning under the first, critical under the second.
WATCH_DISK = tuple(float(x) for x in (os.environ.get("WATCH_DISK") or "10/5").split("/"))
# Services whose being down is critical; any other is a warning.
WATCH_CRITICAL = set((os.environ.get("WATCH_CRITICAL") or
                      "llama-server douglas-voice douglas-memory deck-api").split())
WATCH_TASK_DAYS = float(os.environ.get("WATCH_TASK_DAYS", "7"))   # a task left open this long is said so
SYS = os.environ.get("SYSFS_ROOT", "/sys")     # overridable for tests
PROC = os.environ.get("PROCFS_ROOT", "/proc")

PROM_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)')


# ── machine ──────────────────────────────────────────────────────────────

def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _int(path: str) -> int | None:
    v = _read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


class CpuMeter:
    """Busy % between two /proc/stat reads. The first call has no interval
    to measure and says so with None rather than a made-up 0."""

    def __init__(self) -> None:
        self._prev: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def sample(self) -> float | None:
        line = (_read(f"{PROC}/stat") or "").splitlines()[:1]
        if not line or not line[0].startswith("cpu "):
            return None
        vals = [int(x) for x in line[0].split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        total = sum(vals[:8])
        with self._lock:
            prev, self._prev = self._prev, (idle, total)
        if prev is None or total <= prev[1]:
            return None
        d_total, d_idle = total - prev[1], idle - prev[0]
        return round(100.0 * (d_total - d_idle) / d_total, 1)


CPU = CpuMeter()


class Kept:
    """A reading that costs something to take (a process started, a folder
    walked, files hashed), taken again only once it is that many seconds old.
    The panel asks every two seconds; the answer to "which model files are
    on the disk" does not change that often."""

    def __init__(self, take, seconds: float) -> None:
        self.take, self.seconds = take, seconds
        self.at, self.value = 0.0, None
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            if not self.at or time.monotonic() - self.at >= self.seconds:
                self.value, self.at = self.take(), time.monotonic()
            return self.value


def _cpu_name() -> str | None:
    for line in (_read(f"{PROC}/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            return " ".join(line.partition(":")[2].split()) or None
    return None


CPU_NAME = Kept(_cpu_name, 3600)


def host_metrics() -> dict:
    mem = {}
    for line in (_read(f"{PROC}/meminfo") or "").splitlines():
        k, _, rest = line.partition(":")
        try:
            mem[k] = int(rest.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
    up = (_read(f"{PROC}/uptime") or "").split()
    load = (_read(f"{PROC}/loadavg") or "").split()
    try:
        load1 = float(load[0]) if load else None
    except ValueError:
        load1 = None
    return {
        "cpu": CPU_NAME(),
        "cores": os.cpu_count(),
        "kernel": os.uname().release,
        "load": load1,
        "cpu_pct": CPU.sample(),
        "mem_total_bytes": total,
        "mem_used_bytes": total - avail if total and avail is not None else None,
        "uptime_s": float(up[0]) if up else None,
        "temp_c": apu_temp(),
        "power_w": gpu_power(),
    }


def _hwmons(name: str) -> list[str]:
    return [d for d in sorted(glob.glob(f"{SYS}/class/hwmon/hwmon*"))
            if _read(f"{d}/name") == name]


def apu_temp() -> float | None:
    """k10temp's Tctl is the package's own reading; amdgpu's edge sensor is
    the fallback on kernels that do not expose k10temp for this part."""
    for name in ("k10temp", "amdgpu"):
        for d in _hwmons(name):
            v = _int(f"{d}/temp1_input")
            if v is not None:
                return round(v / 1000, 1)
    return None


def gpu_power() -> float | None:
    """amdgpu's socket power — on an APU that is the whole package."""
    for d in _hwmons("amdgpu"):
        for f in ("power1_average", "power1_input"):
            v = _int(f"{d}/{f}")
            if v is not None:
                return round(v / 1_000_000, 1)
    return None


def gpu_metrics() -> dict:
    """The first AMD GPU's busy % and memory. On Strix Halo the model sits in
    GTT (shared system memory) as much as in the BIOS-carved VRAM, so both
    pools are reported and the panel adds them up."""
    for card in sorted(glob.glob(f"{SYS}/class/drm/card[0-9]*/device")):
        if _read(f"{card}/vendor") != "0x1002":
            continue
        return {
            "busy_pct": _int(f"{card}/gpu_busy_percent"),
            "vram_used_bytes": _int(f"{card}/mem_info_vram_used"),
            "vram_total_bytes": _int(f"{card}/mem_info_vram_total"),
            "gtt_used_bytes": _int(f"{card}/mem_info_gtt_used"),
            "gtt_total_bytes": _int(f"{card}/mem_info_gtt_total"),
        }
    return {}


def disks() -> list[dict]:
    """How full the file systems worth knowing about are: each once, however
    many of the paths asked about are on it."""
    out, seen = [], set()
    for path in AGENT_DISKS:
        try:
            st, dev = os.statvfs(path), os.stat(path).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        total = st.f_blocks * st.f_frsize
        out.append({"path": path, "total_bytes": total, "free_bytes": st.f_bavail * st.f_frsize,
                    "used_bytes": total - st.f_bfree * st.f_frsize})
    return out


def _units() -> list[dict] | None:
    """Douglas's services: which are up, since when, and how many times
    systemd has had to start each again. Asked of systemd with one fixed
    command: the unit names are this program's own list, never a caller's.
    None when systemd cannot be asked, which is not the same as none up."""
    try:
        ran = subprocess.run(
            [SYSTEMCTL, "show", *[u + ".service" for u in AGENT_UNITS], "-p",
             "Id,LoadState,ActiveState,SubState,NRestarts,ActiveEnterTimestampMonotonic,Result,UnitFileState"],
            capture_output=True, text=True, timeout=4, check=False)
        up = float((_read(f"{PROC}/uptime") or "0").split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    out = ran.stdout
    if ran.returncode and not out.strip():
        return None                               # systemd would not say: not known, which is not "none"
    units, now = [], time.time()
    for block in out.strip().split("\n\n"):
        p = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if not p.get("Id") or p.get("LoadState") != "loaded":
            continue                              # not installed on this machine
        try:
            since = now - (up - int(p.get("ActiveEnterTimestampMonotonic") or 0) / 1e6)
            restarts = int(p.get("NRestarts") or 0)
        except ValueError:
            since, restarts = None, None
        active = p.get("ActiveState") == "active"
        units.append({"unit": p["Id"].removesuffix(".service"), "state": p.get("ActiveState"),
                      "sub": p.get("SubState"), "restarts": restarts, "result": p.get("Result"),
                      # Whether it is meant to run at all: one that is switched
                      # off on purpose is not one that has gone down.
                      "enabled": p.get("UnitFileState") or None,
                      "since": round(since) if since and active else None})
    return units


def _models() -> list[dict]:
    """The model files on the disk, biggest first: what he could be run on,
    whether or not it is what he is running on now."""
    found = []
    base = MODELS_DIR.rstrip("/")
    for root, dirs, files in os.walk(base):
        if root[len(base):].count(os.sep) >= 3:
            dirs[:] = []
        for name in files:
            if not name.lower().endswith((".gguf", ".safetensors", ".onnx")):
                continue
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            if size >= 100 * 1024 * 1024:           # a model, not a config beside one
                found.append({"file": os.path.join(root, name)[len(base) + 1:], "bytes": size})
    return sorted(found, key=lambda m: -m["bytes"])[:40]


def _versions() -> list[dict]:
    """What is installed: each of Douglas's programs, when it was put there
    and the start of its checksum, which is what tells two versions apart."""
    out = []
    for path in sorted(glob.glob(f"{PROGRAMS_DIR}/*.py")):
        try:
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read(8_000_000)).hexdigest()[:12]
            out.append({"file": os.path.basename(path), "installed": round(os.path.getmtime(path)),
                        "sha": digest})
        except OSError:
            continue
    return out


UNITS, MODELS, VERSIONS = Kept(_units, 10), Kept(_models, 120), Kept(_versions, 120)


# ── model server ─────────────────────────────────────────────────────────

def _get(path: str, timeout: float = 2.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"{LLM_URL}{path}", timeout=timeout) as r:
            return r.status, r.read(512_000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, b""


def llm_metrics() -> dict:
    """llama.cpp, as it describes itself. Throughput needs the server started
    with --metrics; without it those fields are null, not zero."""
    code, _ = _get("/health")
    if code == 503:
        return {"state": "loading"}
    if code != 200:
        return {"state": "stopped"}
    out: dict = {"state": "running"}
    code, body = _get("/v1/models")
    if code == 200:
        try:
            data = json.loads(body).get("data") or []
            name = os.path.basename(str(data[0]["id"])) if data else None
            if name and name.lower().endswith(".gguf"):
                name = name[:-5]
            out["model"] = name
            meta = (data[0].get("meta") or {}) if data else {}
            if isinstance(meta, dict):              # how big a model it is, where the server says
                out["params"], out["size_bytes"] = meta.get("n_params"), meta.get("size")
        except (ValueError, KeyError, IndexError, AttributeError):
            pass
    code, body = _get("/props")
    if code == 200:
        try:
            props = json.loads(body)
            out["ctx"] = (props.get("default_generation_settings") or {}).get("n_ctx")
            out["slots"] = props.get("total_slots")       # conversations it can hold at once
            out["build"] = props.get("build_info")
            out["file"] = os.path.basename(str(props.get("model_path") or "")) or None
            seen = props.get("modalities")
            out["vision"] = bool(seen.get("vision")) if isinstance(seen, dict) else None
        except (ValueError, AttributeError):
            pass
    code, body = _get("/slots")
    if code == 200:
        try:                                        # how many are in use: never what is in them
            out["busy_slots"] = sum(1 for s in json.loads(body) if isinstance(s, dict) and s.get("is_processing"))
        except (ValueError, TypeError):
            pass
    code, body = _get("/metrics")
    if code == 200:
        prom = {}
        for line in body.decode("utf-8", "replace").splitlines():
            m = PROM_LINE.match(line.strip())
            if m and not line.startswith("#") and m.group(1) not in prom:
                try:
                    prom[m.group(1)] = float(m.group(3))
                except ValueError:
                    pass
        out["prompt_tps"] = prom.get("llamacpp:prompt_tokens_seconds")
        out["gen_tps"] = prom.get("llamacpp:predicted_tokens_seconds")
        rp = prom.get("llamacpp:requests_processing")
        rd = prom.get("llamacpp:requests_deferred")
        out["processing"] = int(rp) if rp is not None else None
        out["deferred"] = int(rd) if rd is not None else None
        for key, name in (("prompt_tokens", "llamacpp:prompt_tokens_total"),
                          ("predicted_tokens", "llamacpp:tokens_predicted_total")):
            out[key] = int(prom[name]) if name in prom else None     # since the server started
    return out


# ── conversation ─────────────────────────────────────────────────────────

def voice_state() -> dict:
    """The pipeline's own heartbeat file. Missing or stale means there is no
    pipeline to talk to, which the panel shows as an unusable button."""
    try:
        with open(VOICE_STATE_FILE, encoding="utf-8") as f:
            v = json.load(f)
        if not isinstance(v, dict):
            raise ValueError
    except (OSError, ValueError):
        return {"state": "absent", "satellite": False}
    if time.time() - float(v.get("ts") or 0) > VOICE_STALE_S:
        return {"state": "absent", "satellite": False}
    heard, shot = v.get("heard"), v.get("shot")
    return {"state": str(v.get("state") or "absent"),
            "satellite": bool(v.get("satellite")),
            "eyes": bool(v.get("eyes")),          # he can be sent a picture
            # The long side of a photo he can make use of, where that is more
            # than a screen sends by itself: he looks closer by cutting a part
            # out of it. A number in a sane range, or nothing.
            "shot": shot if isinstance(shot, int) and 1280 < shot <= 4096 else 0,
            "last_turn": v.get("last_turn"),
            # The last time he listened: how long, how loud, how many words.
            # What lets a panel say "heard nothing" instead of saying nothing.
            "heard": heard if isinstance(heard, dict) else None,
            # A change that is waiting for the owner's yes, in the words that
            # were read back, and which of his tools are on, off or not set
            # up. Passed on only in the shape they are known to have.
            "pending": _pending(v.get("pending")),
            "job": _job(v.get("job")),
            "tools": ({str(k)[:24]: s for k, s in list(v["tools"].items())[:40] if s in ("on", "off", "absent")}
                      if isinstance(v.get("tools"), dict) else None)}


def _pending(p) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"]:
        return None
    num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else None      # noqa: E731
    return {"id": p["id"][:32], "kind": str(p.get("kind") or "")[:24], "text": str(p.get("text") or "")[:8000],
            "via": str(p.get("via") or "")[:12], "asked": num(p.get("asked")), "until": num(p.get("until"))}


def _job(j) -> dict | None:
    """The job in his workshop, as the pipeline says there is one: passed on
    only in the shape it is known to have."""
    if not isinstance(j, dict) or not isinstance(j.get("running"), bool):
        return None
    num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else None      # noqa: E731
    return {"running": j["running"], "what": str(j.get("what") or "")[:300], "began": num(j.get("began")),
            "ended": num(j.get("ended")), "report": _job_report(j.get("report"))}


def _job_report(r) -> dict | None:
    """What a job was handed and what became of it, as the pipeline has it
    from the workshop: passed on only in the shape it is known to have, each
    piece cut to size. Requirements carry one of four states and no other."""
    if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"]:
        return None
    text = lambda x, n: str(x or "")[:n]                                                        # noqa: E731
    rows = lambda key, n: [x for x in (r.get(key) if isinstance(r.get(key), list) else [])[:n]  # noqa: E731
                           if isinstance(x, dict)]
    states = ("done", "outstanding", "blocked", "pending")
    return {"id": r["id"][:8], "repo": text(r.get("repo"), 120), "branch": text(r.get("branch"), 80),
            "kind": text(r.get("kind"), 12), "state": text(r.get("state"), 12),
            "outcome": r.get("outcome") if r.get("outcome") in ("complete", "partial", "unmet") else "",
            "asked": text(r.get("asked"), 1200), "base": text(r.get("base"), 12),
            "base_branch": text(r.get("base_branch"), 80),
            "before": [text(b, 300) for b in (r.get("before") if isinstance(r.get("before"), list) else [])[:3]
                       if isinstance(b, str)],
            "context": [{"from": text(c.get("from"), 80), "text": text(c.get("text"), 1500)} for c in rows("context", 3)],
            "needs": [{"n": n["n"] if isinstance(n.get("n"), int) and not isinstance(n["n"], bool) else 0,
                       "text": text(n.get("text"), 400), "accept": text(n.get("accept"), 300),
                       "state": n.get("state") if n.get("state") in states else "outstanding",
                       "note": text(n.get("note"), 240)} for n in rows("needs", 12)],
            "stat": text(r.get("stat"), 80),
            "files": [text(f, 120) for f in (r.get("files") if isinstance(r.get("files"), list) else [])[:12]
                      if isinstance(f, str)],
            "checks": [{"what": text(c.get("what"), 120), "ok": c.get("ok") is True, "said": text(c.get("said"), 240)}
                       for c in rows("checks", 6)],
            "ran": [{"cmd": text(c.get("cmd"), 160),
                     "code": c["code"] if isinstance(c.get("code"), int) and not isinstance(c["code"], bool) else None}
                    for c in rows("ran", 8)],
            "looked": r.get("looked") if r.get("looked") in ("ok", "found", "none") else "",
            "summary": text(r.get("summary"), 1500), "error": text(r.get("error"), 300), "pr": text(r.get("pr"), 200)}


def relay_approval(body: dict) -> tuple[int, dict]:
    """The owner's answer, on one of their screens, to the change that is
    waiting: passed to the pipeline with the number of what it answers. A
    yes is a true and a no a false; anything else is not passed on."""
    pid, yes = body.get("id"), body.get("yes")
    if not isinstance(pid, str) or not pid or not isinstance(yes, bool):
        return 400, {"ok": False, "message": "an answer is the number of what is waiting, and yes: true or false"}
    url = VOICE_PTT_URL.replace("/ptt", "/approve", 1) if VOICE_PTT_URL else ""
    return _relay(url, {"id": pid[:32], "yes": yes, "via": "phone" if body.get("via") == "phone" else "deck"},
                  "approved" if yes else "refused", timeout=5)


def relay_switch(body: dict) -> tuple[int, dict]:
    """The owner switching something of Douglas's on or off: passed to
    douglas-switch, the one program that writes that down. This one has the
    key to its door (a group of this unit's); the pipeline does not."""
    tool, on = body.get("tool"), body.get("on")
    if not isinstance(tool, str) or not re.fullmatch(r"[a-z_]{1,24}", tool) or not isinstance(on, bool):
        return 400, {"ok": False, "message": "a switch is a tool, and on: true or false"}
    if not SWITCH_SOCKET:
        return 200, {"ok": False, "message": "the switches are not installed on Douglas"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(SWITCH_SOCKET)
            s.sendall(json.dumps({"op": "set", "tool": tool[:24], "on": on}).encode() + b"\n")
            with s.makefile("rb") as f:
                out = json.loads(f.readline(8192) or b"null")
        if not isinstance(out, dict):
            raise ValueError
    except (OSError, ValueError):
        return 200, {"ok": False, "message": "the switches did not answer"}
    print(f"douglas-agent: {tool[:24]} switched {'on' if on else 'off'} from a screen"
          + ("" if out.get("ok") else ", refused"), flush=True)
    return 200, {"ok": bool(out.get("ok")), "message": str(out.get("text") or ("on" if on else "off"))[:200],
                 "off": [str(x)[:24] for x in out.get("off") or []][:40] if isinstance(out.get("off"), list) else None}


def transcript() -> list[dict]:
    """The last N turns. Reads only the file's tail, so a transcript that
    has grown for a year costs the same as one from this morning."""
    try:
        with open(TRANSCRIPT_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    if size > 64 * 1024:
        tail = tail[1:]          # the first line is probably cut mid-record
    turns = []
    # Parse the whole tail and keep the last N good turns: slicing lines
    # first would let one torn write cost the panel a real turn.
    for line in tail:
        turn = _turn(line)
        if turn is not None:
            turns.append(turn)
    return turns[-TRANSCRIPT_TURNS:]


def _turn(line) -> dict | None:
    """One transcript line as a screen is given it, or None for a line that
    is not a turn (a torn write, a blank)."""
    try:
        t = json.loads(line)
    except ValueError:
        return None
    if not (isinstance(t, dict) and t.get("text")):
        return None
    turn = {"ts": t.get("ts"),
            "who": "user" if t.get("who") == "user" else "douglas",
            "text": str(t["text"])[:2000],
            "via": str(t.get("via") or "voice")}
    if PICTURE_ID.fullmatch(str(t.get("image") or "")):
        turn["image"] = t["image"]       # he drew one: GET /picture?id= has it
    return turn


TRANSCRIPT_PAGE = 30
TRANSCRIPT_PAGE_MAX = 100
TRANSCRIPT_CHUNK = 64 * 1024
TRANSCRIPT_READ_MAX = 4 * 1024 * 1024


def transcript_before(before: object, limit: object) -> dict:
    """The newest `limit` turns older than `before`, oldest first, for a
    screen scrolled up past what /status carries. The file is read backwards
    from its end a chunk at a time, and never more than TRANSCRIPT_READ_MAX
    in one request, because the record grows for years."""
    try:
        before = float(before) if before not in (None, "") else float("inf")
    except (TypeError, ValueError):
        before = float("inf")
    if before != before:                   # NaN would match nothing, forever
        before = float("inf")
    try:
        limit = int(limit) if limit not in (None, "") else TRANSCRIPT_PAGE
    except (TypeError, ValueError):
        limit = TRANSCRIPT_PAGE
    limit = max(1, min(TRANSCRIPT_PAGE_MAX, limit))
    found: list[dict] = []                 # newest first while reading
    try:
        with open(TRANSCRIPT_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            rest = b""                     # the start of a line cut by a chunk
            read = 0
            # One more than asked for is what tells "more" from "that was all".
            while pos > 0 and len(found) <= limit and read < TRANSCRIPT_READ_MAX:
                step = min(TRANSCRIPT_CHUNK, pos, TRANSCRIPT_READ_MAX - read)
                pos -= step
                f.seek(pos)
                rest = f.read(step) + rest
                read += step
                lines = rest.split(b"\n")
                # Unless this is the start of the file, the first piece may be
                # the end of a line whose beginning is in the next chunk back.
                rest = lines.pop(0) if pos > 0 else b""
                for line in reversed(lines):
                    _older(line, before, found)
                    if len(found) > limit:
                        break
    except OSError:
        return {"ok": True, "lines": [], "more": False}
    # Stopping short of the file's start, for either reason, leaves lines older
    # than these that a screen can still ask for.
    more = len(found) > limit or pos > 0
    return {"ok": True, "lines": list(reversed(found[:limit])), "more": more}


def _older(line: bytes, before: float, found: list) -> None:
    turn = _turn(line.decode("utf-8", "replace")) if line.strip() else None
    ts = turn.get("ts") if turn else None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts < before:
        found.append(turn)


def relay_ptt(body: dict) -> tuple[int, dict]:
    action = str(body.get("action") or "")
    if action not in ("start", "stop", "cancel"):
        return 400, {"ok": False, "message": "action must be start, stop or cancel"}
    return _relay(VOICE_PTT_URL, {"action": action,
                                  "via": str(body.get("via") or "deck")}, action)


def relay_say(body: dict) -> tuple[int, dict]:
    """A typed turn, with or without a picture, relayed to the pipeline's
    /say beside its /ptt (same token). The pipeline answers at once; the
    reply lands in the transcript."""
    text = str(body.get("text") or "").strip()
    image = body.get("image") if isinstance(body.get("image"), str) else ""
    if not text and not image:
        return 400, {"ok": False, "message": "nothing to say"}
    url = VOICE_PTT_URL.replace("/ptt", "/say", 1) if VOICE_PTT_URL else ""
    payload = {"text": text[:2000], "via": str(body.get("via") or "deck"),
               "speak": bool(body.get("speak"))}
    if image:
        payload["image"] = image            # base64, passed on untouched
    return _relay(url, payload, "thinking", timeout=20 if image else 3)


def relay_hear(body: dict) -> tuple[int, dict]:
    """A turn recorded on a phone's own microphone, relayed whole to the
    pipeline's /hear. The pipeline answers at once; the reply lands in the
    transcript, and its audio waits there to be collected (speech())."""
    audio = body.get("audio") if isinstance(body.get("audio"), str) else ""
    if not audio:
        return 400, {"ok": False, "message": "nothing recorded"}
    try:
        rate = int(body.get("rate") or 16000)
    except (TypeError, ValueError):
        rate = 16000
    url = VOICE_PTT_URL.replace("/ptt", "/hear", 1) if VOICE_PTT_URL else ""
    return _relay(url, {"audio": audio, "rate": rate, "via": str(body.get("via") or "phone"),
                        "id": str(body.get("id") or "")[:64]}, "thinking", timeout=20)


def speech(ask_id: str) -> tuple[int, bytes | dict]:
    """The reply to a recorded turn as the pipeline kept it: a WAV once it
    is ready, and until then the pipeline's own word on where it stands."""
    if not VOICE_PTT_URL:
        return 200, {"ok": False, "state": "unknown"}
    url = VOICE_PTT_URL.replace("/ptt", "/speech", 1)
    url += ("&" if "?" in url else "?") + "id=" + quote(ask_id[:64])
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            body = r.read(SPEECH_MAX)
            if r.headers.get_content_type() == "audio/wav":
                return 200, body
            return r.status, json.loads(body)
    except urllib.error.HTTPError as e:     # it answered: an id it does not know
        try:
            return e.code, json.loads(e.read(4096))
        except (ValueError, OSError):
            return e.code, {"ok": False, "state": "unknown"}
    except (urllib.error.URLError, OSError, ValueError):
        return 502, {"ok": False, "state": "failed",
                     "message": "the voice pipeline did not answer"}


def _relay(url: str, payload: dict, done: str, timeout: float = 3) -> tuple[int, dict]:
    if not url:
        return 200, {"ok": False, "message": "no voice pipeline installed on Douglas yet"}
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = 200 <= r.status < 300
    except urllib.error.HTTPError as e:     # it answered, and said no
        try:
            why = json.loads(e.read(4096)).get("message")
        except (ValueError, AttributeError, OSError):
            why = None
        return 200, {"ok": False, "message": str(why or "the voice pipeline refused")}
    except (urllib.error.URLError, OSError, ValueError):
        return 200, {"ok": False, "message": "the voice pipeline did not answer"}
    return 200, {"ok": ok, "message": done if ok else "the voice pipeline refused"}


def picture(pid: str) -> tuple[int, bytes | dict, str]:
    """A picture Douglas drew, from the pipeline that keeps it: the bytes
    and their type. The pictures are the pipeline's alone to read, so this
    asks it rather than the disk."""
    if not PICTURE_ID.fullmatch(pid or "") or not VOICE_PTT_URL:
        return 404, {"error": "no such picture"}, ""
    url = VOICE_PTT_URL.replace("/ptt", "/picture", 1)
    url += ("&" if "?" in url else "?") + "id=" + quote(pid)
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            kind, body = r.headers.get_content_type(), r.read(PICTURE_MAX)
    except urllib.error.HTTPError as e:
        return (404 if e.code == 404 else 502), {"error": "no such picture"}, ""
    except (urllib.error.URLError, OSError):
        return 502, {"error": "douglas-voice did not answer"}, ""
    if kind not in ("image/jpeg", "image/png"):
        return 502, {"error": "that is not a picture"}, ""
    return 200, body, kind


# ── what he remembers ────────────────────────────────────────────────────

# What the owner's screen may ask of the memory through here, and what each
# question may carry: one plain value per name. Reading is one list and
# changing another, so that a GET can never change anything. Nothing else
# the memory's door answers (the whole of it at once, say) is passed on.
MEMORY_LOOKS = {"list": ("kind", "status", "q", "limit", "offset"), "get": ("id",),
                "events": ("limit", "kind"), "stats": (), "notices": ("limit",)}
MEMORY_CHANGES = {"correct": ("id", "text", "subject", "kind", "confidence", "done"),
                  "add": ("kind", "subject", "text"),
                  "delete": ("id",), "restore": ("id",), "purge": ("id",),
                  # A notice the owner has seen, and where each severity is to go.
                  "notice_ack": ("id",),
                  "notice_prefs": ("severity", "tell", "aloud", "quiet_from", "quiet_to")}
# What the watch in this program asks of the same door. Not on either list
# above: nothing that comes in over the network can raise or clear a notice.
WATCH_ASKS = {"notice_raise": ("key", "rule", "subject", "severity", "text"), "notice_clear": ("key",),
              "notices": ("limit",), "list": ("kind", "status", "limit")}
MEMORY_MOST = {"list": 50, "events": 200}        # rows at a time


def memory(op: object, fields: dict, allowed: dict) -> tuple[int, dict]:
    """One question to Douglas's memory through the owner's door, and its
    answer as the memory gave it. `allowed` is which questions this request
    may ask at all."""
    if not isinstance(op, str) or op not in allowed:
        return 400, {"ok": False, "text": "That is not something to ask of the memory this way."}
    if not MEMORY_OWNER_SOCKET:
        return 200, {"ok": False, "text": "Douglas has no memory installed."}
    ask: dict = {"op": op}
    for name in allowed[op]:
        v = fields.get(name)
        if isinstance(v, bool) or isinstance(v, (int, float)):
            ask[name] = v
        elif isinstance(v, str):
            ask[name] = v[:4000]
    if op in MEMORY_MOST:
        try:
            ask["limit"] = min(max(1, int(ask.get("limit") or MEMORY_MOST[op])), MEMORY_MOST[op])
        except (TypeError, ValueError, OverflowError):
            ask["limit"] = MEMORY_MOST[op]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(MEMORY_OWNER_SOCKET)
            s.sendall(json.dumps(ask).encode() + b"\n")
            with s.makefile("rb") as f:
                line = f.readline(MEMORY_ANSWER_MAX + 1)
        if len(line) > MEMORY_ANSWER_MAX:
            return 200, {"ok": False, "text": "That is more than can be shown at once: ask for less."}
        out = json.loads(line)
        if not isinstance(out, dict):
            raise ValueError
    except (OSError, ValueError):
        return 200, {"ok": False, "text": "Douglas's memory did not answer."}
    return 200, out


# ── the watch ────────────────────────────────────────────────────────────

def _bytes(b) -> str:
    for unit, size in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20)):
        if b >= size:
            return f"{b / size:.0f} {unit}" if b / size >= 100 else f"{b / size:.1f} {unit}"
    return f"{b / 1024:.0f} KB"


def day(when) -> str:
    """A day, the way it is said: 15 November."""
    try:
        return time.strftime("%-d %B", time.localtime(float(when)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "a day not known"


def findings(v: dict | None, fleet: dict | None, tasks: list, now: float, memo: dict) -> list[dict]:
    """What holds right now that a rule is about, each as the notice it would
    be: its key (one notice for a thing), who it is about, how bad, the
    sentence, how long it has to hold before it is raised and how long it
    has to be gone before it is cleared. A notice stays as it was written
    for as long as it is open, so its sentence says what stays true that
    long, and a number in it is said to be from when it was raised. From
    readings only: `v` is this
    machine's vitals, `fleet` what the Pi says of the rest (None when the Pi
    did not answer), `tasks` the open tasks in his memory. `memo` is the
    little that has to be carried from one look to the next."""
    out: list[dict] = []

    def find(key, rule, subject, severity, text, after, hold) -> None:
        out.append({"key": key, "rule": rule, "subject": subject, "severity": severity, "text": text,
                    "after": after, "hold": hold})

    seen = []                             # (id, name, degrees or None, disks, services or None)
    if v:
        seen.append(("douglas", "DOUGLAS", (v.get("host") or {}).get("temp_c"), v.get("disks") or [],
                     v.get("units")))
    if FLEET_URL and fleet is None:
        # The Pi itself. Nothing is said of the machines it speaks for: with
        # it gone they are not known to be down, only not known.
        find("gone:node1", "gone", "DOUG PRIME", "critical",
             "Mission Control's Pi (DOUG PRIME) is not answering Douglas.", 90, 60)
    for m in (fleet or {}).get("machines") or []:
        if not isinstance(m, dict):
            continue
        mid, name, r = str(m.get("id") or ""), str(m.get("name") or m.get("id") or "a machine"), m.get("reports")
        r = r if isinstance(r, dict) else {}
        if mid in WATCH_ALWAYS_ON and not m.get("up"):
            find(f"gone:{mid}", "gone", name, "warning", f"{name} is not answering.", 90, 60)
        if mid == "node1":
            seen.append((mid, name, r.get("temp_c"), [r["disk"]] if isinstance(r.get("disk"), dict) else [],
                         m.get("services") if "services" in m else []))
        elif mid == "workstation" and m.get("up"):
            seen.append((mid, name, r.get("gpu_temp_c"), [], []))

    # What was installed lately: a service that is down just after its
    # program changed is a deployment that did not take.
    for x in (v or {}).get("versions") or []:
        f, sha = str(x.get("file") or ""), x.get("sha")
        if f and memo["sha"].get(f) not in (None, sha):
            memo["changed"][f.removesuffix(".py")] = now
        if f:
            memo["sha"][f] = sha

    for mid, name, degrees, disks, services in seen:
        warm, hot = WATCH_HOT.get(mid, (None, None))
        if isinstance(degrees, (int, float)) and warm is not None and degrees > warm:
            find(f"hot:{mid}", "hot", name, "critical" if degrees > hot else "warning",
                 f"{name} is running hot: {degrees:.0f} degrees C when this was raised, over its line of "
                 f"{hot if degrees > hot else warm:.0f}.", 120, 300)
        for d in disks:
            total, free = d.get("total_bytes"), d.get("free_bytes")
            if not total or free is None:
                continue
            left = 100.0 * free / total
            if left < WATCH_DISK[0]:
                find(f"disk:{mid}:{d.get('path')}", "disk", f"the disk {d.get('path')} on {name}",
                     "critical" if left < WATCH_DISK[1] else "warning",
                     f"The disk {d.get('path')} on {name} is nearly full: {_bytes(free)} left "
                     f"({100 - left:.0f}% full) when this was raised.", 60, 600)
        for u in services or []:
            unit, state = str(u.get("unit") or ""), u.get("state")
            if not unit:
                continue
            grave = "critical" if unit in WATCH_CRITICAL else "warning"
            off = state == "inactive" and u.get("enabled") in ("disabled", "masked")
            if state != "active" and not off:
                fresh = mid == "douglas" and now - memo["changed"].get(unit, 0) < 900
                find(f"unit:{mid}:{unit}", "unit", f"{unit} on {name}", "critical" if fresh else grave,
                     f"{unit} on {name} is not running "
                     f"({SUB_SAID.get(u.get('sub'), u.get('sub')) or state or 'unknown'})"
                     + (", and its program was changed a few minutes ago: the change did not take" if fresh else "")
                     + ".", 30, 300)
            # Started again and again: up when looked at, and not well.
            n = u.get("restarts")
            if isinstance(n, int):
                was = memo["restarts"].setdefault(f"{mid}:{unit}", [])
                was.append((now, n))
                while was and now - was[0][0] > 600:
                    was.pop(0)
                if n - was[0][1] >= 3:
                    find(f"restarts:{mid}:{unit}", "unit", f"{unit} on {name}", grave,
                         f"{unit} on {name} keeps being started again: {n - was[0][1]} times in ten minutes "
                         "when this was raised.", 0, 600)

    for t in tasks:
        if not isinstance(t, dict) or t.get("done") or t.get("kind") != "task":
            continue
        due, made = t.get("due"), t.get("created") or now
        if due and due <= now:
            find(f"task:{t.get('id')}", "task", "a task", "information",
                 f"A task was due on {day(due)}: {str(t.get('text'))[:200]}", 0, 0)
        elif not due and now - made >= WATCH_TASK_DAYS * 86400:
            find(f"task:{t.get('id')}", "task", "a task", "information",
                 f"A task has been open since {day(made)}: {str(t.get('text'))[:200]}", 0, 0)
    return out


RANK = {"information": 0, "warning": 1, "critical": 2}
# systemd's word for how a service is not running, as a person would say it.
SUB_SAID = {"dead": "stopped", "exited": "ended", "auto-restart": "being started again", "start-pre": "starting",
            "start": "starting", "start-post": "starting", "stop": "stopping", "stop-sigterm": "stopping",
            "stop-sigkill": "stopping", "stop-post": "stopping", "final-sigterm": "stopping"}


class Watch:
    """Which findings have become notices. A finding is raised once it has
    held without a break for as long as its rule asks, and cleared once it
    has been gone without a break for as long as its rule asks. So one that
    flickers below that never becomes a notice, and one that flickers while
    a notice is open leaves it open: one notice for a thing, not one a look."""

    def __init__(self) -> None:
        self.known: dict[str, dict] = {}

    def adopt(self, key: str, severity: str) -> None:
        """A notice that was open when this program started: watched from
        here on, so that it is cleared if what it says holds no longer."""
        self.known[key] = {"bad": None, "ok": None, "open": True, "severity": severity, "hold": 300}

    def unraised(self, key: str) -> None:
        """The notice could not be kept (the memory did not answer): it is
        raised at the next look, not after holding all over again."""
        if key in self.known:
            self.known[key]["open"] = False

    def uncleared(self, key: str) -> None:
        """The notice could not be cleared: it is still open, and is cleared
        at the next look that finds it still gone."""
        self.known[key] = {"bad": None, "ok": 0.0, "open": True, "severity": "warning", "hold": 0}

    def see(self, found: list[dict], now: float) -> tuple[list[dict], list[str]]:
        """Take a look's findings: the notices to raise (or to make worse)
        and the keys of those to clear."""
        raise_, clear, here = [], [], {f["key"]: f for f in found}
        for key, f in here.items():
            k = self.known.setdefault(key, {"bad": None, "ok": None, "open": False, "severity": "", "hold": 0})
            k["bad"], k["ok"], k["hold"] = k["bad"] if k["bad"] is not None else now, None, f["hold"]
            if not k["open"] and now - k["bad"] >= f["after"]:
                k.update(open=True, severity=f["severity"])
                raise_.append(f)
            elif k["open"] and RANK.get(f["severity"], 0) > RANK.get(k["severity"], 0):
                k["severity"] = f["severity"]
                raise_.append(f)
        for key in [k for k in self.known if k not in here]:
            k = self.known[key]
            k["bad"], k["ok"] = None, k["ok"] if k["ok"] is not None else now
            if not k["open"]:
                del self.known[key]               # it never became a notice
            elif now - k["ok"] >= k["hold"]:
                clear.append(key)
                del self.known[key]
        return raise_, clear


def fleet_seen() -> dict | None:
    """Mission Control's view of the fleet, or None when the Pi does not
    answer. The token goes in the address, which is written down nowhere."""
    try:
        with urllib.request.urlopen(FLEET_URL + ("&" if "?" in FLEET_URL else "?") + "token="
                                    + quote(FLEET_TOKEN), timeout=6) as r:
            got = json.loads(r.read(600_000))
        return got if isinstance(got, dict) and isinstance(got.get("machines"), list) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def look(watch: Watch, memo: dict, now: float | None = None) -> tuple[int, int]:
    """One look: read, hold against the rules, raise and clear. How many of
    each. What is raised is kept by the memory service; if that does not
    answer, the notice is tried again at the next look."""
    now = time.time() if now is None else now
    if now - memo["tasks"][0] > 600:              # his open tasks: asked for every ten minutes, not every look
        got = memory("list", {"kind": "task", "status": "active", "limit": 50}, WATCH_ASKS)[1]
        memo["tasks"] = (now, got.get("memories") or [] if got.get("ok") else memo["tasks"][1])
    found = findings(vitals(), fleet_seen() if FLEET_URL else None, memo["tasks"][1], now, memo)
    to_raise, to_clear = watch.see(found, now)
    raised = cleared = 0
    for f in to_raise:
        got = memory("notice_raise", f, WATCH_ASKS)[1]
        if got.get("ok"):
            raised += 1
            # The key, never the sentence: a task's words are not for the journal.
            print(f"douglas-agent: notice {got.get('what')}: {f['key']} ({f['severity']})", flush=True)
        else:
            watch.unraised(f["key"])
    for key in to_clear:
        if memory("notice_clear", {"key": key}, WATCH_ASKS)[1].get("ok"):
            cleared += 1
            print(f"douglas-agent: notice over: {key}", flush=True)
        else:
            watch.uncleared(key)
    return raised, cleared


def watch_forever() -> None:
    watch, memo = Watch(), {"restarts": {}, "sha": {}, "changed": {}, "tasks": (0.0, [])}
    for n in (memory("notices", {"limit": 200}, WATCH_ASKS)[1].get("open") or []):
        watch.adopt(str(n.get("key")), str(n.get("severity")))
    while True:
        try:
            look(watch, memo)
        except Exception as e:                    # noqa: BLE001 - the watch outlives a bad reading
            print(f"douglas-agent: the watch tripped: {type(e).__name__}", flush=True)
        time.sleep(WATCH_EVERY_S)


def vitals() -> dict:
    """This machine and what runs on it, as read just now. No conversation
    in it: this is what the vitals socket gives to whoever asks."""
    return {"ts": time.time(), "host": host_metrics(), "gpu": gpu_metrics(), "llm": llm_metrics(),
            "disks": disks(), "units": UNITS(), "models": MODELS(), "versions": VERSIONS()}


def status() -> dict:
    return dict(vitals(), voice=voice_state(), transcript=transcript())


def vitals_socket() -> socket.socket | None:
    """The socket the machine's vitals are given out at: the one systemd made
    and handed over (douglas-agent-vitals.socket), or, run by hand, one made
    here at VITALS_SOCKET."""
    names = (os.environ.get("LISTEN_FDNAMES") or "").split(":")
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and "vitals" in names:
        return socket.socket(fileno=3 + names.index("vitals"))
    if not VITALS_SOCKET:
        return None
    try:
        os.unlink(VITALS_SOCKET)
    except OSError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(VITALS_SOCKET)
    os.chmod(VITALS_SOCKET, 0o666)
    s.listen(16)
    return s


def serve_vitals(sock: socket.socket) -> None:
    """One reading for each connection, then it is closed. Nothing is read
    from the caller: there is nothing here to ask for, and nothing to change."""
    def give(conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            conn.sendall(json.dumps(vitals()).encode() + b"\n")
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            time.sleep(0.5)
            continue
        threading.Thread(target=give, args=(conn,), daemon=True).start()


# ── http ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "douglas-agent/1"

    def log_message(self, fmt, *args):     # journald gets errors, not polls
        pass

    def _send(self, code: int, obj, kind: str = "text/plain") -> None:
        body = (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if not isinstance(obj, bytes)
                         else kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        q = parse_qs(urlparse(self.path).query)
        return bool(TOKEN) and (q.get("token") or [""])[0] == TOKEN

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, b"ok")
        if path == "/status":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            return self._send(200, status())
        if path == "/speech":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            ask_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            code, out = speech(ask_id)
            return self._send(code, out, "audio/wav")
        if path == "/picture":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            code, out, kind = picture((parse_qs(urlparse(self.path).query).get("id") or [""])[0])
            return self._send(code, out, kind)
        if path == "/transcript":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            return self._send(200, transcript_before(q.get("before"), q.get("limit")))
        if path == "/memory":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            code, out = memory(q.get("op"), q, MEMORY_LOOKS)
            return self._send(code, out)
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/ptt", "/say", "/hear", "/memory", "/approval", "/switch"):
            return self._send(404, {"error": "not found"})
        if not self._authorised():
            return self._send(403, {"error": "forbidden"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0),
                    {"/say": SAY_BODY_MAX, "/hear": HEAR_BODY_MAX,
                     "/memory": MEMORY_BODY_MAX}.get(path, 16384))
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if path == "/memory":
            body = body if isinstance(body, dict) else {}
            code, out = memory(body.get("op"), body, MEMORY_CHANGES)
            return self._send(code, out)
        relay = {"/say": relay_say, "/hear": relay_hear, "/approval": relay_approval,
                 "/switch": relay_switch}.get(path, relay_ptt)
        code, out = relay(body if isinstance(body, dict) else {})
        return self._send(code, out)


def main() -> None:
    if not TOKEN:
        print("douglas-agent: DOUGLAS_AGENT_TOKEN is empty - every request "
              "but /health will be refused", flush=True)
    CPU.sample()                          # prime, so the first poll has an interval
    door = vitals_socket()
    if door is not None:
        threading.Thread(target=serve_vitals, args=(door,), daemon=True).start()
    watching = WATCH and bool(MEMORY_OWNER_SOCKET)
    if watching:
        threading.Thread(target=watch_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"douglas-agent: listening on :{PORT}, model at {LLM_URL}, vitals socket "
          f"{'on' if door is not None else 'off'}, watch {'on' if watching else 'off'}"
          + (", with the fleet" if watching and FLEET_URL else ""), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
