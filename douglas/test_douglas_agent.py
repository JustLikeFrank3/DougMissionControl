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
LLAMA = {"health": 200, "model": {}, "props": {}, "slots": None}


class Llama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            code, body = LLAMA["health"], b'{"status":"ok"}'
        elif self.path == "/v1/models":
            code, body = 200, json.dumps({"data": [{"id": "/models/Qwen3-235B-A22B-Q3_K_M.gguf", **LLAMA["model"]}]}).encode()
        elif self.path == "/props":
            code, body = 200, json.dumps(dict({"default_generation_settings": {"n_ctx": 32768}}, **LLAMA["props"])).encode()
        elif self.path == "/slots" and LLAMA["slots"] is not None:
            code, body = 200, json.dumps(LLAMA["slots"]).encode()
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

print("-- the machine's own account of itself: disks, services, model files, what is installed --")
import socket      # noqa: E402
import subprocess  # noqa: E402

LLAMA["health"] = 200
check("an older model server, which says less, is not made to say more",
      [agent.llm_metrics().get(k) for k in ("slots", "build", "vision", "busy_slots", "params")], [None] * 5)
LLAMA["model"] = {"meta": {"n_params": 123_611_033_088, "size": 70_095_370_240}}
LLAMA["props"] = {"total_slots": 2, "build_info": "b12600-abc", "modalities": {"vision": True},
                  "model_path": "/srv/models/a/the-model-00001-of-00002.gguf"}
# What a slot holds is the conversation in it: only whether it is in use may come out.
LLAMA["slots"] = [{"id": 0, "is_processing": True, "prompt": "my sister Ann lives in Leeds", "next_token": {}},
                  {"id": 1, "is_processing": False}]
m = agent.llm_metrics()
check("how big the model is, how many conversations it holds at once and how many are in use",
      [m.get(k) for k in ("params", "size_bytes", "slots", "busy_slots", "build", "vision", "file")],
      [123_611_033_088, 70_095_370_240, 2, 1, "b12600-abc", True, "the-model-00001-of-00002.gguf"])
check("and nothing of what is in a slot", "Leeds" in json.dumps(m), False)
write(proc, "loadavg", "0.42 0.30 0.25 1/900 12345\n")
write(proc, "cpuinfo", "processor\t: 0\nmodel name\t: AMD Ryzen AI Max+ 395 w/ Radeon 8060S\n")
agent.CPU_NAME.at = 0.0
h = agent.host_metrics()
check("the load, the processor and how many threads it has", (h["load"], h["cpu"], h["cores"] == os.cpu_count(),
                                                             bool(h["kernel"])),
      (0.42, "AMD Ryzen AI Max+ 395 w/ Radeon 8060S", True, True))
agent.AGENT_DISKS = ("/", "/", os.path.join(tmp, "nowhere"))
dk = agent.disks()
check("each file system once, with its room, and one that is not there left out",
      (len(dk), dk[0]["path"], dk[0]["total_bytes"] >= dk[0]["used_bytes"] > 0, dk[0]["free_bytes"] > 0),
      (1, "/", True, True))
models_dir = os.path.join(tmp, "models")
for rel, size in (("big/the-model-00001-of-00002.gguf", 300 << 20), ("draw/flux.safetensors", 200 << 20),
                  ("kokoro/voice.onnx", 150 << 20), ("big/config.json", 4096), ("eyes/tiny.gguf", 5 << 20),
                  ("a/b/c/d/too-deep.gguf", 400 << 20), ("fetch.sh", 100)):
    write(models_dir, rel, "")
    os.truncate(os.path.join(models_dir, rel), size)         # takes no room: only its size is read
agent.MODELS_DIR = models_dir
check("the model files on the disk, biggest first: not what merely sits beside one, and not a whole disk walked",
      agent._models(), [{"file": "big/the-model-00001-of-00002.gguf", "bytes": 300 << 20},
                        {"file": "draw/flux.safetensors", "bytes": 200 << 20},
                        {"file": "kokoro/voice.onnx", "bytes": 150 << 20}])
