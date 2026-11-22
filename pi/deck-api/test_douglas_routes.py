#!/usr/bin/env python3
"""The Douglas routes' journal lines: what a question leaves behind for whoever
has to work out, later, why a phone's SEND went grey. No Pi, no Douglas needed."""
import io, json, os, pathlib, socket, sys, threading, urllib.error, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ["DEFAULT_SURFACE"] = "evals"
import deck_api as d

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

# Douglas is not here: the two calls that would reach him are stood in for.
real_say = d.douglas_say
said = []
def fake_say(text, via, image=None, ask_id=None):
    said.append((text, via, image))
    return (False, "douglas-agent did not\nanswer") if text == "nobody home" else (True, "thinking")
d.douglas_say = fake_say
d.douglas_ptt = lambda action: (action in ("start", "stop", "cancel"), "action must be start, stop or cancel")

server = d.ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

def post(path, body):
    """(status, reply, what the journal got) for one POST."""
    journal, real = io.StringIO(), sys.stderr
    sys.stderr = journal
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
    finally:
        sys.stderr = real
    return code, out, journal.getvalue()

print("-- a typed question --")
code, out, log = post("/api/douglas/say?token=SECRET-TOKEN", {"text": "is the garage door open", "via": "phone"})
check("it is passed on", (code, out["ok"], said[-1][:2]), (200, True, ("is the garage door open", "phone")))
check("one journal line", log.count("\n"), 1)
check("with the route, the caller, the size and the outcome",
      log.strip(), "http: /api/douglas/say from 127.0.0.1: 23 chars accepted in 0.0 s")
check("and none of the words", "garage" in log, False)
check("and not the token", "SECRET" in log, False)

print("-- a photo --")
picture = "A" * 400_000
code, out, log = post("/api/douglas/say", {"text": "", "via": "phone", "image": picture})
check("it is passed on whole", (code, said[-1][2] == picture), (200, True))
check("the journal has its size", log.strip(),
      "http: /api/douglas/say from 127.0.0.1: 0 chars and a 292 KB picture accepted in 0.0 s")

print("-- Douglas does not take it --")
code, out, log = post("/api/douglas/say", {"text": "nobody home", "via": "deck"})
check("the phone is told", (code, out["ok"]), (502, False))
check("the journal says why, on one line", log.strip(),
      "http: /api/douglas/say from 127.0.0.1: 11 chars refused (douglas-agent did not answer) in 0.0 s")

print("-- push to talk --")
code, out, log = post("/api/douglas/ptt", {"action": "start"})
check("a press is one line", log.strip(), "http: /api/douglas/ptt from 127.0.0.1: start")
code, out, log = post("/api/douglas/ptt", {"action": "sing\nhttp: forged line"})
check("a bad action cannot forge a line", (code, log.count("\n"), log.startswith("http: /api/douglas/ptt from 127.0.0.1: sing http: f")),
      (502, 1, True))

print("-- a body that stops part-way, as a phone on a bad link sends --")
before = len(said)
journal, real = io.StringIO(), sys.stderr
sys.stderr = journal
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"POST /api/douglas/say HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
              b"Content-Length: 5000\r\n\r\n" + b'{"text": "half a ques')
    s.shutdown(socket.SHUT_WR)
    reply = b""
    while chunk := s.recv(4096):
        reply += chunk
    s.close()
finally:
    sys.stderr = real
log = journal.getvalue()
check("it is refused", reply.split(b"\r\n")[0], b"HTTP/1.1 400 Bad Request")
check("and never passed on as if it were whole", len(said), before)
check("and the journal says the body was cut short", log.strip(),
      "http: /api/douglas/say from 127.0.0.1: body cut short, 21 of 5000 bytes")
check("without the half that did arrive", "half a ques" in log, False)

