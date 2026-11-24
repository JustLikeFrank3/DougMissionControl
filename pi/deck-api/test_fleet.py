#!/usr/bin/env python3
"""The fleet as a reading, and the token that opens it and nothing else. No Pi,
no Douglas and no other machine needed: the state is set by hand."""
import json, os, pathlib, re, subprocess, sys, threading, time, urllib.error, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ.update({"DEFAULT_SURFACE": "evals", "DECK_TOKEN": "the-deck-token",
                   "DECK_READ_TOKEN": "the-read-token", "DOUGLAS_AGENT_TOKEN": "agent-token",
                   "FLEET_UNITS": "deck-api fauxmo k3s gone"})
import deck_api as d

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

# Everything on this deck that does something is stood in for: a route that
# opened when it should not have would otherwise boot a workstation.
DONE = []
for name in ("fire_boot", "fire_shutdown", "fire_launch", "fire_abort", "douglas_say", "douglas_hear",
             "douglas_ptt", "douglas_memory", "sim_command"):
    if hasattr(d, name):
        setattr(d, name, (lambda n: lambda *a, **k: (DONE.append(n), (False, "stood in"))[1])(name))
STATUS = {"agent": True, "ts": 1_800_000_000.0,
          "host": {"cpu_pct": 4.0, "mem_total_bytes": 134 << 30, "mem_used_bytes": 80 << 30, "temp_c": 51.0},
          "gpu": {"busy_pct": 3}, "llm": {"state": "running", "model": "a-model", "gen_tps": 22.5},
          "disks": [{"path": "/", "total_bytes": 2 << 40, "used_bytes": 300 << 30}],
          "units": [{"unit": "llama-server", "state": "active", "restarts": 0}],
          "voice": {"state": "idle", "satellite": True, "heard": {"words": 7}},
          "transcript": [{"ts": 1, "who": "user", "text": "my sister Ann lives in Leeds", "via": "phone"}],
          "models": [{"file": "a-model.gguf", "bytes": 70 << 30}]}
d.douglas_status = lambda: dict(STATUS)
SYSTEMD = {"asked": [], "out": (
    "Id=deck-api.service\nLoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=0\n"
    "ActiveEnterTimestampMonotonic=1000000\nResult=success\n\n"
    "Id=fauxmo.service\nLoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=3\n"
    "ActiveEnterTimestampMonotonic=0\nResult=exit-code\n\n"
    "Id=k3s.service\nLoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=14\n"
    "ActiveEnterTimestampMonotonic=5000000\nResult=success\nUnitFileState=enabled\n\n"
    "Id=gone.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nNRestarts=0\n"
    "ActiveEnterTimestampMonotonic=0\nResult=success\n")}
real_run = d.subprocess.run
def fake_run(argv, **kw):
    if argv[:2] != ["systemctl", "show"]:
        return real_run(argv, **kw)
    SYSTEMD["asked"].append(argv)
    return subprocess.CompletedProcess(argv, 0, SYSTEMD["out"], "")
d.subprocess.run = fake_run

now = time.time()
def set_state(s):
    s["workstation"].update(os="windows", since=now - 600, last_alive=now - 2, ip="192.0.2.50", agent=True)
    s["telemetry"] = {"ts": now - 3, "source": "windows", "ws": {"gpu_temp_c": 47, "gpu_util_pct": 12}, "pi": {}}
    s["pi"] = {"temp_c": 44.2, "load": 0.41, "mem_pct": 38, "uptime": 86400 * 9, "cpu_pct": 7}
    s["mini"].update(up=False, ssh=False, screen=False, ip="192.0.2.68", last_alive=now - 7200)
    s["mobile"].update(up=False, host="DougMobile.local", last_alive=None)
    s["douglas"].update(up=True, chat=True, stt=True, tts=False, host="douglas", last_alive=now - 1,
                        model={"state": "running", "name": "a-model"})
    s["voice"].update(up=True, satellite=True, host="dougvoice", last_alive=now - 4)
    s["nav_plan"] = [{"name": "HOME FIELD", "lat": 28.5, "lon": -81.3}]
    s["events"] = [{"ts": now - 30, "source": "probe", "level": "warn",
                    "text": "workstation at 192.0.2.50 refused http://198.51.100.7:9107/x?token=abc123&y=1"},
                   {"ts": now - 90, "source": "deck-api", "level": "info", "text": "listening on :8088"}]
d.DECK.mutate(set_state)

