#!/usr/bin/env python3
"""Push to the phone app: which notices are news, how a phone is registered
and forgotten, the token Apple is spoken to with, and what is kept out of the
journal. No Pi, no Douglas and no Apple needed: the one call that would leave
the machine is stood in for."""
import io, json, os, pathlib, shutil, subprocess, sys, tempfile, threading, urllib.error, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["DEFAULT_SURFACE"] = "evals"
os.environ["DECK_PUSH_FILE"] = str(tmp / "push.json")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_TOPIC"):
    os.environ.pop(k, None)
import deck_api as d

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

server = d.ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

def call(path, body=None, headers=None):
    """(status, reply, what the journal got) for one request."""
    journal, real = io.StringIO(), sys.stderr
    sys.stderr = journal
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     json.dumps(body).encode() if body is not None else None,
                                     dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
    finally:
        sys.stderr = real
    return code, out, journal.getvalue()

A, B = "a" * 64, "b" * 64

print("-- which notices are news --")
rows = [{"id": 1, "severity": "warning"}, {"id": 2, "severity": "information"},
        {"id": 3, "severity": "critical", "acked": True}]
due, seen = d.push_due(rows, None)
check("the first look sends nothing", due, [])
check("and remembers what is open", seen, {1, 2, 3})
due, seen = d.push_due(rows + [{"id": 4, "severity": "critical", "text": "disk"}], seen)
check("a new critical one is sent", [n["id"] for n in due], [4])
due, seen = d.push_due(rows + [{"id": 4, "severity": "critical"}], seen)
check("once", due, [])
due, seen = d.push_due(rows + [{"id": 5, "severity": "information"}, {"id": 6, "severity": "warning", "acked": True}], seen)
check("information is not pushed, nor one already seen on a screen", due, [])
due, seen = d.push_due(rows, seen)
due, seen = d.push_due(rows + [{"id": 4, "severity": "critical"}], seen)
check("one that was over and is back is news again", [n["id"] for n in due], [4])
check("a row that is not a notice is passed over", d.push_due(["x", {"severity": "critical"}], set()), ([], set()))

print("-- nothing is set up --")
code, out, _ = call("/api/push")
check("it says so, and why", (code, out["configured"], out["why"].split(" unset")[0], out["devices"]),
      (200, False, "APNS_KEY_FILE, APNS_KEY_ID, APNS_TEAM_ID, APNS_TOPIC", 0))
check("and pushes nothing", d.push("t", "b", "test"), {"sent": 0, "failed": 0, "why": out["why"]})

print("-- a phone is registered --")
code, out, log = call("/api/push/register", {"token": A.upper(), "env": "production", "model": "iPhone\n17"})
check("it is taken", (code, out["ok"]), (200, True))
check("kept in lower case, with its service and model", [(r["token"], r["env"], r["model"]) for r in d.push_devices()],
      [(A, "production", "iPhone 17")])
check("the journal has that, on one line", log.strip(), "http: /api/push/register from 127.0.0.1: registered")
check("and not the token", A in log.lower(), False)
check("the file is the owner's alone", oct(os.stat(tmp / "push.json").st_mode & 0o777), "0o600")
call("/api/push/register", {"token": A, "env": "nonsense"})
check("the same phone again is one line, not two", [(r["token"], r["env"]) for r in d.push_devices()],
      [(A, "development")])
code, out, log = call("/api/push/register", {"token": "not-a-token"})
check("something that is not a token is refused", (code, out["ok"], log.strip().endswith("registered refused")),
      (400, False, True))
code, out, _ = call("/api/push", headers={"X-Push-Device": A})
check("the phone is told it is among them", (out["devices"], out["here"]), (1, True))
code, out, _ = call("/api/push", headers={"X-Push-Device": B})
check("and another that it is not", out["here"], False)

print("-- sending --")
d.APNS_KEY_FILE, d.APNS_KEY_ID, d.APNS_TEAM_ID, d.APNS_TOPIC = __file__, "KEYID12345", "TEAMID1234", "com.example.deck"
call("/api/push/register", {"token": B, "env": "development"})
sent = []
answers = {}
def fake_send(token, env, payload, collapse=""):
    sent.append((token[0], env, payload["aps"]["alert"]["title"], payload["kind"], collapse))
    return answers.get((token[0], env), (200, ""))
