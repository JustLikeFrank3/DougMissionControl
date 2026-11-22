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
said = []
def fake_say(text, via, image=None):
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

server.shutdown()
print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("all passed")