print("-- the reading itself --")
f = d.fleet()
by = {m["id"]: m for m in f["machines"]}
check("every machine, by a name and with what it is for",
      [(m["id"], m["name"], bool(m["role"])) for m in f["machines"]],
      [("node1", "DOUG PRIME", True), ("workstation", "DOUG DUALBOOT", True), ("douglas", "DOUGLAS", True),
       ("dougvoice", "DOUGVOICE", True), ("mini", "MINIDOUG4", True), ("mobile", "DOUGMOBILE", True)])
check("which answer, and when each last did: never a guess",
      [(m["id"], m["up"], None if m["seen"] is None else round(now - m["seen"])) for m in f["machines"][1:]],
      [("workstation", True, 2), ("douglas", True, 1), ("dougvoice", True, 4), ("mini", False, 7200),
       ("mobile", False, None)])
check("the workstation: which system is up, and its numbers with the moment they were read. Since when is "
      "not said of a state nobody saw begin: only how long it has been watched",
      (by["workstation"]["state"], by["workstation"]["since"], round(now - by["workstation"]["watched_since"]),
       by["workstation"]["reports"], round(now - by["workstation"]["as_of"]), by["workstation"]["booting"]),
      ("windows", None, 600, {"gpu_temp_c": 47, "gpu_util_pct": 12}, 3, None))
d.DECK.mutate(lambda s: s["workstation"].update(since_seen=True))
w = d.fleet()["machines"][1]
check("a change that was seen happening is said with when", (round(now - w["since"]), w["watched_since"]), (600, None))
pi = by["node1"]
check("this Pi: its own readings, and how full its disk is",
      ({k: pi["reports"][k] for k in ("temp_c", "load", "mem_pct", "cpu_pct", "uptime_s")},
       sorted(pi["reports"]["disk"]), pi["reports"]["disk"]["total_bytes"] > pi["reports"]["disk"]["free_bytes"] > 0),
      ({"temp_c": 44.2, "load": 0.41, "mem_pct": 38, "cpu_pct": 7, "uptime_s": 777600},
       ["free_bytes", "path", "total_bytes", "used_bytes"], True))
check("and its services: up or failed, how often restarted, and one that is not installed is not listed",
      [(u["unit"], u["state"], u["restarts"], u["since"] is not None, u["enabled"]) for u in pi["services"]],
      [("deck-api", "active", 0, True, None), ("fauxmo", "failed", 3, False, None),
       ("k3s", "active", 14, True, "enabled")])
check("asked of systemd with a fixed command, and nothing a caller says is in it",
      SYSTEMD["asked"][0][:6], ["systemctl", "show", "deck-api.service", "fauxmo.service", "k3s.service",
                                "gone.service"])
d.fleet(); d.fleet()
check("and not asked again for every reading", len(SYSTEMD["asked"]), 1)
dg = by["douglas"]
check("Douglas's machine gives its own account of its health",
      (sorted(dg["reports"]), dg["services"], dg["model"], dg["doors"], dg["as_of"]),
      (["disks", "gpu", "host", "llm"], [{"unit": "llama-server", "state": "active", "restarts": 0}],
       {"state": "running", "name": "a-model"},
       {"chat page": True, "speech to text": True, "text to speech": False}, 1_800_000_000.0))
whole = json.dumps(f)
check("and nothing else of his: not what was said to him, not his voice, not a word of his",
      [w for w in ("transcript", "Leeds", "sister", "heard", "satellite\": true, \"heard") if w in whole], [])
