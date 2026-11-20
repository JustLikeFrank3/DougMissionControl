#!/usr/bin/env python3
"""douglas-agent — Douglas's one window for the Flight Deck panel.

Runs ON DOUGLAS (the GEEKOM A9 Mega inference box), installed by
douglas/setup.sh as the douglas-agent systemd unit. Stdlib only.

    GET  /status?token=<t>   one JSON: host, GPU and model-server metrics,
                             voice-pipeline state, the recent transcript
    POST /ptt?token=<t>      {"action":"start|stop|cancel","via":"deck"}
                             relayed to the voice pipeline's own PTT endpoint
    GET  /health             200 "ok" — liveness, no token, no data

Everything it reports is read at request time from where it already lives:
/proc and /sys for the machine, llama.cpp's /health, /metrics, /props and
/v1/models for the model, and two files the voice pipeline owns for the
conversation. It invents nothing: a value it cannot read is null, never 0,
because "idle" and "unknown" are different facts on the panel.

The voice pipeline contract (whatever implements DougVoice's brain):
    VOICE_STATE_FILE   JSON {"state": "idle|listening|thinking|speaking",
                             "satellite": bool, "ts": <epoch>, "last_turn": <epoch>}
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
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("DOUGLAS_AGENT_PORT", "9111"))
TOKEN = os.environ.get("DOUGLAS_AGENT_TOKEN", "")
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8081").rstrip("/")
VOICE_STATE_FILE = os.environ.get("VOICE_STATE_FILE", "/run/douglas/voice.json")
VOICE_STALE_S = float(os.environ.get("VOICE_STALE_S", "30"))
TRANSCRIPT_FILE = os.environ.get("TRANSCRIPT_FILE", "/var/lib/douglas/transcript.jsonl")
TRANSCRIPT_TURNS = int(os.environ.get("TRANSCRIPT_TURNS", "40"))
VOICE_PTT_URL = os.environ.get("VOICE_PTT_URL", "")
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
    return {
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
        except (ValueError, KeyError, IndexError, AttributeError):
            pass
    code, body = _get("/props")
    if code == 200:
        try:
            props = json.loads(body)
            out["ctx"] = (props.get("default_generation_settings") or {}).get("n_ctx")
        except (ValueError, AttributeError):
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
    return {"state": str(v.get("state") or "absent"),
            "satellite": bool(v.get("satellite")),
            "last_turn": v.get("last_turn")}


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
            turns.append({"ts": t.get("ts"),
                          "who": "user" if t.get("who") == "user" else "douglas",
                          "text": str(t["text"])[:2000],
                          "via": str(t.get("via") or "voice")})
    return turns[-TRANSCRIPT_TURNS:]


def relay_ptt(body: dict) -> tuple[int, dict]:
    action = str(body.get("action") or "")
    if action not in ("start", "stop", "cancel"):
        return 400, {"ok": False, "message": "action must be start, stop or cancel"}
    if not VOICE_PTT_URL:
        return 200, {"ok": False, "message": "no voice pipeline installed on Douglas yet"}
    req = urllib.request.Request(
        VOICE_PTT_URL, method="POST",
        data=json.dumps({"action": action, "via": str(body.get("via") or "deck")}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            ok = 200 <= r.status < 300
    except urllib.error.HTTPError as e:     # it answered, and said no
        try:
            why = json.loads(e.read(4096)).get("message")
        except (ValueError, AttributeError, OSError):
            why = None
        return 200, {"ok": False, "message": str(why or "the voice pipeline refused")}
    except (urllib.error.URLError, OSError, ValueError):
        return 200, {"ok": False, "message": "the voice pipeline did not answer"}
    return 200, {"ok": ok, "message": action if ok else "the voice pipeline refused"}


def status() -> dict:
    return {"ts": time.time(), "host": host_metrics(), "gpu": gpu_metrics(),
            "llm": llm_metrics(), "voice": voice_state(),
            "transcript": transcript()}


# ── http ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "douglas-agent/1"

    def log_message(self, fmt, *args):     # journald gets errors, not polls
        pass

    def _send(self, code: int, obj) -> None:
        body = (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if not isinstance(obj, bytes)
                         else "text/plain")
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
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/ptt":
            return self._send(404, {"error": "not found"})
        if not self._authorised():
            return self._send(403, {"error": "forbidden"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        code, out = relay_ptt(body if isinstance(body, dict) else {})
        return self._send(code, out)


def main() -> None:
    if not TOKEN:
        print("douglas-agent: DOUGLAS_AGENT_TOKEN is empty - every request "
              "but /health will be refused", flush=True)
    CPU.sample()                          # prime, so the first poll has an interval
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"douglas-agent: listening on :{PORT}, model at {LLM_URL}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
