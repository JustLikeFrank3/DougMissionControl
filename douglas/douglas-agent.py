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
MEMORY_BODY_MAX = 64 * 1024              # a correction is a sentence or two
MEMORY_ANSWER_MAX = 240_000              # what the panel's relay will carry on
# What this machine is asked about itself, beyond the meters: the services
# worth knowing the health of, the file systems worth knowing the room on,
# where the model files are and where the programs are installed.
AGENT_UNITS = tuple((os.environ.get("AGENT_UNITS") or
                     "llama-server llama-eyes douglas-voice douglas-memory douglas-work douglas-job "
                     "douglas-tts douglas-draw douglas-agent douglas-chatgpt open-webui "
                     "wyoming-whisper wyoming-piper").split())
AGENT_DISKS = tuple((os.environ.get("AGENT_DISKS") or "/ /srv/models").split())
MODELS_DIR = os.environ.get("MODELS_DIR", "/srv/models")
PROGRAMS_DIR = os.environ.get("PROGRAMS_DIR", "/opt/douglas")
SYSTEMCTL = os.environ.get("SYSTEMCTL", "/usr/bin/systemctl")
VITALS_SOCKET = os.environ.get("VITALS_SOCKET", "")     # run by hand: the socket is made here
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
            "heard": heard if isinstance(heard, dict) else None}


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
        try:
            t = json.loads(line)
        except ValueError:
            continue
        if isinstance(t, dict) and t.get("text"):
            turn = {"ts": t.get("ts"),
                    "who": "user" if t.get("who") == "user" else "douglas",
                    "text": str(t["text"])[:2000],
                    "via": str(t.get("via") or "voice")}
            if PICTURE_ID.fullmatch(str(t.get("image") or "")):
                turn["image"] = t["image"]       # he drew one: GET /picture?id= has it
            turns.append(turn)
    return turns[-TRANSCRIPT_TURNS:]


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
                "events": ("limit", "kind"), "stats": ()}
MEMORY_CHANGES = {"correct": ("id", "text", "subject", "kind", "confidence", "done"),
                  "add": ("kind", "subject", "text"),
                  "delete": ("id",), "restore": ("id",), "purge": ("id",)}
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
        if path == "/memory":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            code, out = memory(q.get("op"), q, MEMORY_LOOKS)
            return self._send(code, out)
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/ptt", "/say", "/hear", "/memory"):
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
        relay = {"/say": relay_say, "/hear": relay_hear}.get(path, relay_ptt)
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
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"douglas-agent: listening on :{PORT}, model at {LLM_URL}, vitals socket "
          f"{'on' if door is not None else 'off'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