check("no address is in it anywhere, no token, no flight plan, nothing to press",
      ([w for w in ("192.0.2", "198.51.100", "abc123", ".local", "HOME FIELD", "profiles", "the-deck-token",
                    "agent-token", "the-read-token") if w in whole],
       re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", whole)), ([], []))
check("what happened lately is there, with the address and the token taken out of it",
      [(e["source"], e["level"], e["text"]) for e in f["events"]],
      [("probe", "warn", "workstation at (an address) refused http://(an address):9107/x?token=...&y=1"),
       ("deck-api", "info", "listening on :8088")])
check("and it says what it cannot tell", "say only whether they answer" in f["not_reported"], True)
STATUS["agent"] = False
check("Douglas's agent not answering: what the probe knows still stands, and no numbers are made up",
      ("reports" in d.fleet()["machines"][2], d.fleet()["machines"][2]["up"]), (False, True))
STATUS["agent"] = True
def off(s):
    s["workstation"].update(os="off", since=now - 60)
    s["boot"].update(in_flight=True, target="windows", phase_name="PING")
d.DECK.mutate(off)
ws = d.fleet()["machines"][1]
check("a machine that is off reports nothing, rather than its last numbers as if they were now",
      (ws["up"], ws["state"], ws["reports"], ws["as_of"], ws["booting"]),
      (False, "off", {}, None, {"to": "windows", "phase": "PING"}))
SYSTEMD["out"] = ""
d._units_kept.update(at=0.0, value=None)
check("systemd saying nothing is no services, not an error", d.fleet()["machines"][0]["services"], [])
d.subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "Failed to get properties")
d._units_kept.update(at=0.0, value=None)
check("systemd refusing to say is not known", d.fleet()["machines"][0]["services"], None)
def broken(argv, **kw): raise OSError("no systemctl here")
d.subprocess.run = broken
d._units_kept.update(at=0.0, value=None)
check("and no systemd at all is said as not known", d.fleet()["machines"][0]["services"], None)
d.subprocess.run = fake_run

print("-- who may read it, and what else that opens --")
server = d.ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
AWAY = {"X-Forwarded-For": "100.64.0.9"}          # as a request arrives from another machine

def call(path, token="", method="GET", headers=AWAY, body=None):
    url = f"http://127.0.0.1:{port}{path}" + (("&" if "?" in path else "?") + "token=" + token if token else "")
    data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers, **({"Content-Type": "application/json"} if data else {})))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

check("from another machine: nothing without a token, nothing with a wrong one",
      (call("/api/fleet"), call("/api/fleet", "nonsense"), call("/api/fleet", "the-read-toke")), (403, 403, 403))
check("the read token opens it, and so does the deck's own", (call("/api/fleet", "the-read-token"),
                                                             call("/api/fleet", "the-deck-token")), (200, 200))
check("the panel itself, on this Pi, needs none", call("/api/fleet", headers={}), 200)
source = pathlib.Path(d.__file__).read_text()
routes = sorted(set(re.findall(r'path == "(/api/[a-z/_-]+)"', source)))
check("every route this deck has is known to this test", len(routes) >= 25 and "/api/boot" in routes
      and "/api/douglas/memory" in routes and "/api/state" in routes, True)
got = {r: (call(r, "the-read-token"), call(r, "the-read-token", "POST")) for r in routes if r != "/api/fleet"}
# (/api/phase is the workstation's own callback: it is let in by a token of
# the boot agent's in its body, never by one in the address, and says 403 to this.)
# A route that only takes a POST has nothing at all for a GET (404): either
# way it did not open. To act, the answer is 403 every time.
check("the read token opens no other route, to look or to act: not the state, not Douglas, not a boot",
      ({r: c for r, c in got.items() if c[0] not in (403, 404) or c[1] != 403}, got["/api/state"],
       got["/api/douglas"], got["/api/douglas/memory"], got["/api/boot"][1], got["/api/shutdown"][1]),
      ({}, (403, 403), (403, 403), (403, 403), 403, 403))
check("and with it nothing was done", DONE, [])
check("nor does it open the fleet for anything but a look", call("/api/fleet", "the-read-token", "POST"), 403)
check("nor an address made to look like the fleet's",
      [call(p, "the-read-token") for p in ("/api/fleet/../state", "/api/fleet/", "/api/fleet%2f..%2fstate",
                                           "/api/state?x=/api/fleet", "//api/fleet/../../api/state")],
      [404, 404, 404, 403, 404])
check("the deck's own token still opens the deck", (call("/api/state", "the-deck-token"),
                                                   call("/api/douglas", "the-deck-token")), (200, 200))
d.DECK_READ_TOKEN = ""
check("with no read token set, an empty one opens nothing", (call("/api/fleet"), call("/api/fleet?token=")), (403, 403))
d.DECK_READ_TOKEN = "the-read-token"
server.shutdown()

print("-- a read token that is the deck's token --")
out = subprocess.run([sys.executable, "-c", "import deck_api as d; print(repr(d.DECK_READ_TOKEN), repr(d.DECK_TOKEN))"],
                     cwd=str(pathlib.Path(__file__).resolve().parent), capture_output=True, text=True,
                     env=dict(os.environ, DECK_TOKEN="same", DECK_READ_TOKEN="same"))
check("is not a read token: it is ignored, and the journal says so",
      (out.stdout.strip(), "DECK_READ_TOKEN is the same as DECK_TOKEN" in out.stderr), ("'' 'same'", True))

print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("all passed")