print("-- a caller that leaves before the answer --")
journal, real = io.StringIO(), sys.stderr
sys.stderr = journal
try:
    import struct, time
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    d.douglas_say = lambda *a, **k: (time.sleep(0.4), (True, "thinking"))[1]
    body = b'{"text": "gone before you answer"}'
    s.sendall(b"POST /api/douglas/say HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()                                  # a reset, as a sleeping phone leaves one
    time.sleep(1.0)
finally:
    sys.stderr = real
    d.douglas_say = fake_say
log = journal.getvalue()
check("is one line in the journal, not a traceback",
      (log.count("\n"), "Traceback" in log), (1, False))

print("-- a caller that leaves between requests --")
journal, real = io.StringIO(), sys.stderr
sys.stderr = journal
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
    s.recv(65536)                              # one request answered, the connection kept
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()                                  # and reset while it waits for the next
    time.sleep(0.5)
finally:
    sys.stderr = real
check("leaves nothing in the journal", journal.getvalue(), "")

print("-- the same question sent twice, as a phone does when the reply was lost --")
import time
d.DOUGLAS_AGENT_TOKEN = "agent-token"
reached, answer = [], {"got": {"ok": True, "message": "thinking"}, "wait": 0.0}
def fake_agent(url, payload=None, timeout=3):
    reached.append(payload)
    time.sleep(answer["wait"])
    return answer["got"]
d._ws_json = fake_agent
check("the first copy goes to Douglas", real_say("is it raining", "phone", None, "q-00000001"), (True, "thinking"))
check("the second is acknowledged", real_say("is it raining", "phone", None, "q-00000001"), (True, "already sent"))
check("and Douglas was asked once", len(reached), 1)
check("another question still goes", (real_say("and tomorrow", "phone", None, "q-00000002"), len(reached)),
      ((True, "thinking"), 2))

answer["got"] = None
check("a copy Douglas never took", real_say("anyone", "phone", None, "q-00000003"),
      (False, "douglas-agent did not answer"))
answer["got"] = {"ok": True, "message": "thinking"}
check("is sent again, not written off as done", (real_say("anyone", "phone", None, "q-00000003"), len(reached)),
      ((True, "thinking"), 4))

answer["wait"] = 0.4
before, outcomes = len(reached), []
pair = [threading.Thread(target=lambda: outcomes.append(real_say("two at once", "phone", "A" * 64, "q-00000004")))
        for _ in range(2)]
[t.start() for t in pair]; [t.join() for t in pair]
answer["wait"] = 0.0
check("two copies arriving together reach him once", (len(reached) - before, sorted(outcomes)),
      (1, [(True, "already sent"), (True, "thinking")]))

before = len(reached)
real_say("no id", "deck"); real_say("no id", "deck")
real_say("bad id", "deck", None, "x"); real_say("bad id", "deck", None, "x")
check("without a proper id every copy is sent", len(reached) - before, 4)

d._said["q-ancient01"] = time.time() - d.SAID_KEEP_S - 5
real_say("tidy up", "phone", None, "q-00000005")
check("old ids are forgotten", "q-ancient01" in d._said, False)

d.douglas_say = real_say
code, out, log = post("/api/douglas/say", {"text": "is it raining", "via": "phone", "id": "q-00000001"})
check("over the wire a repeat is a success", (code, out["ok"], out["message"]), (200, True, "already sent"))
check("and the journal says it was a repeat", log.strip(),
      "http: /api/douglas/say from 127.0.0.1: 13 chars again: Douglas already has it in 0.0 s")
answer["got"] = None
code, out, log = post("/api/douglas/say", {"text": "hello", "via": "phone", "id": "q-00000006"})
check("Douglas not answering is marked worth another try", (code, out["ok"], out["retry"]), (502, False, True))
code, out, log = post("/api/douglas/say", {"text": "", "via": "phone", "id": "q-00000007"})
check("an empty question is not", (code, out["retry"]), (502, False))
d.douglas_say = fake_say

server.shutdown()
print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("all passed")