programs = os.path.join(tmp, "opt")
write(programs, "douglas-voice.py", "print('one')\n")
write(programs, "douglas-agent.py", "print('two')\n")
write(programs, "notes.txt", "not a program")
agent.PROGRAMS_DIR = programs
v = agent._versions()
check("what is installed, each with when and the start of its checksum",
      ([x["file"] for x in v], [len(x["sha"]) for x in v], v[0]["sha"] != v[1]["sha"],
       all(abs(x["installed"] - time.time()) < 60 for x in v)),
      (["douglas-agent.py", "douglas-voice.py"], [12, 12], True, True))
ASKED = []
SHOWN = {"out": (
    "Id=llama-server.service\nLoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=0\n"
    "ActiveEnterTimestampMonotonic=600000000\nResult=success\n\n"
    "Id=douglas-voice.service\nLoadState=loaded\nActiveState=activating\nSubState=auto-restart\nNRestarts=7\n"
    "ActiveEnterTimestampMonotonic=0\nResult=exit-code\n\n"
    "Id=llama-eyes.service\nLoadState=loaded\nActiveState=inactive\nSubState=dead\nNRestarts=0\n"
    "ActiveEnterTimestampMonotonic=0\nResult=success\nUnitFileState=disabled\n\n"
    "Id=open-webui.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nNRestarts=0\n"
    "ActiveEnterTimestampMonotonic=0\nResult=success\n")}


def systemd(argv, **kw):
    ASKED.append(argv)
    return subprocess.CompletedProcess(argv, 0, SHOWN["out"], "")


real_run, agent.subprocess.run = agent.subprocess.run, systemd
agent.AGENT_UNITS = ("llama-server", "douglas-voice", "llama-eyes", "open-webui")
u = agent._units()
check("his services: up, coming back up again and again, or off; one that is not installed is not listed",
      [(x["unit"], x["state"], x["sub"], x["restarts"]) for x in u],
      [("llama-server", "active", "running", 0), ("douglas-voice", "activating", "auto-restart", 7),
       ("llama-eyes", "inactive", "dead", 0)])
check("and whether one that is not running is meant to be: switched off is not gone down",
      [x["enabled"] for x in u], [None, None, "disabled"])
check("since when, for one that is up: the machine has been up an hour and it came up ten minutes in",
      (abs(time.time() - u[0]["since"] - 3000.5) < 2, u[1]["since"], u[2]["since"]), (True, None, None))
check("asked of systemd with one fixed command, made of this program's own list",
      ASKED[0], ["/usr/bin/systemctl", "show", "llama-server.service", "douglas-voice.service",
                 "llama-eyes.service", "open-webui.service", "-p",
                 "Id,LoadState,ActiveState,SubState,NRestarts,ActiveEnterTimestampMonotonic,Result,UnitFileState"])
agent.UNITS.at = 0.0
for _ in range(5):
    agent.UNITS()
check("and not for every poll of the panel", len(ASKED), 2)


def no_systemd(argv, **kw):
    raise OSError("no such file")


agent.subprocess.run = no_systemd
check("systemd out of reach is not known, which is not the same as nothing running", agent._units(), None)
agent.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
    argv, 1, "", "Failed to get properties: Transport endpoint is not connected")
check("nor is systemd refusing to say", agent._units(), None)
agent.subprocess.run = systemd
for kept in (agent.UNITS, agent.MODELS, agent.VERSIONS):
    kept.at = 0.0
with open(transcript_file, "w") as f:
    f.write(json.dumps({"ts": 1, "who": "user", "text": "my sister Ann lives in Leeds", "via": "phone"}) + "\n")
vt = agent.vitals()
check("the vitals: the machine, the model, the disks, the services, the model files, what is installed",
      (sorted(vt), len(vt["units"]), len(vt["models"]), len(vt["versions"]), len(vt["disks"])),
      (["disks", "gpu", "host", "llm", "models", "ts", "units", "versions"], 3, 3, 2, 1))
st = agent.status()
check("the panel's frame is those and the conversation; the vitals are those without it",
      (sorted(set(st) - set(vt)), st["transcript"][0]["text"], "Leeds" in json.dumps(vt), "voice" in vt),
      (["transcript", "voice"], "my sister Ann lives in Leeds", False, False))

agent.VITALS_SOCKET = os.path.join(tmp, "vitals.sock")
door = agent.vitals_socket()
threading.Thread(target=agent.serve_vitals, args=(door,), daemon=True).start()


