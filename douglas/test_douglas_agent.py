#!/usr/bin/env python3
"""douglas-agent against a fake /proc, /sys and llama.cpp. Runs anywhere.

    python3 douglas/test_douglas_agent.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


# ── a fake llama.cpp, whose /health answer the test controls ──────────────
LLAMA = {"health": 200}


class Llama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            code, body = LLAMA["health"], b'{"status":"ok"}'
        elif self.path == "/v1/models":
            code, body = 200, json.dumps({"data": [{"id": "/models/Qwen3-235B-A22B-Q3_K_M.gguf"}]}).encode()
        elif self.path == "/props":
            code, body = 200, b'{"default_generation_settings":{"n_ctx":32768}}'
        elif self.path == "/metrics":
            code, body = 200, (b"# HELP x\nllamacpp:prompt_tokens_seconds 412.5\n"
                               b"llamacpp:predicted_tokens_seconds 23.25\n"
                               b"llamacpp:requests_processing 1\nllamacpp:requests_deferred 0\n")
        else:
            code, body = 404, b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = HTTPServer(("127.0.0.1", 0), Llama)
threading.Thread(target=srv.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp()
proc, sysfs = os.path.join(tmp, "proc"), os.path.join(tmp, "sys")
write(proc, "stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
write(proc, "meminfo", "MemTotal:       134217728 kB\nMemAvailable:    67108864 kB\n")
write(proc, "uptime", "3600.5 7000.0\n")
card = "class/drm/card1/device"
write(sysfs, f"{card}/vendor", "0x1002\n")
write(sysfs, f"{card}/gpu_busy_percent", "37\n")
write(sysfs, f"{card}/mem_info_vram_used", str(512 << 20))
write(sysfs, f"{card}/mem_info_vram_total", str(512 << 20))
write(sysfs, f"{card}/mem_info_gtt_used", str(80 << 30))
write(sysfs, f"{card}/mem_info_gtt_total", str(110 << 30))
write(sysfs, "class/hwmon/hwmon0/name", "amdgpu\n")
write(sysfs, "class/hwmon/hwmon0/power1_average", "98500000\n")
write(sysfs, "class/hwmon/hwmon0/temp1_input", "61000\n")
write(sysfs, "class/hwmon/hwmon1/name", "k10temp\n")
write(sysfs, "class/hwmon/hwmon1/temp1_input", "72500\n")
voice_file = os.path.join(tmp, "voice.json")
transcript_file = os.path.join(tmp, "transcript.jsonl")

os.environ.update({
    "PROCFS_ROOT": proc, "SYSFS_ROOT": sysfs,
    "LLM_URL": f"http://127.0.0.1:{srv.server_address[1]}",
    "VOICE_STATE_FILE": voice_file, "TRANSCRIPT_FILE": transcript_file,
    "TRANSCRIPT_TURNS": "3", "DOUGLAS_AGENT_TOKEN": "t", "VOICE_PTT_URL": "",
})
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("agent", os.path.join(here, "douglas-agent.py"))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

print("-- machine --")
h = agent.host_metrics()
check("cpu first sample is None, not 0", h["cpu_pct"], None)
write(proc, "stat", "cpu  200 0 200 900 0 0 0 0 0 0\n")
check("cpu busy over the interval", agent.host_metrics()["cpu_pct"], 66.7)
check("mem used = total - available", h["mem_used_bytes"], 64 << 30)
check("k10temp preferred over amdgpu edge", h["temp_c"], 72.5)
check("socket power in watts", h["power_w"], 98.5)
g = agent.gpu_metrics()
check("gpu busy", g["busy_pct"], 37)
check("gtt reported", g["gtt_used_bytes"], 80 << 30)

print("-- model server --")
m = agent.llm_metrics()
check("running", m["state"], "running")
check("model stem", m["model"], "Qwen3-235B-A22B-Q3_K_M")
check("context", m["ctx"], 32768)
check("gen tok/s", m["gen_tps"], 23.25)
check("requests processing", m["processing"], 1)
LLAMA["health"] = 503
check("503 reads as loading", agent.llm_metrics()["state"], "loading")
LLAMA["health"] = 500
check("other errors read as stopped", agent.llm_metrics()["state"], "stopped")

print("-- conversation --")
check("no state file = absent", agent.voice_state()["state"], "absent")
with open(voice_file, "w") as f:
    json.dump({"state": "listening", "satellite": True, "ts": time.time() - 300}, f)
check("stale heartbeat = absent", agent.voice_state()["state"], "absent")
with open(voice_file, "w") as f:
    json.dump({"state": "listening", "satellite": True, "ts": time.time()}, f)
check("fresh heartbeat", agent.voice_state()["state"], "listening")
check("no transcript file = empty", agent.transcript(), [])
with open(transcript_file, "w") as f:
    for i in range(5):
        f.write(json.dumps({"ts": i, "who": "user" if i % 2 == 0 else "douglas",
                            "text": f"turn {i}", "via": "voice"}) + "\n")
    f.write("not json\n")
t = agent.transcript()
check("last N turns, bad line skipped", [x["text"] for x in t], ["turn 2", "turn 3", "turn 4"])
check("ptt without a pipeline says so",
      agent.relay_ptt({"action": "start"}),
      (200, {"ok": False, "message": "no voice pipeline installed on Douglas yet"}))
check("ptt rejects unknown action", agent.relay_ptt({"action": "x"})[0], 400)

srv.shutdown()
print("douglas-agent tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
