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

print("-- what is waiting for a yes, and the owner's switches --")
WAITING = {"id": "a1b2c3d4", "kind": "push", "text": "This would push 2 commits to kettle.", "via": "webui",
           "asked": 1_800_000_000.0, "until": 1_800_000_180.0}
with open(voice_file, "w") as f:
    json.dump({"state": "idle", "ts": time.time(), "pending": dict(WAITING, extra="x" * 5000),
               "tools": {"search": "on", "repo": "off", "draw": "absent", "shell": "enabled", "memory": 1},
               "job": {"running": True, "what": "Fix the kettle timer. " * 40, "began": 1_800_000_000.0, "ended": None,
                       "log": "x" * 5000}}, f)
got = agent.voice_state()
check("the job in his workshop is passed on as that there is one, what it was asked to do, cut short, and since when",
      (got["job"]["running"], len(got["job"]["what"]), got["job"]["began"], got["job"]["ended"], sorted(got["job"])),
      (True, 300, 1_800_000_000.0, None, ["began", "ended", "report", "running", "what"]))
check("with no report of it, none is passed on", got["job"]["report"], None)
with open(voice_file, "w") as f:
    json.dump({"state": "idle", "ts": time.time(), "job": {
        "running": False, "what": "Implement those improvements.", "began": 1.0, "ended": 2.0, "report": {
            "id": "875c0c1f" + "x" * 50, "repo": "acme/kettle", "branch": "douglas/x-875c0c", "state": "done",
            "outcome": "splendid", "asked": "Implement those improvements. " * 200, "base_branch": "main",
            "before": ["Look into it.", 7], "context": [{"from": "the report of job aa11bb22", "text": "y" * 9000},
                                                        "not one"],
            "needs": [{"n": 1, "text": "Add a test", "accept": "a test passes", "state": "done", "note": ""},
                      {"n": "two", "text": "Publish it", "state": "triumphant", "note": "z" * 900}] + [{}] * 30,
            "files": ["boil.py", 9], "checks": [{"what": "syntax", "ok": "yes"}],
            "ran": [{"cmd": "pytest", "code": 1}, {"cmd": "true", "code": True}], "looked": "maybe",
            "summary": "s" * 9000, "secret": "x" * 5000}}}, f)
rep = agent.voice_state()["job"]["report"]
check("what a job was handed and what became of it is passed on in the shape it is known to have: each piece cut "
      "to size, a requirement in one of its states and no other, and nothing it has no name for",
      (rep["id"], rep["outcome"], len(rep["asked"]), rep["before"], len(rep["context"]), len(rep["context"][0]["text"]),
       [(n["n"], n["state"], len(n["note"])) for n in rep["needs"][:2]], len(rep["needs"]), rep["files"],
       rep["checks"], rep["ran"], rep["looked"], len(rep["summary"]), "secret" in rep),
      ("875c0c1f", "", 1200, ["Look into it."], 1, 1500, [(1, "done", 0), (0, "outstanding", 240)], 12, ["boil.py"],
       [{"what": "syntax", "ok": False, "said": ""}], [{"cmd": "pytest", "code": 1}, {"cmd": "true", "code": None}],
       "", 1500, False))
check("the change that is waiting is passed to the owner's screens in the shape it is known to have, and each tool "
      "as on, off or not set up and nothing else", (got["pending"], got["tools"]),
      (WAITING, {"search": "on", "repo": "off", "draw": "absent"}))
with open(voice_file, "w") as f:
    json.dump({"state": "idle", "ts": time.time(), "pending": {"text": "No number."}, "tools": ["search"]}, f)
check("what is not in that shape is not passed on at all",
      (agent.voice_state()["pending"], agent.voice_state()["tools"], agent.voice_state()["job"]), (None, None, None))
check("an answer needs the number of what it answers and a true or a false: nothing else is passed to the pipeline",
      [agent.relay_approval(b)[0] for b in ({}, {"id": "a1b2c3d4"}, {"id": "a1b2c3d4", "yes": "true"},
                                             {"id": "a1b2c3d4", "yes": 1}, {"id": 7, "yes": True}, {"id": "", "yes": True})],
      [400] * 6)