d.apns_send = fake_send
check("configured now", d.push_why(), "")
got = d.push("WARNING · disk", "the disk is nearly full", "notice", "notice-4")
check("one to each phone", (got["sent"], got["failed"], sent),
      (2, 0, [("a", "development", "WARNING · disk", "notice", "notice-4"),
              ("b", "development", "WARNING · disk", "notice", "notice-4")]))
sent.clear()
answers[("a", "development")] = (400, "BadDeviceToken")
got = d.push("t", "b", "test")
check("a phone on Apple's other service is found there", (got["sent"], [s[:2] for s in sent]),
      (2, [("a", "development"), ("a", "production"), ("b", "development")]))
check("and remembered", {r["token"][0]: r["env"] for r in d.push_devices()}, {"a": "production", "b": "development"})
answers[("b", "development")] = (410, "Unregistered")
got = d.push("t", "b", "test")
check("a phone Apple says is gone is counted", (got["sent"], got["failed"], got["why"]), (1, 1, "Unregistered"))
check("and dropped", [r["token"][0] for r in d.push_devices()], ["a"])
answers[("a", "production")] = (0, "")
code, out, log = call("/api/push/test", {})
check("a test that reached nobody says so", (code, out["ok"], out["message"]), (502, False, "Apple was not reached"))
check("the journal has the count", log.strip().splitlines()[-1], "http: /api/push/test from 127.0.0.1: test to 0, 1 refused")

print("-- a phone is forgotten --")
code, out, log = call("/api/push/forget", {"token": A})
check("it is gone", (code, out["ok"], d.push_devices()), (200, True, []))
check("on one journal line", log.strip(), "http: /api/push/forget from 127.0.0.1: forgotten")

print("-- the signature --")
check("two short integers become 64 bytes", d.der_to_raw(bytes([0x30, 6, 2, 1, 5, 2, 1, 7])),
      b"\x00" * 31 + b"\x05" + b"\x00" * 31 + b"\x07")
check("a leading zero is not part of the number",
      d.der_to_raw(bytes([0x30, 70, 2, 33, 0]) + b"\x80" * 32 + bytes([2, 33, 0]) + b"\x90" * 32),
      b"\x80" * 32 + b"\x90" * 32)
check("something else is not a signature", (d.der_to_raw(b""), d.der_to_raw(b"\x31\x00"), d.der_to_raw(b"\x30\x03\x04\x01\x00")),
      (None, None, None))
check("what goes into a curl config is quoted", d._curl_quoted('a "b" \\n'), '"a \\"b\\" \\\\n"')
if shutil.which("openssl"):
    key = tmp / "key.p8"
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key)],
                   check=True, capture_output=True)
    d.APNS_KEY_FILE = str(key)
    d._apns_jwt.update(at=-1e9, value=None)
    jwt = d.apns_jwt() or ""
    parts = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    import base64
    check("the token has its three parts", len(parts), 3)
    check("says which key and whose", (json.loads(base64.urlsafe_b64decode(pad(parts[0]))),
                                         json.loads(base64.urlsafe_b64decode(pad(parts[1])))["iss"]),
          ({"alg": "ES256", "kid": "KEYID12345"}, "TEAMID1234"))
    check("and a signature of 64 bytes", len(base64.urlsafe_b64decode(pad(parts[2]))), 64)
    # openssl checks it against the same key, with the signature put back as it wrote it.
    raw = base64.urlsafe_b64decode(pad(parts[2]))
    def der_int(b):
        b = b.lstrip(b"\x00") or b"\x00"
        if b[0] & 0x80: b = b"\x00" + b
        return bytes([2, len(b)]) + b
    body = der_int(raw[:32]) + der_int(raw[32:])
    (tmp / "sig").write_bytes(bytes([0x30, len(body)]) + body)
    (tmp / "msg").write_bytes(f"{parts[0]}.{parts[1]}".encode())
    pub = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(tmp / "pub")], capture_output=True)
    ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(tmp / "pub"), "-signature", str(tmp / "sig"),
                         str(tmp / "msg")], capture_output=True)
    check("which the key's other half accepts", ok.returncode, 0)
    check("it is kept for the next one", d.apns_jwt(), jwt)
else:
    print("  SKIP  no openssl here: the token is not made")

server.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
