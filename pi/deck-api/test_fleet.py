#!/usr/bin/env python3
"""The fleet as a reading, and the token that opens it and nothing else. No Pi,
no Douglas and no other machine needed: the state is set by hand."""
import json, os, pathlib, re, subprocess, sys, threading, time, urllib.error, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ.update({"DEFAULT_SURFACE": "evals", "DECK_TOKEN": "the-deck-token",
                   "DECK_READ_TOKEN": "the-read-token", "DOUGLAS_AGENT_TOKEN": "agent-token",
                   "FLEET_UNITS": "deck-api fauxmo k3s gone"})
import deck_api as d
d.FLEET_KEEP_S = 0          # each reading made afresh here; that it is kept for a moment is tested below

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

# Everything on this deck that does something is stood in for: a route that
# opened when it should not have would otherwise boot a workstation.
relay_as_it_is = d.douglas_memory          # before it is stood in for below
DONE = []
for name in ("fire_boot", "fire_shutdown", "fire_launch", "fire_abort", "douglas_say", "douglas_hear",
             "douglas_ptt", "douglas_memory", "douglas_owner", "sim_command"):
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
                   {"ts": now - 60, "source": "probe", "level": "warn",
                    "text": "fe80::1ff:fe23:4567:890a and 2001:db8::7 (aa-bb-cc-dd-ee-ff) at node1.tail1234.ts.net: "
                            "Authorization: Bearer abc.def-123, and DECK_TOKEN is the-deck-token, api_key: zzz999"},
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
       ("probe", "warn", "(an address) and (an address) ((an address)) at (an address): Authorization=..., and "
                         "DECK_TOKEN=..., api_key=..."),
       ("deck-api", "info", "listening on :8088")])
check("whatever kind of address or credential it was: none of it is left",
      [w for w in ("fe80", "2001:db8", "aa-bb", "ts.net", "abc.def", "the-deck-token", "zzz999") if w in whole], [])
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

d.subprocess.run = fake_run
d.FLEET_KEEP_S, asked = 2.0, []
real_status, d.douglas_status = d.douglas_status, lambda: (asked.append(1), dict(STATUS))[1]
d._fleet_kept.update(at=0.0, value=None)
for _ in range(25):
    d.fleet()
check("a reader asking over and over does not have Douglas's machine asked each time: the reading is kept for "
      "a moment", len(asked), 1)
d.douglas_status, d.FLEET_KEEP_S = real_status, 0
SYSTEMD["asked"].clear()
d.subprocess.run = broken
d._units_kept.update(at=0.0, value=None)
calls = []
d.subprocess.run = lambda argv, **kw: (calls.append(1), broken(argv))[1]
for _ in range(10):
    d.own_units()
check("nor is systemd, when it cannot be asked: that is kept as long as an answer would be", len(calls), 1)
d.subprocess.run = fake_run

print("-- the Pi's own word when Douglas or his ears go quiet --")
d.DECK.mutate(lambda s: s["events"].clear())
d._quiet.clear()
for up in (False, False, False, False):
    d.gone_or_back("DOUGLAS", up)
check("a machine that was never seen up since this started is not said to have gone", d.DECK.snapshot()["events"], [])
for up in (True, False, True, False, False, True, False, False, False, False, False, True, True):
    d.gone_or_back("DOUGLAS", up)
check("gone is said after three looks in a row and once, not for one lost look; and back is said once",
      [(e["source"], e["level"], e["text"]) for e in reversed(d.DECK.snapshot()["events"])],
      [("probe", "warn", "DOUGLAS: not answering"), ("probe", "info", "DOUGLAS: answering again")])
check("and it is in the fleet's reading, where Douglas reads it when he is back",
      [e["text"] for e in d.fleet()["events"]][:2], ["DOUGLAS: answering again", "DOUGLAS: not answering"])

d.DECK.mutate(lambda s: s["events"].clear())
d._quiet.clear()
real_open, PORT = d.tcp_open, {"open": True}
d.tcp_open = lambda host, port, timeout=1.5: PORT["open"]
was_token, d.DOUGLAS_AGENT_TOKEN = d.DOUGLAS_AGENT_TOKEN, "t"
for machine, agent in ((True, True), (True, False), (True, False), (True, False), (True, False), (False, False),
                       (False, False), (False, False), (False, False), (True, False), (True, True), (True, True)):
    PORT["open"] = agent
    d.who_is_gone({"up": machine}, {"up": True})
check("the watch on Douglas gone while his machine is up is said, once; with the machine itself gone only that is "
      "said; and each is said to be back",
      [e["text"] for e in reversed(d.DECK.snapshot()["events"])],
      ["the watch on DOUGLAS: not answering", "DOUGLAS: not answering", "DOUGLAS: answering again",
       "the watch on DOUGLAS: answering again"])
d.DECK.mutate(lambda s: s["events"].clear())
d._quiet.clear()
for machine in (True, False, False, False, False, True):
    PORT["open"] = machine                           # a machine that is down has no ports open either
    d.who_is_gone({"up": machine}, {"up": True})
check("a machine gone is said to be gone, and nothing is added about the watch on it",
      [e["text"] for e in reversed(d.DECK.snapshot()["events"])], ["DOUGLAS: not answering", "DOUGLAS: answering again"])
d.tcp_open, d.DOUGLAS_AGENT_TOKEN = real_open, was_token

real_ws, ASKED = d._ws_json, []
OPEN = {"got": {"ok": True, "open": [
    {"id": 1, "severity": "critical", "acked": None}, {"id": 2, "severity": "warning", "acked": 1_800_000_000.0},
    {"id": 3, "severity": "warning", "acked": None}, {"id": 4, "severity": "information", "acked": None}]}}
d._ws_json = lambda url, body=None, timeout=3: (ASKED.append(url.split("?")[0].rsplit("/", 1)[1]), OPEN["got"])[1]
was_token, d.DOUGLAS_AGENT_TOKEN = d.DOUGLAS_AGENT_TOKEN, "t"
d._notices_kept.update(at=-1e9, value=None)
first, again = d.douglas_notices(), d.douglas_notices()
check("for the top bar: how many notices are open, how many warnings and worse nobody has seen, and the worst of "
      "those; asked of Douglas once, not at every look",
      (first, again == first, ASKED), ({"open": 4, "new": 2, "worst": "critical"}, True, ["memory"]))
OPEN["got"] = {"ok": True, "open": [{"id": 4, "severity": "information", "acked": None},
                                    {"id": 2, "severity": "critical", "acked": 1_800_000_000.0}]}
d._notices_kept["at"] = -1e9
check("information, and what has been seen, are not for the top bar", d.douglas_notices(),
      {"open": 2, "new": 0, "worst": None})
OPEN["got"] = None
d._notices_kept["at"] = -1e9
check("not read is not none open", d.douglas_notices(), None)
OPEN["got"] = {"ok": True, "open": []}
check("and seen on a screen, the count is asked for afresh at the next look",
      (d.douglas_notices(), relay_as_it_is("notice_ack", {"id": 1}, change=True)[0], d.douglas_notices()),
      (None, 200, {"open": 0, "new": 0, "worst": None}))
d._ws_json, d.DOUGLAS_AGENT_TOKEN = real_ws, was_token
d._notices_kept.update(at=-1e9, value=None)

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