def read_vitals(send=b""):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(agent.VITALS_SOCKET)
    if send:
        c.sendall(send)
    with c.makefile("rb") as f:
        line = f.readline()
    c.close()
    return json.loads(line)


got = read_vitals()
check("at the vitals socket: one reading for whoever connects, and no conversation in it",
      (sorted(got) == sorted(vt), got["llm"]["model"], "transcript" in got, "Leeds" in json.dumps(got),
       oct(os.stat(agent.VITALS_SOCKET).st_mode & 0o777)), (True, "Qwen3-235B-A22B-Q3_K_M", False, False, "0o666"))
got = read_vitals(b'{"op": "status", "token": "t", "path": "/etc/shadow"}\nGET /status?token=t HTTP/1.0\r\n\r\n')
check("whatever is sent to it is not read: the same reading comes back, and nothing more",
      (sorted(got) == sorted(vt), "transcript" in got), (True, False))
os.environ.update({"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1", "LISTEN_FDNAMES": "other"})
agent.VITALS_SOCKET = ""
check("with neither a socket handed over under that name nor a path, there is none", agent.vitals_socket(), None)
for k in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
    del os.environ[k]
agent.subprocess.run = real_run

print("-- what he remembers, for the owner --")
# The real memory service, with both its doors, and the agent holding the
# key to the owner's.
import urllib.error      # noqa: E402
import urllib.request    # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

spec = importlib.util.spec_from_file_location("memsvc", os.path.join(here, "douglas-memory.py"))
memsvc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memsvc)
desk = memsvc.Desk(memsvc.Store(os.path.join(tmp, "memory.db")))
doors = {}
for role in ("assistant", "owner"):
    door = memsvc.Server(os.path.join(tmp, role + ".sock"), memsvc.Door)
    door.role, door.desk = role, desk
    threading.Thread(target=door.serve_forever, daemon=True).start()
    doors[role] = door
first = desk.assistant("remember", {"kind": "preference", "subject": "coffee", "source": "said",
                                    "text": "Sam takes his coffee black."})["id"]
desk.assistant("log", {"kind": "tool", "name": "weather", "action": "the person's asking", "ms": 412})

check("no memory installed is said, not an error",
      agent.memory("list", {}, agent.MEMORY_LOOKS), (200, {"ok": False, "text": "Douglas has no memory installed."}))
agent.MEMORY_OWNER_SOCKET = os.path.join(tmp, "owner.sock")
code, got = agent.memory("list", {"limit": "5000", "junk": "x"}, agent.MEMORY_LOOKS)
check("the owner's list", (code, got["ok"], got["total"], got["memories"][0]["text"], got["memories"][0]["source"]),
      (200, True, 1, "Sam takes his coffee black.", "said"))
check("the record of what he did", [(e["name"], e["ms"]) for e in agent.memory("events", {}, agent.MEMORY_LOOKS)[1]["events"]],
      [("weather", 412)])
check("a change is not a look", agent.memory("purge", {"id": first}, agent.MEMORY_LOOKS)[0], 400)
check("the whole memory at once is not passed on", agent.memory("export", {}, agent.MEMORY_LOOKS)[0], 400)
check("nor are the assistant's questions", agent.memory("recall", {"text": "coffee"}, agent.MEMORY_CHANGES)[0], 400)
check("nor a question that is not a word", agent.memory(["list"], {}, agent.MEMORY_LOOKS)[0], 400)
code, got = agent.memory("correct", {"id": first, "text": "Sam takes his coffee with milk.", "status": "deleted",
                                     "source": "inferred", "text2": ["x"]}, agent.MEMORY_CHANGES)
check("a correction is the owner's word from then on, and only what a correction may carry is passed on",
      (got["text"], got["source"], got["status"], got["history"][0]["action"]),
      ("Sam takes his coffee with milk.", "owner", "active", "corrected"))
check("what is not one plain value is not passed on",
      agent.memory("correct", {"id": [first], "text": {"a": 1}}, agent.MEMORY_CHANGES)[1]["ok"], False)

