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
real_ws = d._ws_json
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

print("-- the same deck behind a local HTTPS proxy, as tailscale serve puts it --")
def get(path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
d.DECK_TOKEN = "deck-token"
check("the panel itself, from loopback, needs no token", get("/api/state"), 200)
check("a request the proxy passed on does", get("/api/state", {"X-Forwarded-For": "100.64.0.9"}), 403)
check("with the token it is let in", get("/api/state?token=deck-token", {"X-Forwarded-For": "100.64.0.9"}), 200)
check("a wrong token is not", get("/api/state?token=nope", {"X-Forwarded-For": "100.64.0.9"}), 403)
away = {"X-Forwarded-For": "100.64.0.9"}
check("an app sends it in a header instead, and is let in the same",
      get("/api/state", {**away, "Authorization": "Bearer deck-token"}), 200)
check("whatever case the word is in", get("/api/state", {**away, "Authorization": "bearer deck-token"}), 200)
check("a wrong one in the header is not", get("/api/state", {**away, "Authorization": "Bearer nope"}), 403)
check("nor the right one under another scheme",
      get("/api/state", {**away, "Authorization": "Basic deck-token"}), 403)
check("nor the word with nothing after it", get("/api/state", {**away, "Authorization": "Bearer"}), 403)
check("where the address has one too, the address is the one read",
      (get("/api/state?token=nope", {**away, "Authorization": "Bearer deck-token"}),
       get("/api/state?token=deck-token", {**away, "Authorization": "Bearer nope"})), (403, 200))
d.DECK_TOKEN = ""
check("with no deck token set up, an empty header opens nothing",
      get("/api/state", {**away, "Authorization": "Bearer "}), 403)
d.DECK_TOKEN = "deck-token"
check("the proxy's identity header alone is enough to ask for one",
      get("/api/douglas", {"Tailscale-User-Login": "someone@example.com"}), 403)
journal, real = io.StringIO(), sys.stderr
sys.stderr = journal
try:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/douglas/ptt?token=deck-token",
                                 json.dumps({"action": "start"}).encode(),
                                 {"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.9, 10.0.0.1"})
    urllib.request.urlopen(req, timeout=5).read()
finally:
    sys.stderr = real
check("and the journal names who it was passed on for", journal.getvalue().strip(),
      "http: /api/douglas/ptt from 100.64.0.9: start")

print("-- a refused request does not spoil the next one on the same connection --")
import http.client
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
conn.request("POST", "/api/douglas/say", body=json.dumps({"text": "let me in"}),
             headers={"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.9"})
r = conn.getresponse()
first = (r.status, r.getheader("Connection"))
r.read()
try:
    conn.request("GET", "/api/state?token=deck-token", headers={"X-Forwarded-For": "100.64.0.9"})
    second = conn.getresponse().status
except (http.client.HTTPException, OSError) as e:
    second = type(e).__name__
conn.close()
check("the refusal reads its body out of the way", first, (403, None))
check("so the next request on that connection starts clean", second, 200)
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
conn.request("POST", "/api/douglas/hear", body=json.dumps({"audio": "A" * 700_000}),
             headers={"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.9"})
r = conn.getresponse()
check("a refused recording is heard out too, and told no", (r.status, r.getheader("Connection")), (403, None))
r.read()
conn.close()
s2 = socket.create_connection(("127.0.0.1", port), timeout=5)
s2.sendall(b"POST /api/douglas/say HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 100.64.0.9\r\n"
           b"Content-Length: 50000000\r\n\r\n{")
reply = s2.recv(4096)
s2.close()
check("a body too big to be worth reading ends the connection instead",
      (reply.split(b"\r\n")[0], b"Connection: close" in reply), (b"HTTP/1.1 403 Forbidden", True))

print("-- a turn recorded on the phone --")
heard, answer["got"], answer["wait"] = [], {"ok": True, "message": "thinking"}, 0.0
d._ws_json = lambda url, payload=None, timeout=3: (heard.append((url.split("?")[0].rsplit("/", 1)[-1], payload, timeout)), answer["got"])[1]
recording = "A" * 85336                                  # two seconds of 16 kHz, as base64
code, out, log = post("/api/douglas/hear", {"audio": recording, "rate": 16000, "via": "phone", "id": "q-heard-0001"})
check("it goes to Douglas whole, with time to take it",
      (code, out["ok"], heard[-1][0], heard[-1][1]["audio"] == recording, heard[-1][1]["rate"], heard[-1][2]),
      (200, True, "hear", True, 16000, 25))
