#!/usr/bin/env python3
"""DougRing through Mission Control: who each token opens, what reaches
dougring's socket and with which role. dougring itself is stood in for; its
own rules are tested in dougring/test_dougring.py. No Pi needed."""
import json, os, pathlib, socket, sys, tempfile, threading, urllib.error, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ.update({"DEFAULT_SURFACE": "evals", "DECK_TOKEN": "the-deck-token",
                   "DECK_READ_TOKEN": "the-read-token", "DECK_CAMERA_TOKEN": "the-camera-token"})
import deck_api as d

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

# dougring, stood in for: it records what it is asked and by whom.
tmp = tempfile.mkdtemp()
d.DOUGRING_SOCKET = os.path.join(tmp, "api.sock")
ASKED = []
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 3000 + b"\xff\xd9"
OBS = {"camera_id": "cam1", "camera_name": "Driveway", "source": "live-triggered", "age_s": 4}

def ring_side():
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(d.DOUGRING_SOCKET)
    srv.listen(8)
    while True:
        c, _ = srv.accept()
        f = c.makefile("rb")
        first = f.readline().decode().split()
        hdrs = {}
        while (line := f.readline().decode().strip()):
            k, _, v = line.partition(":")
            hdrs[k.lower()] = v.strip()
        body = f.read(int(hdrs.get("content-length", 0)))
        ASKED.append((first[0], first[1], hdrs.get("x-dougring-role"), json.loads(body or b"{}")))
        if first[1] == "/image":
            if json.loads(body).get("camera") == "nope":
                out = b'{"error": "not a camera that is switched on"}'
                c.sendall(b"HTTP/1.0 403 Forbidden\r\nContent-Type: application/json\r\n\r\n" + out)
            else:
                c.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\nX-Observation: "
                          + json.dumps(OBS).encode() + b"\r\n\r\n" + JPEG)
        else:
            c.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n"
                      + json.dumps({"global_on": True, "cameras": [{"id": "cam1"}], "journal": []}).encode())
        f.close()
        c.close()
threading.Thread(target=ring_side, daemon=True).start()
import time; time.sleep(0.2)

server = d.ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

def call(method, path, body=None, away=True):
    """(status, headers, body). away: as if from another machine, not the panel."""
    h = {"Content-Type": "application/json"}
    if away:
        h["X-Forwarded-For"] = "100.64.0.9"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

print("-- Douglas's camera token --")
code, h, img = call("POST", "/api/eyes/image?token=the-camera-token", {"camera": "Driveway"})
check("a picture", (code, img == JPEG, h.get("Content-Type")), (200, True, "image/jpeg"))
check("with what it is", json.loads(h["X-Observation"])["camera_name"], "Driveway")
check("asked of dougring as Douglas", ASKED[-1][:3], ("POST", "/image", "douglas"))
code, _, body = call("GET", "/api/eyes/cameras?token=the-camera-token")
check("his cameras", (code, ASKED[-1][:3]), (200, ("GET", "/cameras", "douglas")))
code, _, body = call("POST", "/api/eyes/image?token=the-camera-token", {"camera": "nope"})
check("a refusal passes through", (code, json.loads(body)["error"]), (403, "not a camera that is switched on"))
n = len(ASKED)
for path, method in (("/api/cameras", "GET"), ("/api/cameras", "POST"), ("/api/cameras/image", "POST"),
                     ("/api/fleet", "GET"), ("/api/state", "GET"), ("/api/douglas/memory", "GET")):
    code, _, _ = call(method, path + "?token=the-camera-token", {"op": "switch", "on": True} if method == "POST" else None)
    check(f"opens nothing else: {method} {path}", code, 403)
check("and nothing reached dougring", len(ASKED), n)

print("-- the other tokens on Douglas's routes --")
for tok in ("the-read-token", "the-deck-token", "", "wrong"):
    code, _, _ = call("POST", f"/api/eyes/image?token={tok}", {"camera": "Driveway"})
    check(f"eyes image refuses {tok or 'no token'}", code, 403)
    code, _, _ = call("GET", f"/api/eyes/cameras?token={tok}")
    check(f"eyes cameras refuses {tok or 'no token'}", code, 403)
check("nothing reached dougring", len(ASKED), n)

print("-- the owner --")
code, _, body = call("GET", "/api/cameras?token=the-deck-token")
v = json.loads(body)
check("the panel's reading", (code, v["up"], [a[1] for a in ASKED[-3:]]), (200, True, ["/status", "/cameras", "/journal"]))
check("asked as the owner", {a[2] for a in ASKED[-3:]}, {"owner"})
code, _, _ = call("POST", "/api/cameras?token=the-deck-token", {"op": "switch", "on": False})
check("the global switch", (code, ASKED[-1][1], ASKED[-1][3]), (200, "/switch", {"on": False}))
call("POST", "/api/cameras?token=the-deck-token", {"op": "enable", "id": "cam/1"})
check("a camera's switch, id quoted", ASKED[-1][1], "/cameras/cam%2F1/enable")
call("POST", "/api/cameras?token=the-deck-token", {"op": "name", "id": "cam1", "name": "x" * 90})
check("a name, kept short", (ASKED[-1][1], len(ASKED[-1][3]["name"])), ("/cameras/cam1/name", 40))
code, _, _ = call("POST", "/api/cameras?token=the-deck-token", {"op": "reboot"})
check("no other op", code, 400)
code, h, img = call("POST", "/api/cameras/image?token=the-deck-token", {"camera": "cam1"})
check("a picture for the owner", (code, img == JPEG, ASKED[-1][2]), (200, True, "owner"))
code, _, _ = call("GET", "/api/cameras?token=the-read-token")
check("the fleet's read token opens no camera route", code, 403)
code, _, _ = call("GET", "/api/cameras", away=False)
check("the panel itself needs no token", code, 200)

print("-- dougring not there --")
d.DOUGRING_SOCKET = os.path.join(tmp, "gone.sock")
code, _, body = call("GET", "/api/cameras?token=the-deck-token")
check("said plainly", (code, json.loads(body)["installed"]), (200, False))
code, _, _ = call("POST", "/api/eyes/image?token=the-camera-token", {"camera": "Driveway"})
check("a picture cannot be had", code, 503)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