check("and with no pipeline it says so", agent.relay_approval({"id": "a1b2c3d4", "yes": True}),
      (200, {"ok": False, "message": "no voice pipeline installed on Douglas yet"}))


class Pipe(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Pipe.seen.append((self.path.split("?")[0], body))
        ok = body.get("id") == "a1b2c3d4"
        out = json.dumps({"ok": ok, "message": "approved" if ok else "That is no longer waiting for an answer."}).encode()
        self.send_response(200 if ok else 409)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


pipe = HTTPServer(("127.0.0.1", 0), Pipe)
threading.Thread(target=pipe.serve_forever, daemon=True).start()
agent.VOICE_PTT_URL = f"http://127.0.0.1:{pipe.server_address[1]}/ptt?token=p"
check("a yes given on the phone goes to the pipeline with its number, as a yes from the phone; a no as a no; and "
      "what the pipeline says of a number it does not know comes back as it said it",
      (agent.relay_approval({"id": "a1b2c3d4", "yes": True, "via": "phone", "extra": "x"}),
       agent.relay_approval({"id": "a1b2c3d4", "yes": False, "via": "somewhere"}),
       agent.relay_approval({"id": "zzzz", "yes": True}), Pipe.seen),
      ((200, {"ok": True, "message": "approved"}), (200, {"ok": True, "message": "refused"}),
       (200, {"ok": False, "message": "That is no longer waiting for an answer."}),
       [("/approve", {"id": "a1b2c3d4", "yes": True, "via": "phone"}), ("/approve", {"id": "a1b2c3d4", "yes": False, "via": "deck"}),
        ("/approve", {"id": "zzzz", "yes": True, "via": "deck"})]))
pipe.shutdown()
agent.VOICE_PTT_URL = ""
check("a switch is a tool and a true or a false; without the switches installed it says so",
      ([agent.relay_switch(b)[0] for b in ({}, {"tool": "search"}, {"tool": "search", "on": "off"}, {"tool": 3, "on": True},
                                           {"tool": "search\ndouglas-agent: all is well", "on": True},
                                           {"tool": "Search Engine", "on": True})],
       agent.relay_switch({"tool": "search", "on": False})),
      ([400] * 6, (200, {"ok": False, "message": "the switches are not installed on Douglas"})))
import socket as _socket      # noqa: E402
sdir = tempfile.mkdtemp(dir=os.environ.get("XDG_RUNTIME_DIR") or None)
spath, SWITCHED = os.path.join(sdir, "switch.sock"), []
sdoor = _socket.socket(_socket.AF_UNIX)
sdoor.bind(spath)
sdoor.listen(4)


def switches():
    while True:
        c, _ = sdoor.accept()
        with c, c.makefile("rb") as f:
            q = json.loads(f.readline())
            SWITCHED.append(q)
            known = q.get("tool") in ("search", "repo")
            c.sendall(json.dumps({"ok": True, "off": ["search"]} if known else
                                 {"ok": False, "text": "That is not something of his that can be switched."}).encode() + b"\n")


threading.Thread(target=switches, daemon=True).start()
agent.SWITCH_SOCKET = spath
import contextlib as _ctx, io as _io      # noqa: E401, E402
said_by_agent = _io.StringIO()
with _ctx.redirect_stdout(said_by_agent):
    got = (agent.relay_switch({"tool": "search", "on": False, "op": "write", "text": "x"}),
           agent.relay_switch({"tool": "shell", "on": True}))
check("it is passed to the one program that writes it down, as one question and nothing added to it; what that says "
      "comes back, a refusal as a refusal",
      (got, SWITCHED),
      (((200, {"ok": True, "message": "off", "off": ["search"]}),
        (200, {"ok": False, "message": "That is not something of his that can be switched.", "off": None})),
       [{"op": "set", "tool": "search", "on": False}, {"op": "set", "tool": "shell", "on": True}]))
check("and the journal has which and which way", said_by_agent.getvalue().splitlines(),
      ["douglas-agent: search switched off from a screen", "douglas-agent: shell switched on from a screen, refused"])
agent.SWITCH_SOCKET = os.path.join(sdir, "nobody.sock")
check("the switches not answering is said", agent.relay_switch({"tool": "search", "on": True}),
      (200, {"ok": False, "message": "the switches did not answer"}))
agent.SWITCH_SOCKET = ""

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

print("-- the watch: rules over readings, and the notices they raise --")
NOWW = 1_800_000_000.0
agent.FLEET_URL = "http://pi.example/api/fleet"
memo = lambda: {"restarts": {}, "sha": {}, "changed": {}, "tasks": (0.0, [])}      # noqa: E731
unit = lambda name, state="active", **more: dict({"unit": name, "state": state, "sub": "running" if state == "active" else state, "restarts": 0}, **more)   # noqa: E731
V = {"host": {"temp_c": 95.2}, "disks": [{"path": "/", "total_bytes": 1000 << 30, "free_bytes": 40 << 30}],
     "units": [unit("llama-server", "failed"), unit("douglas-draw", "failed"), unit("douglas-tts"),
               unit("llama-eyes", "inactive", enabled="disabled")],
     "versions": [{"file": "douglas-voice.py", "sha": "aaa"}]}
FLEET = {"machines": [
    {"id": "node1", "name": "DOUG PRIME", "up": True, "reports": {"temp_c": 76.0, "disk": {
        "path": "/", "total_bytes": 28 << 30, "free_bytes": 6 << 30}}, "services": [unit("deck-api"), unit("fauxmo", "failed")]},
    {"id": "workstation", "name": "DOUG DUALBOOT", "up": True, "reports": {"gpu_temp_c": 93}},
    {"id": "dougvoice", "name": "DOUGVOICE", "up": False}, {"id": "mini", "name": "MINIDOUG4", "up": False},
    {"id": "mobile", "name": "DOUGMOBILE", "up": False}]}
TASKS = [{"id": 3, "kind": "task", "text": "Order a battery for the garage sensor.", "created": NOWW - 9 * 86400},
         {"id": 4, "kind": "task", "text": "Done already.", "created": NOWW - 30 * 86400, "done": NOWW - 86400},
         {"id": 5, "kind": "task", "text": "Call the dentist.", "created": NOWW - 3600, "due": NOWW - 60},
         {"id": 6, "kind": "task", "text": "New today.", "created": NOWW - 3600}]
got = {f["key"]: f for f in agent.findings(V, FLEET, TASKS, NOWW, memo())}
check("what the rules find in one look: his own machine hot, its disk nearly full, a service down (and which "
      "matters most); the Pi warm with a service down; the workstation's card too hot; the satellite gone; two tasks",
      {k: f["severity"] for k, f in got.items()},
      {"hot:douglas": "warning", "disk:douglas:/": "critical", "unit:douglas:llama-server": "critical",
       "unit:douglas:douglas-draw": "warning", "hot:node1": "warning", "unit:node1:fauxmo": "warning",
       "hot:workstation": "critical", "gone:dougvoice": "warning", "task:3": "information", "task:5": "information"})
check("each in a sentence with the reading in it",
      [got[k]["text"] for k in ("hot:douglas", "disk:douglas:/", "unit:douglas:llama-server", "gone:dougvoice",
                                "hot:workstation", "task:3", "task:5")],
      ["DOUGLAS is running hot: 95 degrees C when this was raised, over its line of 92.",
       "The disk / on DOUGLAS is nearly full: 40.0 GB left (96% full) when this was raised.",
       "llama-server on DOUGLAS is not running (failed).", "DOUGVOICE is not answering.",
       "DOUG DUALBOOT is running hot: 93 degrees C when this was raised, over its line of 92.",
       f"A task has been open since {agent.day(NOWW - 9 * 86400)}: Order a battery for the garage sensor.",
       f"A task was due on {agent.day(NOWW - 60)}: Call the dentist."])
os.environ["TZ"] = "UTC"
time.tzset()
check("a day is said as a day, and one that is not a time is not a crash",
      (agent.day(1_800_000_000), agent.day(None), agent.day("x")), ("15 January", "a day not known", "a day not known"))
os.environ.pop("TZ")
time.tzset()
check("nothing is made of a laptop being away, a service switched off on purpose, a task that is done or one "
      "from today, or a disk with room", [k for k in got if any(w in k for w in ("mini", "mobile", "llama-eyes", "task:4",
                                                                         "task:6", "disk:node1"))], [])
got = agent.findings(V, None, [], NOWW, memo())
check("the Pi not answering is the Pi not answering: critical, and nothing is supposed of the machines it "
      "speaks for", sorted(f["key"] for f in got if not f["key"].endswith(":douglas") and "douglas:" not in f["key"]),
      ["gone:node1"])
check("a reading that is not there finds nothing: no degrees is not too many",
      agent.findings({"host": {}, "disks": [{"path": "/"}], "units": None}, {"machines": [{"id": "node1", "up": True}]},
                     [], NOWW, memo()), [])
m = memo()
looks = [agent.findings({"units": [unit("douglas-voice", restarts=n)], "versions": []}, {"machines": []}, [],
                        NOWW + t, m) for t, n in ((0, 5), (60, 6), (120, 7), (180, 9), (1200, 9))]
check("a service that is up each time it is looked at and has been started again four times in ten minutes is "
      "said so, and no longer once the ten minutes are over",
      [[f["text"] for f in fs] for fs in looks],
      [[], [], [], ["douglas-voice on DOUGLAS keeps being started again: 4 times in ten minutes when this was "
                    "raised."], []])
m = memo()
agent.findings({"units": [unit("douglas-voice")], "versions": [{"file": "douglas-voice.py", "sha": "aaa"}]},
               {"machines": []}, [], NOWW, m)
got = agent.findings({"units": [unit("douglas-voice", "activating", sub="auto-restart")],
                      "versions": [{"file": "douglas-voice.py", "sha": "bbb"}]}, {"machines": []}, [], NOWW + 20, m)
check("a service that is down just after its program was changed is a change that did not take, and critical "
      "whichever service it is", [(f["severity"], f["text"]) for f in got],
      [("critical", "douglas-voice on DOUGLAS is not running (being started again), and its program was changed "
                    "a few minutes ago: the change did not take.")])
check("a service that was stopped is said to be stopped, in a person's word for it",
      [f["text"] for f in agent.findings({"units": [unit("douglas-draw", "inactive", sub="dead", enabled="enabled")],
                                          "versions": []}, {"machines": []}, [], NOWW, memo())],
      ["douglas-draw on DOUGLAS is not running (stopped)."])

w = agent.Watch()
F = lambda sev="warning", after=30, hold=300: [{"key": "unit:x", "severity": sev, "after": after, "hold": hold}]  # noqa: E731
steps = [(0, F()), (20, F()), (40, F()), (60, F()), (80, []), (100, F()), (300, []), (500, []), (700, [])]
check("a finding becomes a notice once it has held for as long as its rule asks, once; a break while the notice "
      "is open leaves it open; it is cleared when it has been gone for as long as the rule asks",
      [(len(r), c) for r, c in (w.see(f, NOWW + t) for t, f in steps)],
      [(0, []), (0, []), (1, []), (0, []), (0, []), (0, []), (0, []), (0, []), (0, ["unit:x"])])
w = agent.Watch()
check("one that flickers and never holds that long raises nothing",
      [len(w.see(F() if n % 2 == 0 else [], NOWW + 20 * n)[0]) for n in range(12)], [0] * 12)
w = agent.Watch()
check("made worse while open, it is raised again as worse, and not again after that",
      [len(w.see(F(sev, after=0), NOWW + t)[0]) for t, sev in ((0, "warning"), (20, "warning"), (40, "critical"),
                                                               (60, "critical"), (80, "warning"))], [1, 0, 1, 0, 0])
w = agent.Watch()
w.adopt("unit:old", "warning")
check("a notice that was open when the program started is cleared when what it says no longer holds",
      [w.see([], NOWW + t)[1] for t in (0, 200, 400)], [[], [], ["unit:old"]])

# With the real memory behind the owner's door.
LOOKS = {"v": V, "fleet": FLEET}
real_vitals, real_fleet = agent.vitals, agent.fleet_seen
agent.vitals, agent.fleet_seen = lambda: LOOKS["v"], lambda: LOOKS["fleet"]
task_id = desk.assistant("remember", {"kind": "task", "subject": "garage sensor", "source": "said",
                                      "text": "Order a battery for the garage sensor marked zebra."})["id"]
desk.store.db.execute("UPDATE memories SET created = ? WHERE kind = 'task'", (NOWW - 9 * 86400,))
import contextlib, io      # noqa: E401, E402
said_by_watch = io.StringIO()
w, m = agent.Watch(), memo()
with contextlib.redirect_stdout(said_by_watch):
    counts = [agent.look(w, m, NOWW + t) for t in (0, 20, 40, 100, 130)]
open_now = {n["key"]: n["severity"] for n in desk.owner("notices", {})["open"]}
check("looked at over two minutes, each finding is raised when it has held long enough, and once",
      (counts, open_now),
      ([(1, 0), (0, 0), (3, 0), (2, 0), (3, 0)],
       {f"task:{task_id}": "information", "unit:douglas:llama-server": "critical", "unit:douglas:douglas-draw": "warning",
        "unit:node1:fauxmo": "warning", "disk:douglas:/": "critical", "gone:dougvoice": "warning",
        "hot:douglas": "warning", "hot:node1": "warning", "hot:workstation": "critical"}))
journal = said_by_watch.getvalue()
check("the journal has which notice and how bad, and not a task's words",
      ("douglas-agent: notice raised: unit:douglas:llama-server (critical)" in journal, "zebra" in journal,
       "battery" in journal), (True, False, False))
LOOKS["v"] = {"host": {"temp_c": 50}, "disks": [], "units": [unit("llama-server"), unit("douglas-draw")], "versions": []}
LOOKS["fleet"] = {"machines": [{"id": "dougvoice", "name": "DOUGVOICE", "up": True}]}
with contextlib.redirect_stdout(io.StringIO()):
    counts = [agent.look(w, m, NOWW + 130 + t) for t in (20, 100, 340, 700)]
check("put right, they are cleared, each when it has been gone for as long as its rule asks; the task stays",
      (counts, sorted(n["key"] for n in desk.owner("notices", {})["open"]), len(desk.owner("notices", {})["over"])),
      ([(0, 0), (0, 1), (0, 6), (0, 1)], [f"task:{task_id}"], 8))
w2 = agent.Watch()
for n in desk.owner("notices", {})["open"]:
    w2.adopt(n["key"], n["severity"])
desk.owner("correct", {"id": task_id, "done": True})
m["tasks"] = (0.0, [])
with contextlib.redirect_stdout(io.StringIO()):
    agent.look(w2, m, NOWW + 2000)
    agent.look(w2, m, NOWW + 2400)
check("a task that is ticked off stops being a notice, also after the program was started afresh",
      [n["key"] for n in desk.owner("notices", {})["open"]], [])
desk.owner("purge", {"id": task_id})
# The memory not answering while the watch has something to say.
LOOKS["v"] = {"host": {}, "disks": [], "units": [unit("douglas-draw", "failed")], "versions": []}
LOOKS["fleet"] = {"machines": []}
w3, m3 = agent.Watch(), memo()
real_memory, DOWN = agent.memory, {"down": True}
agent.memory = lambda op, fields, allowed: ((200, {"ok": False, "text": "Douglas's memory did not answer."})
                                            if DOWN["down"] else real_memory(op, fields, allowed))
with contextlib.redirect_stdout(io.StringIO()):
    quiet = [agent.look(w3, m3, NOWW + 5000 + t) for t in (0, 40, 60)]
    DOWN["down"] = False
    heard = agent.look(w3, m3, NOWW + 5080)
check("a notice the memory could not take is not thought raised: it is raised at the first look the memory answers",
      (quiet, heard, [n["key"] for n in desk.owner("notices", {})["open"]]),
      ([(0, 0)] * 3, (1, 0), ["unit:douglas:douglas-draw"]))
LOOKS["v"] = {"host": {}, "disks": [], "units": [unit("douglas-draw")], "versions": []}
with contextlib.redirect_stdout(io.StringIO()):
    agent.look(w3, m3, NOWW + 5100)
    DOWN["down"] = True
    quiet = agent.look(w3, m3, NOWW + 5500)          # gone long enough, and the memory does not answer
    DOWN["down"] = False
    later = [agent.look(w3, m3, NOWW + 5520 + t) for t in (0, 400)]
check("and one the memory could not clear is not thought cleared: it is cleared at the first look it answers",
      (quiet, later, [n["key"] for n in desk.owner("notices", {})["open"]]), ((0, 0), [(0, 1), (0, 0)], []))
agent.memory = real_memory
agent.vitals, agent.fleet_seen = real_vitals, real_fleet

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
desk.owner("notice_raise", {"key": "gone:dougvoice", "rule": "gone", "subject": "DOUGVOICE", "severity": "warning",
                            "text": "DOUGVOICE is not answering."})
code, got = call("/memory?token=t&op=notices")
nid = got["open"][0]["id"]
check("the owner's screen reads the notices and where each severity goes", (code, got["open"][0]["text"],
                                                                         got["prefs"]["critical"]),
      (200, "DOUGVOICE is not answering.", {"tell": True, "aloud": False}))
check("acknowledges one, and says where a severity is to go",
      (call("/memory?token=t", {"op": "notice_ack", "id": nid})[1]["changed"],
       call("/memory?token=t", {"op": "notice_prefs", "severity": "warning", "tell": False, "aloud": {"x": 1}})[1]["prefs"]["warning"]),
      (True, {"tell": False, "aloud": False}))
check("and cannot raise one or clear one: only the watch in this program does, and nothing that comes in over "
      "the network", (call("/memory?token=t", {"op": "notice_raise", "key": "x", "rule": "x", "subject": "x",
                                               "severity": "critical", "text": "Made up."})[0],
                      call("/memory?token=t", {"op": "notice_clear", "key": "gone:dougvoice"})[0],
                      call("/memory?token=t&op=notice_clear&key=gone:dougvoice")[0],
                      len(desk.owner("notices", {})["open"])), (400, 400, 400, 1))
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

print("-- older lines of the conversation, for a screen scrolled up --")
with open(transcript_file, "w") as f:
    for i in range(250):
        f.write(json.dumps({"ts": 1000 + i, "who": "user" if i % 2 == 0 else "douglas",
                            "text": f"line {i} " + "x" * (i % 37), "via": "phone"}) + "\n")
        if i == 120:
            f.write("not json at all\n")
    f.write('{"ts": 2000, "who": "douglas", "text": "a torn wri')
real_chunk = agent.TRANSCRIPT_CHUNK
agent.TRANSCRIPT_CHUNK = 97          # small chunks, so lines are cut between them
check("without the token, no older lines", call("/transcript?before=1100")[0], 403)
code, got = call("/transcript?token=t&before=1100&limit=5")
check("the five lines just before, oldest first, in the shape /status gives them",
      (code, got["ok"], [x["ts"] for x in got["lines"]], got["more"], sorted(got["lines"][0])),
      (200, True, [1095, 1096, 1097, 1098, 1099], True, ["text", "ts", "via", "who"]))
check("a line is given exactly as /status gives it",
      agent.transcript_before(1250, 1)["lines"], agent.transcript()[-1:])
got = agent.transcript_before(1125, 10)
check("a line that is not JSON is skipped, and no line is cut in two by a chunk",
      [x["text"].split()[1] for x in got["lines"]], [str(i) for i in range(115, 125)])
got = agent.transcript_before(1003, 30)
check("at the start of the record, what is left and no more", ([x["ts"] for x in got["lines"]], got["more"]),
      ([1000, 1001, 1002], False))
check("exactly enough left says there is no more", agent.transcript_before(1003, 3)["more"], False)
check("no `before` is the newest, the limit kept to a hundred, a strange one to the default",
      ([x["ts"] for x in agent.transcript_before(None, 2)["lines"]], len(agent.transcript_before("", 500)["lines"]),
       len(agent.transcript_before("nan", "lots")["lines"])), ([1248, 1249], 100, 30))
agent.TRANSCRIPT_CHUNK = real_chunk
real_cap = agent.TRANSCRIPT_READ_MAX
agent.TRANSCRIPT_READ_MAX = 2000     # a record far longer than one request may read
got = agent.transcript_before(1100, 30)
check("a request reads no more than its cap, and says older lines remain", (got["lines"], got["more"]), ([], True))
agent.TRANSCRIPT_READ_MAX = real_cap
web.shutdown()
doors["assistant"].shutdown()

srv.shutdown()
print("douglas-agent tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