check("the journal has its length, not its sound", log.strip(),
      "http: /api/douglas/hear from 127.0.0.1: 2.0 s recorded accepted in 0.0 s")
code, out, log = post("/api/douglas/hear", {"audio": recording, "rate": 16000, "id": "q-heard-0001"})
check("sent twice it reaches him once", (out["message"], len(heard)), ("already sent", 1))
code, out, log = post("/api/douglas/hear", {"audio": "", "id": "q-heard-0002"})
check("an empty recording is refused here", (code, out["message"], len(heard)), (502, "nothing recorded", 1))
code, out, log = post("/api/douglas/hear", {"audio": "A" * (d.DOUGLAS_AUDIO_MAX + 4), "id": "q-heard-0003"})
check("and one that is too long", (code, out["message"]), (502, "that recording is too long"))

print("-- his reply, as sound for the phone --")
class Agent(d.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        q = d.parse_qs(d.urlparse(self.path).query)
        which = (q.get("id") or [""])[0]
        if which == "q-ready-0001":
            body, kind, code = b"RIFF....WAVEfmt fake audio", "audio/wav", 200
        elif which == "q-think-0001":
            body, kind, code = b'{"ok": false, "state": "thinking"}', "application/json", 202
        elif which == "q-quiet-0001":
            body, kind, code = b'{"ok": false, "state": "nothing"}', "application/json", 200
        else:
            body, kind, code = b'{"ok": false, "state": "unknown"}', "application/json", 404
        self.send_response(code); self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
agent_srv = d.ThreadingHTTPServer(("127.0.0.1", 0), Agent)
threading.Thread(target=agent_srv.serve_forever, daemon=True).start()
d.DOUGLAS_HOST, d.DOUGLAS_AGENT_PORT = "127.0.0.1", agent_srv.server_address[1]
def speech(which):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/douglas/speech?id={which}", timeout=5) as r:
            return r.status, r.headers.get_content_type(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get_content_type(), e.read()
check("ready: the audio itself", speech("q-ready-0001"), (200, "audio/wav", b"RIFF....WAVEfmt fake audio"))
check("still thinking", (speech("q-think-0001")[0], json.loads(speech("q-think-0001")[2])["state"]), (202, "thinking"))
check("nothing was heard in it", json.loads(speech("q-quiet-0001")[2])["state"], "nothing")
check("an id he never saw", (speech("q-never-0001")[0], json.loads(speech("q-never-0001")[2])["state"]), (404, "unknown"))
check("an id that is not one is not passed on", speech("../../etc")[0], 400)
agent_srv.shutdown()
d.DOUGLAS_AGENT_PORT = 1
check("Douglas not answering", (speech("q-ready-0001")[0], json.loads(speech("q-ready-0001")[2])["state"]), (502, "failed"))

print("-- a picture he drew, for a screen --")
SEEN_IDS = []
class Gallery(d.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = d.urlparse(self.path); q = d.parse_qs(u.query)
        which = (q.get("id") or [""])[0]
        SEEN_IDS.append((u.path, which, (q.get("token") or [""])[0]))
        if which == "p-0123456789ab":
            body, kind, code = b"\xff\xd8\xff\xe0 a picture", "image/jpeg", 200
        elif which == "p-00000000beef":
            body, kind, code = b"<html>oops</html>", "text/html", 200
        else:
            body, kind, code = b'{"error": "no such picture"}', "application/json", 404
        self.send_response(code); self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
gallery = d.ThreadingHTTPServer(("127.0.0.1", 0), Gallery)
threading.Thread(target=gallery.serve_forever, daemon=True).start()
d.DOUGLAS_AGENT_PORT = gallery.server_address[1]
def picture(which, token=""):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/douglas/picture?id={which}"
                                    + (f"&token={token}" if token else ""), timeout=5) as r:
            return r.status, r.headers.get_content_type(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get_content_type(), e.read()
check("the picture itself, with its type", picture("p-0123456789ab"), (200, "image/jpeg", b"\xff\xd8\xff\xe0 a picture"))
check("asked of the agent's picture route, with the agent's token, never the deck's",
      SEEN_IDS[-1], ("/picture", "p-0123456789ab", d.DOUGLAS_AGENT_TOKEN))
check("an id he never gave out", picture("p-aaaaaaaaaaaa")[0], 404)
n = len(SEEN_IDS)
check("an id that is not one is not passed on at all",
      ([picture(x)[0] for x in ("../../etc/passwd", "p-123", "q-ready-0001")], len(SEEN_IDS) - n), ([400, 400, 400], 0))
check("something that is not a picture is not served as one", picture("p-00000000beef")[0], 502)
d.DECK_TOKEN = "deck-token"
check("a picture wants the deck token like the rest of him",
      (get("/api/douglas/picture?id=p-0123456789ab", {"X-Forwarded-For": "100.64.0.9"}),
       get("/api/douglas/picture?id=p-0123456789ab&token=deck-token", {"X-Forwarded-For": "100.64.0.9"})), (403, 200))
d.DECK_TOKEN = ""
gallery.shutdown()
d.DOUGLAS_AGENT_PORT = 1
check("Douglas not answering", picture("p-0123456789ab")[0], 502)
d.DECK_TOKEN = ""

print("-- what he remembers, for the owner to read and put right --")
ASKED = []
class Memory(d.BaseHTTPRequestHandler):
    """Douglas's agent, as far as its /memory goes."""
    def log_message(self, *a): pass
    def answer(self, got):
        u = d.urlparse(self.path); q = {k: v[0] for k, v in d.parse_qs(u.query).items()}
        ASKED.append((self.command, u.path, q.pop("token", ""), got if got is not None else q))
        op = (got if got is not None else q).get("op")
        out = {"list": {"ok": True, "total": 1, "memories": [{"id": 7, "kind": "preference", "text": "Sam takes his coffee black."}]},
               "stats": {"ok": True, "active": 1}, "delete": {"ok": True, "id": 7, "status": "deleted"},
               "purge": {"ok": True}}.get(op, {"ok": False, "text": "There is no such memory."})
        body = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self): self.answer(None)
    def do_POST(self): self.answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
mem = d.ThreadingHTTPServer(("127.0.0.1", 0), Memory)
threading.Thread(target=mem.serve_forever, daemon=True).start()
d.DOUGLAS_AGENT_PORT = mem.server_address[1]
d._ws_json = real_ws                      # the agent is really asked here, over HTTP
def look(query, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/douglas/memory?{query}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
code, out = look("op=list&kind=preference&q=coffee&limit=20&token=whatever&junk=1")
check("the list, as his memory gave it", (code, out["memories"][0]["text"]), (200, "Sam takes his coffee black."))
check("asked of the agent with the agent's token, and only with what a question may carry",
      ASKED[-1], ("GET", "/memory", d.DOUGLAS_AGENT_TOKEN, {"op": "list", "kind": "preference", "q": "coffee", "limit": "20"}))
n = len(ASKED)
check("a GET that asks for a change is not passed on at all",
      ([look(f"op={op}&id=7")[0] for op in ("delete", "purge", "correct", "restore", "add", "export", "recall", "")],
       len(ASKED) - n), ([400] * 8, 0))
code, out, log = post("/api/douglas/memory", {"op": "delete", "id": 7, "text": "ignored", "status": "x", "junk": [1]})
check("a deletion is passed on", (code, out["status"], ASKED[-1][0], ASKED[-1][3]),
      (200, "deleted", "POST", {"op": "delete", "id": 7, "text": "ignored", "status": "x"}))
check("and leaves one line, with which memory and none of its words", log.strip(),
      "http: /api/douglas/memory from 127.0.0.1: delete of memory 7")
code, out, log = post("/api/douglas/memory", {"op": "correct", "id": 99, "text": "Sam takes his coffee with oat milk."})
check("a refusal comes back as his memory worded it", (code, out), (200, {"ok": False, "text": "There is no such memory."}))
check("and the journal has neither the old words nor the new", log.strip(),
      "http: /api/douglas/memory from 127.0.0.1: correct of memory 99 refused")
n = len(ASKED)
code, out, log = post("/api/douglas/memory", {"op": "list\nhttp: forged line", "id": "7\nhttp: forged"})
check("a POST cannot look, and what it sent cannot forge a line",
      (code, len(ASKED) - n, log.strip()), (400, 0, "http: /api/douglas/memory from 127.0.0.1: not a change refused"))
code, out = look("op=notices&limit=5")
check("the notices are read like the rest of what is kept on Douglas", (code, ASKED[-1][0], ASKED[-1][3]),
      (200, "GET", {"op": "notices", "limit": "5"}))
code, out, log = post("/api/douglas/memory", {"op": "notice_ack", "id": 4})
check("one is acknowledged, and the journal says which notice", (ASKED[-1][3], log.strip()),
      ({"op": "notice_ack", "id": 4}, "http: /api/douglas/memory from 127.0.0.1: notice_ack of notice 4 refused"))
code, out, log = post("/api/douglas/memory", {"op": "notice_prefs", "severity": "critical", "aloud": True,
                                             "quiet_from": 23, "text": ["x"], "key": "gone:node1"})
check("and where a severity goes is said, with only what that may carry",
      ASKED[-1][3], {"op": "notice_prefs", "severity": "critical", "aloud": True, "quiet_from": 23})
n = len(ASKED)
check("a notice cannot be raised or cleared from a screen: that is the watch's, on Douglas",
      ([post("/api/douglas/memory", {"op": op, "key": "gone:node1", "severity": "critical", "text": "Made up."})[0]
        for op in ("notice_raise", "notice_clear", "notice_told")], look("op=notice_clear&key=gone:node1")[0],
       len(ASKED) - n), ([400, 400, 400], 400, 0))
check("nor ask what only he asks of his memory",
      [post("/api/douglas/memory", {"op": op, "text": "The sky is green."})[0] for op in ("remember", "consolidate", "forget", "log")],
      [400] * 4)
# An answer to what is waiting for a yes, and the owner's switches.
code, out, log = post("/api/douglas/approval", {"id": "a1b2c3d4", "yes": True, "via": "phone", "text": "push everything"})
check("a yes given on a screen is passed to his agent as the number of what it answers, a yes, and which screen: "
      "and the journal has that one was given, not to what",
      (code, ASKED[-1][0], ASKED[-1][1], ASKED[-1][3], log.strip()),
      (200, "POST", "/approval", {"id": "a1b2c3d4", "yes": True, "via": "phone"},
       "http: /api/douglas/approval from 127.0.0.1: yes refused"))
code, out, log = post("/api/douglas/approval", {"id": "a1b2c3d4", "yes": False, "via": "the moon"})
check("a no likewise, from the panel unless it says the phone", (ASKED[-1][3], log.strip()),
      ({"id": "a1b2c3d4", "yes": False, "via": "deck"}, "http: /api/douglas/approval from 127.0.0.1: no refused"))
n = len(ASKED)
check("what is not an answer is not passed on: no number, or a yes that only looks like one",
      ([post("/api/douglas/approval", b)[0] for b in ({}, {"yes": True}, {"id": "a1b2c3d4"}, {"id": "a1b2c3d4", "yes": "true"},
                                                       {"id": "a1b2c3d4", "yes": 1}, {"id": 5, "yes": True})],
       len(ASKED) - n), ([400] * 6, 0))
code, out, log = post("/api/douglas/chat", {"op": "rename", "id": "a1b2c3d4e5f6", "title": "  The   boiler ", "x": 1})
check("what is done with one of his conversations is passed to his agent as which thing, its number and a name; "
      "the journal has which thing, and not the name",
      (code, ASKED[-1][1], ASKED[-1][3], log.strip()),
      (200, "/chat", {"op": "rename", "id": "a1b2c3d4e5f6", "title": "The boiler"},
       "http: /api/douglas/chat from 127.0.0.1: rename refused"))
n = len(ASKED)
check("what is not one of the four things, or no number of his, is not passed on",
      ([post("/api/douglas/chat", b)[0] for b in ({}, {"op": "burn"}, {"op": "switch", "id": "../../etc"},
                                                   {"op": "delete", "id": 5}, {"op": "new", "title": 9})],
       len(ASKED) - n), ([400] * 5, 0))
code, out, log = post("/api/douglas/switch", {"tool": "search", "on": False, "op": "write"})
check("a switch is passed on as the tool and the way, and the journal has both",
      (code, ASKED[-1][1], ASKED[-1][3], log.strip()),
      (200, "/switch", {"tool": "search", "on": False}, "http: /api/douglas/switch from 127.0.0.1: search off refused"))
n = len(ASKED)
check("what is not a switch is not passed on: no tool, a name that is not one, an on that is not true or false",
      ([post("/api/douglas/switch", b)[0] for b in ({}, {"tool": "search"}, {"tool": "search", "on": "off"},
                                                     {"tool": "../etc", "on": True}, {"tool": "Search Engine", "on": True},
                                                     {"tool": ["search"], "on": True})], len(ASKED) - n), ([400] * 6, 0))
d.DECK_TOKEN = "deck-token"
away = {"X-Forwarded-For": "100.64.0.9"}
check("from anywhere but the panel itself, his memory wants the deck token",
      (look("op=list", away)[0], look("op=list&token=deck-token", away)[0], look("op=list&token=wrong", away)[0]),
      (403, 200, 403))
def owners(path, query, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}{query}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.9"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
n = len(ASKED)
check("so do an answer and a switch: from another machine, nothing without the deck's token",
      ([owners(p, q, b) for p, b in (("/api/douglas/approval", {"id": "a1b2c3d4", "yes": True}),
                                     ("/api/douglas/switch", {"tool": "search", "on": True}))
        for q in ("", "?token=wrong", "?token=deck-token")], len(ASKED) - n), ([403, 403, 200, 403, 403, 200], 2))
def change(query, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/douglas/memory{query}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.9"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
n = len(ASKED)
check("and so does changing it", (change("", {"op": "purge", "id": 7}), len(ASKED) - n,
                                  change("?token=deck-token", {"op": "purge", "id": 7}), len(ASKED) - n), (403, 0, 200, 1))
d.DECK_TOKEN = ""
mem.shutdown()
d.DOUGLAS_AGENT_PORT = 1
check("Douglas not answering", look("op=stats"), (502, {"ok": False, "text": "douglas-agent did not answer"}))
token, d.DOUGLAS_AGENT_TOKEN = d.DOUGLAS_AGENT_TOKEN, ""
check("no agent token set up", look("op=stats")[1]["text"], "DOUGLAS_AGENT_TOKEN unset in boot.env")
d.DOUGLAS_AGENT_TOKEN = token

print("-- older lines of his conversation, for a screen scrolled up --")
TOLD = []
class Record(d.BaseHTTPRequestHandler):
    """Douglas's agent, as far as its /transcript goes."""
    def log_message(self, *a): pass
    def do_GET(self):
        u = d.urlparse(self.path); q = {k: v[0] for k, v in d.parse_qs(u.query).items()}
        TOLD.append((u.path, q.pop("token", ""), q))
        out = {"ok": True, "more": True, "lines": [{"ts": 1700000000.5, "who": "user", "text": "What is on the shelf?",
                                                   "via": "phone"}]}
        body = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
rec = d.ThreadingHTTPServer(("127.0.0.1", 0), Record)
threading.Thread(target=rec.serve_forever, daemon=True).start()
d.DOUGLAS_AGENT_PORT = rec.server_address[1]
def earlier(query, headers=None):
    """(status, reply, what the journal got) for one GET."""
    journal, real = io.StringIO(), (sys.stdout, sys.stderr)
    sys.stdout = sys.stderr = journal
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/douglas/transcript?{query}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
    finally:
        sys.stdout, sys.stderr = real
    return code, out, journal.getvalue()
code, out, log = earlier("before=1700000100.25&limit=30")
check("the agent's older lines come back as it gave them", (code, out["more"], out["lines"][0]["text"]),
      (200, True, "What is on the shelf?"))
check("asked of the agent with the agent's token and the two numbers",
      TOLD[-1], ("/transcript", d.DOUGLAS_AGENT_TOKEN, {"before": "1700000100.25", "limit": "30"}))
check("and none of his words reach the journal", "shelf" in log, False)
earlier("before=1%26token%3Dx&limit=9999&junk=1")
check("only numbers are passed on, the limit kept to a hundred", TOLD[-1][2], {"limit": "100"})
earlier("before=inf&limit=x")
check("a number that is not a time is dropped", TOLD[-1][2], {})
d.DECK_TOKEN, d.DECK_READ_TOKEN = "deck-token", "read-token"
n = len(TOLD)
check("from anywhere but the panel itself, it wants the deck token, and the read token does not open it",
      ([earlier(f"before=5{t}", away)[0] for t in ("", "&token=read-token", "&token=wrong", "&token=deck-token")],
       len(TOLD) - n), ([403, 403, 403, 200], 1))
d.DECK_TOKEN, d.DECK_READ_TOKEN = "", ""
rec.shutdown()
d.DOUGLAS_AGENT_PORT = 1
check("Douglas not answering", earlier("before=5")[:2], (502, {"ok": False, "text": "douglas-agent did not answer"}))

server.shutdown()
print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("all passed")