# What goes through the door, seen from the other side: a stand-in for the
# memory that writes down exactly what it was asked.
ARRIVED = []
ear = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
ear.bind(os.path.join(tmp, "ear.sock"))
ear.listen(4)


def listen():
    while True:
        try:
            c, _ = ear.accept()
        except OSError:
            return
        with c.makefile("rb") as f:
            ARRIVED.append(json.loads(f.readline()))
        c.sendall(b'{"ok": true}\n')
        c.close()


threading.Thread(target=listen, daemon=True).start()
real_door, agent.MEMORY_OWNER_SOCKET = agent.MEMORY_OWNER_SOCKET, os.path.join(tmp, "ear.sock")
agent.memory("correct", {"id": 7, "text": "x" * 9000, "status": "deleted", "source": "inferred", "who": "owner",
                         "op": "purge", "ids": [1, 2], "confidence": 0.5, "done": True, "limit": 5},
             agent.MEMORY_CHANGES)
agent.memory("list", {"limit": "100000", "q": "coffee", "id": 7, "text": "x", "export": 1}, agent.MEMORY_LOOKS)
agent.memory("events", {"limit": "many"}, agent.MEMORY_LOOKS)
check("what reaches the memory is the question that was allowed and the fields that question may carry, each "
      "cut to size: nothing else a caller put in travels",
      [dict(x, text=len(x["text"])) if "text" in x else x for x in ARRIVED],
      [{"op": "correct", "id": 7, "text": 4000, "confidence": 0.5, "done": True},
       {"op": "list", "q": "coffee", "limit": 50}, {"op": "events", "limit": 200}])
agent.MEMORY_OWNER_SOCKET = real_door
ear.close()

web = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
threading.Thread(target=web.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{web.server_address[1]}"


def call(path, body=None):
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


check("without the token, nothing", (call("/memory?op=list")[0], call("/memory", {"op": "purge", "id": first})[0],
                                     call("/memory?token=wrong&op=list")[0]), (403, 403, 403))
code, got = call("/memory?token=t&op=list&q=coffee&status=active")
check("with it, the list", (code, [m["text"] for m in got["memories"]]), (200, ["Sam takes his coffee with milk."]))
check("a GET cannot change anything", (call(f"/memory?token=t&op=delete&id={first}")[0],
                                       call(f"/memory?token=t&op=purge&id={first}")[0],
                                       desk.store.get(first)["status"]), (400, 400, "active"))
check("a POST cannot be made to ask what only the assistant asks",
      (call("/memory?token=t", {"op": "remember", "kind": "fact", "text": "The sky is green."})[0],
       call("/memory?token=t", {"op": "log", "name": "x"})[0], call("/memory?token=t", {})[0],
       call("/memory?token=t", ["purge"])[0]), (400, 400, 400, 400))
code, got = call("/memory?token=t", {"op": "delete", "id": first})
check("deleted from the owner's screen", (code, got["status"], desk.assistant("recall", {"text": "coffee"})["memories"]),
      (200, "deleted", []))
code, got = call("/memory?token=t", {"op": "restore", "id": first})
check("and brought back", (got["status"], len(desk.assistant("recall", {"text": "coffee"})["memories"])), ("active", 1))
check("a memory that is not there is said so, and the agent stays up",
      (call("/memory?token=t&op=get&id=999")[1], call("/memory?token=t&op=get&id=x")[1]["ok"]),
      ({"ok": False, "text": "There is no such memory."}, False))
code, got = call("/memory?token=t", {"op": "add", "kind": "fact", "subject": "the door code",
                                     "text": "The password is hunter2hunter2."})
check("a secret is refused at the door whoever brings it", (got["ok"], "password" in got["text"]), (False, True))
call("/memory?token=t", {"op": "purge", "id": first})
check("purged, it is gone", (call(f"/memory?token=t&op=get&id={first}")[1]["ok"],
                             call("/memory?token=t&op=stats")[1]["active"]), (False, 0))
doors["owner"].shutdown()
doors["owner"].server_close()
check("the memory gone quiet is said, and is no error of the agent's",
      call("/memory?token=t&op=stats"), (200, {"ok": False, "text": "Douglas's memory did not answer."}))
web.shutdown()
doors["assistant"].shutdown()

srv.shutdown()
print("douglas-agent tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
