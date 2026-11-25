#!/usr/bin/env python3
"""Tests for dougring.py against a stand-in for Ring's servers.

The stand-in plays oauth.ring.com and api.amazonvision.com as Ring's docs
describe them (read 2026-11-25): a form-encoded token endpoint, users/me, the
app-integrations POST (which recomputes the nonce the way Ring does) and PATCH,
devices, and the two-step image download. These are mocked tests: they show
DougRing follows the documented flow, not that Ring behaves as documented.

Run:  python3 -m unittest dougring/test_dougring.py
"""
import base64
import hashlib
import hmac
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dougring  # noqa: E402

CLIENT_ID = "test-client-id-0123456789abcd"
CLIENT_SECRET = "test-client-secret-" + "s" * 40
HMAC_KEY = "test-hmac-key-" + "k" * 30
ACCOUNT_ID = "acct-7f3a9c21"
EMAIL = "frankie@example.com"
CREDS = {"RING_CLIENT_ID": CLIENT_ID, "RING_CLIENT_SECRET": CLIENT_SECRET, "RING_HMAC_KEY": HMAC_KEY}
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 50 + b"\xff\xd9"


def ring_nonce(t_ms, account_id):
    mac = hmac.new(HMAC_KEY.encode(), f"{t_ms}:{account_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


class FakeRing:
    """Ring's side, with a record of what it was sent."""

    def __init__(self):
        self.calls = []
        self.codes = {"good-code": ACCOUNT_ID}
        self.valid_access = set()
        self.valid_refresh = set()
        self.link_time = None          # Ring's stored link start time
        self.integration = None
        self.n = 0
        self.revoked = False
        self.reject_next_access = False
        self.not_ready = 0             # 416s before a live-triggered picture is ready
        self.throttle = 0              # 429s still to give
        self.recorded_after_live = True
        self.live_started = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj=None, headers=None, raw=None):
                body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self):
                tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                ok = (tok in fake.valid_access and not fake.revoked
                      and self.headers.get("User-Agent", "").startswith("DougRing/")
                      and self.headers.get("Hardware_Id"))
                if ok and fake.reject_next_access:
                    fake.reject_next_access = False
                    fake.valid_access.discard(tok)
                    return False
                return ok

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                fake.calls.append(("POST", self.path, dict(self.headers), raw))
                if self.path == "/oauth/token":
                    if self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                        return self._send(400, {"error": "invalid_request"})
                    f = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                    if f.get("client_id") != CLIENT_ID or f.get("client_secret") != CLIENT_SECRET:
                        return self._send(401, {"error": "invalid_client"})
                    if f.get("grant_type") == "authorization_code" and f.get("code") in fake.codes:
                        fake.codes.pop(f["code"])
                    elif f.get("grant_type") == "refresh_token" and f.get("refresh_token") in fake.valid_refresh and not fake.revoked:
                        fake.valid_refresh.discard(f["refresh_token"])   # rotated
                    else:
                        return self._send(400, {"error": "invalid_grant"})
                    fake.n += 1
                    a, r = f"access-{fake.n}", f"refresh-{fake.n}"
                    fake.valid_access.add(a)
                    fake.valid_refresh.add(r)
                    return self._send(200, {"access_token": a, "refresh_token": r, "expires_in": 14400,
                                            "token_type": "Bearer", "scope": "ava"})
                if not self._auth():
                    return self._send(401, {"errors": [{"status": "401", "title": "Invalid Client"}]})
                if self.path == "/v1/accounts/me/app-integrations":
                    body = json.loads(raw)
                    if body.get("nonce") != ring_nonce(fake.link_time, ACCOUNT_ID):
                        return self._send(400, {"errors": [{"status": "400", "code": "INVALID_NONCE"}]})
                    fake.integration = {"status": "awaiting", "account_identifier": body["account_identifier"]}
                    return self._send(200, {"status": "awaiting"})
                if self.path.endswith("/media/image/download"):
                    body = json.loads(raw)
                    if fake.throttle:
                        fake.throttle -= 1
                        return self._send(429, {"errors": [{"status": "429"}]}, {"Retry-After": "7"})
                    if body.get("type") not in ("latest_in_range", "at_timestamp"):
                        return self._send(400, {"errors": [{"code": "INVALID_PAYLOAD"}]})
                    if body["type"] == "at_timestamp":
                        if not fake.live_started or not fake.recorded_after_live:
                            return self._send(416, {"errors": [{"code": "MEDIA_NOT_FOUND"}]})
                        if fake.not_ready:
                            fake.not_ready -= 1
                            return self._send(425, {"errors": [{"code": "RECORDING_NOT_READY"}]})
                        fake.picture_ts = body["timestamp"]
                    else:
                        fake.picture_ts = int(time.time() * 1000) - 3 * 3600 * 1000
                    loc = f"http://127.0.0.1:{fake.port}/presigned?security_token=xyz"
                    return self._send(303, headers={"Location": loc})
                self._send(404, {"errors": [{"status": "404"}]})

            def do_PATCH(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                fake.calls.append(("PATCH", self.path, dict(self.headers), raw))
                if not self._auth():
                    return self._send(401, {})
                body = json.loads(raw)
                if fake.integration is None or body.get("status") != "completed":
                    return self._send(400, {})
                fake.integration["status"] = "completed"
                self._send(200, {**fake.integration, "updated_at": "2026-11-25T00:00:00Z"})

            def do_GET(self):
                fake.calls.append(("GET", self.path, dict(self.headers), b""))
                if self.path.startswith("/presigned"):
                    if self.headers.get("Authorization"):
                        return self._send(400, {"error": "token sent to presigned url"})
                    return self._send(200, raw=JPEG, headers={"Content-Type": "image/jpeg",
                                                              "X-Media-Timestamp": str(fake.picture_ts),
                                                              "X-Media-Origin": "recording"})
                if not self._auth():
                    return self._send(401, {})
                if self.path == "/v1/users/me":
                    return self._send(200, {"data": {"type": "users", "id": ACCOUNT_ID,
                                                     "attributes": {"first_name": "F", "last_name": "M", "email": EMAIL}}})
                if self.path.startswith("/v1/devices"):
                    def dev(i, name):
                        return {"type": "devices", "id": i, "attributes": {"name": name}, "relationships": {
                            "status": {"data": {"type": "device-status", "id": "s-" + i}},
                            "capabilities": {"data": {"type": "device-capabilities", "id": "c-" + i}}}}
                    return self._send(200, {
                        "data": [dev("dev-front-001", "Front Yard"), dev("dev-door-002", "Front Door"),
                                 dev("sensor-003", "Back Door Contact")],
                        "included": [
                            {"type": "device-status", "id": "s-dev-front-001", "attributes": {"online": True}},
                            {"type": "device-capabilities", "id": "c-dev-front-001", "attributes": {"video": {"codecs": ["AVC"]}}},
                            {"type": "device-status", "id": "s-dev-door-002",
                             "attributes": {"online": fake.door_online, "battery_status": {"percentage": 80}}},
                            {"type": "device-capabilities", "id": "c-dev-door-002",
                             "attributes": {"video": {}, "button_press": {}}},
                            {"type": "device-status", "id": "s-sensor-003", "attributes": {"online": True}},
                            {"type": "device-capabilities", "id": "c-sensor-003", "attributes": {}}]})
                self._send(404, {})

        self.door_online = False
        self.picture_ts = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        # Ring's RTSP bridge, as its docs describe the signalling (plain TCP here).
        rtsp = socket.socket()
        rtsp.bind(("127.0.0.1", 0))
        rtsp.listen(4)
        self.rtsp = rtsp
        self.rtsp_port = rtsp.getsockname()[1]
        self.rtsp_seen = []
        threading.Thread(target=self._rtsp_loop, daemon=True).start()
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _rtsp_loop(self):
        while True:
            try:
                conn, _ = self.rtsp.accept()
            except OSError:
                return
            threading.Thread(target=self._rtsp_conn, args=(conn,), daemon=True).start()

    def _rtsp_conn(self, conn):
        f = conn.makefile("rb")
        client_port = None
        try:
            while True:
                first = f.readline().decode()
                if not first:
                    return
                hdrs = {}
                while (line := f.readline().decode().strip()):
                    k, _, v = line.partition(":")
                    hdrs[k.strip().lower()] = v.strip()
                method = first.split()[0]
                self.rtsp_seen.append((method, dict(hdrs), first.split()[1]))
                cseq = hdrs.get("cseq", "0")
                if method == "DESCRIBE":
                    if hdrs.get("x-auth-token") not in self.valid_access or self.revoked:
                        conn.sendall(f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n\r\n".encode())
                        continue
                    sdp = b"v=0\r\nm=video 9 RTP/SAVP 127\r\n"
                    conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: sess-1;timeout=60\r\n"
                                 f"Content-Length: {len(sdp)}\r\n\r\n".encode() + sdp)
                elif method == "SETUP":
                    client_port = int(hdrs["transport"].split("client_port=")[1].split("-")[0])
                    conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: sess-1\r\nTransport: "
                                 f"RTP/SAVP;unicast;client_port={client_port}-{client_port+1};"
                                 f"server_port=40000-40001;source=127.0.0.1\r\n\r\n".encode())
                elif method == "PLAY":
                    self.live_started.append(time.time())
                    conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: sess-1\r\n\r\n".encode())
                    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    for _ in range(5):
                        u.sendto(b"\x80" + b"x" * 100, ("127.0.0.1", client_port))
                    u.close()
                elif method == "TEARDOWN":
                    conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: sess-1\r\n\r\n".encode())
                    return
        finally:
            conn.close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"


class DougRingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeRing()
        ring = dougring.Ring(CREDS, oauth_url=self.fake.base + "/oauth/token", api_url=self.fake.base, timeout=5, allow_http=True)
        ring.rtsp_host, ring.rtsp_port, ring.rtsp_plain = "127.0.0.1", self.fake.rtsp_port, True
        self.old_consts = (dougring.LIVE_HOLD_S, dougring.FRESH_WAIT_S, dougring.FRESH_POLL_S)
        dougring.LIVE_HOLD_S, dougring.FRESH_WAIT_S, dougring.FRESH_POLL_S = 0.5, 3, 0.2
        self.store = dougring.Store(os.path.join(self.tmp.name, "state.json"))
        self.app = dougring.DougRing(CREDS, self.store, ring, webhooks=True)
        self.log = io.StringIO()
        self._stderr = redirect_stderr(self.log)
        self._stderr.__enter__()
        self.old_wait = dougring.LINK_WAIT_S
        dougring.LINK_WAIT_S = 1
        self.pub = dougring.BoundedHTTPServer(("127.0.0.1", 0), dougring.public_handler(self.app))
        threading.Thread(target=self.pub.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.pub.server_address[1]}"
        self.sock = os.path.join(self.tmp.name, "api.sock")
        self.priv = dougring.UnixHTTPServer(self.sock, dougring.private_handler(self.app))
        threading.Thread(target=self.priv.serve_forever, daemon=True).start()

    def tearDown(self):
        self._stderr.__exit__(None, None, None)
        dougring.LINK_WAIT_S = self.old_wait
        dougring.LIVE_HOLD_S, dougring.FRESH_WAIT_S, dougring.FRESH_POLL_S = self.old_consts
        self.fake.rtsp.close()
        for s in (self.pub, self.priv, self.fake.server):
            s.shutdown()
            s.server_close()
        self.tmp.cleanup()

    # helpers
    def http(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def private(self, method, path, body=b"", role="owner"):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.sock)
        s.sendall(f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n".encode()
                  + (f"X-DougRing-Role: {role}\r\n".encode() if role else b"") + b"\r\n" + body)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        s.close()
        head, _, payload = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), head.decode("latin-1"), payload

    def send_code(self, code="good-code", client_id=CLIENT_ID):
        return self.http("POST", "/ring/token",
                         urllib.parse.urlencode({"code": code, "client_id": client_id}).encode(),
                         {"Content-Type": "application/x-www-form-urlencoded"})

    def link(self):
        t = int(time.time() * 1000)
        self.fake.link_time = t
        return self.http("GET", f"/ring/link?nonce={ring_nonce(t, ACCOUNT_ID)}&time={t}")

    def linked(self):
        self.assertEqual(self.send_code()[0], 200)
        status, page = self.link()
        self.assertEqual(status, 200, page)
        return page

    # the documented one-way flow
    def test_full_link_flow(self):
        page = self.linked()
        self.assertIn("f***e@example.com", page)
        self.assertNotIn(EMAIL, page)
        self.assertEqual(self.fake.integration, {"status": "completed", "account_identifier": "f***e@example.com"})
        posts = [c for c in self.fake.calls if c[1] == "/v1/accounts/me/app-integrations"]
        self.assertEqual([c[0] for c in posts], ["POST", "PATCH"])
        token_call = next(c for c in self.fake.calls if c[1] == "/oauth/token")
        form = urllib.parse.parse_qs(token_call[3].decode())
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(self.app.status()["accounts"][0]["state"], "linked")

    def test_state_file_private_and_logs_clean(self):
        self.linked()
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        self.assertEqual(mode, 0o600)
        logs = self.log.getvalue()
        for secret in (CLIENT_SECRET, HMAC_KEY, "access-1", "refresh-1", "good-code", EMAIL, ACCOUNT_ID):
            self.assertNotIn(secret, logs)
        self.assertIn("completed", logs)

    def test_token_endpoint_wants_a_form(self):
        st, _ = self.http("POST", "/ring/token", json.dumps({"code": "good-code"}).encode(),
                          {"Content-Type": "application/json"})
        self.assertEqual(st, 415)

    def test_token_endpoint_refuses_other_client(self):
        self.assertEqual(self.send_code(client_id="someone-else")[0], 400)

    def test_bad_code_is_not_2xx(self):
        self.assertEqual(self.send_code(code="nope")[0], 502)

    def test_expired_and_future_links(self):
        self.send_code()
        old = int(time.time() * 1000) - 601_000
        self.assertEqual(self.http("GET", f"/ring/link?nonce={ring_nonce(old, ACCOUNT_ID)}&time={old}")[0], 400)
        fut = int(time.time() * 1000) + 120_000
        self.assertEqual(self.http("GET", f"/ring/link?nonce={ring_nonce(fut, ACCOUNT_ID)}&time={fut}")[0], 400)

    def test_wrong_nonce_is_refused(self):
        self.send_code()
        t = int(time.time() * 1000)
        st, _ = self.http("GET", f"/ring/link?nonce={ring_nonce(t, 'someone-else')}&time={t}")
        self.assertEqual(st, 400)
        self.assertIsNone(self.fake.integration)

    def test_browser_supplied_email_is_ignored(self):
        self.send_code()
        t = int(time.time() * 1000)
        self.fake.link_time = t
        st, page = self.http("GET", f"/ring/link?nonce={ring_nonce(t, ACCOUNT_ID)}&time={t}&email=evil@x.com")
        self.assertEqual(st, 200)
        self.assertEqual(self.fake.integration["account_identifier"], "f***e@example.com")

    def test_probes_and_closed_paths(self):
        for p in ("/ring/token", "/ring/link", "/ring/webhook", "/ring/home"):
            self.assertEqual(self.http("GET", p)[0], 200, p)
        for p in ("/", "/status", "/devices", "/ring", "/ring/link/../status", "/cameras/x/snapshot"):
            self.assertEqual(self.http("GET", p)[0], 404, p)
        self.assertEqual(self.http("POST", "/ring/link", b"x")[0], 404)

    def test_webhook_closed_by_default(self):
        self.app.webhooks = False
        self.assertEqual(self.http("GET", "/ring/webhook")[0], 404)
        self.assertEqual(self.http("POST", "/ring/webhook", b"{}", {"X-Signature": "sha256=00"})[0], 404)

    def test_webhook_signature_dedupe_and_removal(self):
        self.linked()
        ev = json.dumps({"meta": {"version": "1.1", "request_id": "r1", "account_id": ACCOUNT_ID},
                         "data": {"id": "e1", "type": "motion_detected",
                                  "attributes": {"source": "dev-front-001", "timestamp": 1, "sub_type": "human"}}}).encode()
        sig = "sha256=" + hmac.new(HMAC_KEY.encode(), ev, hashlib.sha256).hexdigest()
        self.assertEqual(self.http("POST", "/ring/webhook", ev, {"X-Signature": "sha256=00"})[0], 401)
        self.assertEqual(self.http("POST", "/ring/webhook", ev, {"X-Signature": sig})[0], 200)
        self.assertEqual(self.http("POST", "/ring/webhook", ev, {"X-Signature": sig})[0], 200)
        self.assertEqual(len(self.store.data["events"]), 1)
        rm = json.dumps({"meta": {"request_id": "r2", "account_id": ACCOUNT_ID},
                         "data": {"type": "app_integration_removed", "attributes": {}}}).encode()
        sig = "sha256=" + hmac.new(HMAC_KEY.encode(), rm, hashlib.sha256).hexdigest()
        self.assertEqual(self.http("POST", "/ring/webhook", rm, {"X-Signature": sig})[0], 200)
        self.assertEqual(self.store.data["accounts"], {})

    # cameras, switches, pictures
    def ready(self):
        """Linked, the global switch on, the front camera on."""
        self.linked()
        self.assertEqual(self.private("POST", "/switch", {"on": True})[0], 200)
        self.assertEqual(self.private("POST", "/cameras/dev-front-001/enable")[0], 200)

    def image(self, camera="Front Yard", role="douglas", fresh=True):
        st, head, body = self.private("POST", "/image", {"camera": camera, "fresh": fresh}, role)
        obs = None
        for line in head.split("\r\n"):
            if line.lower().startswith("x-observation:"):
                obs = json.loads(line.split(":", 1)[1])
        return st, obs, body

    def test_cameras_only_not_sensors(self):
        self.linked()
        st, _, body = self.private("GET", "/cameras")
        self.assertEqual(st, 200)
        cams = json.loads(body)["cameras"]
        self.assertEqual([c["ring_name"] for c in cams], ["Front Yard", "Front Door"])
        door = cams[1]
        self.assertTrue(door["battery"] and door["doorbell"] and not door["online"])

    def test_live_triggered_picture_with_observation(self):
        self.ready()
        st, obs, img = self.image()
        self.assertEqual(st, 200)
        self.assertEqual(img, JPEG)
        self.assertEqual(obs["source"], "live-triggered")
        self.assertEqual(obs["camera_name"], "Front Yard")
        self.assertLessEqual(abs(obs["age_s"]), 10)
        self.assertTrue(obs["captured_ms"] and obs["retrieved_ms"])
        methods = [m for m, _, _ in self.fake.rtsp_seen]
        self.assertEqual(methods, ["DESCRIBE", "SETUP", "PLAY", "TEARDOWN"])
        desc = self.fake.rtsp_seen[0]
        self.assertIn("x-auth-token", desc[1])
        self.assertNotIn("access-", desc[2])            # the token is never in the URL

    def test_waits_for_recording_to_be_ready(self):
        self.ready()
        self.fake.not_ready = 3
        st, obs, _ = self.image()
        self.assertEqual((st, obs["source"]), (200, "live-triggered"))

    def test_stale_picture_is_marked_not_live(self):
        self.ready()
        self.fake.recorded_after_live = False     # no Protect recording, say
        st, obs, _ = self.image()
        self.assertEqual(st, 200)
        self.assertEqual(obs["source"], "latest existing")
        self.assertGreater(obs["age_s"], 3 * 3600 - 60)

    def test_global_switch_is_enforced(self):
        self.ready()
        self.private("POST", "/switch", {"on": False})
        self.assertEqual(self.image()[0], 403)
        self.assertEqual(self.image(role="owner")[0], 403)
        st, _, body = self.private("GET", "/cameras", role="douglas")
        self.assertEqual(json.loads(body)["cameras"], [])

    def test_camera_switch_is_enforced_and_immediate(self):
        self.ready()
        self.assertEqual(self.image()[0], 200)
        self.private("POST", "/cameras/dev-front-001/disable")
        self.assertEqual(self.image()[0], 403)
        self.assertEqual(self.image("dev-door-002")[0], 403)       # never switched on

    def test_douglas_sees_only_his_cameras_and_changes_nothing(self):
        self.ready()
        st, _, body = self.private("GET", "/cameras", role="douglas")
        self.assertEqual([c["id"] for c in json.loads(body)["cameras"]], ["dev-front-001"])
        for path, body in (("/switch", {"on": False}), ("/cameras/dev-door-002/enable", {}),
                           ("/cameras/dev-front-001/name", {"name": "x"}), ("/forget", {})):
            self.assertEqual(self.private("POST", path, body, "douglas")[0], 403, path)
        self.assertEqual(self.private("GET", "/journal", role="douglas")[0], 404)
        self.assertTrue(self.store.data["global_on"])

    def test_no_role_no_answer(self):
        self.ready()
        self.assertEqual(self.private("GET", "/cameras", role=None)[0], 403)
        self.assertEqual(self.private("POST", "/image", {"camera": "Front Yard"}, role="chatgpt")[0], 403)

    def test_friendly_name(self):
        self.ready()
        self.assertEqual(self.private("POST", "/cameras/dev-front-001/name", {"name": "  driveway  cam "})[0], 200)
        st, obs, _ = self.image("Driveway Cam")
        self.assertEqual((st, obs["camera_name"]), (200, "driveway cam"))

    def test_offline_camera(self):
        self.ready()
        self.private("POST", "/cameras/dev-door-002/enable")
        self.assertEqual(self.image("Front Door")[0], 503)

    def test_throttling_waits_it_out(self):
        self.ready()
        self.fake.throttle = 5
        self.assertEqual(self.image(fresh=False)[0], 429)
        self.assertGreater(self.app.status()["throttled_for_s"], 0)
        calls = len(self.fake.calls)
        self.assertEqual(self.image(fresh=False)[0], 429)
        self.assertEqual(len(self.fake.calls), calls)        # Ring was not asked again

    def test_journal_has_words_only(self):
        self.ready()
        self.image()
        self.image("dev-door-002")
        st, _, body = self.private("GET", "/journal")
        j = json.loads(body)["journal"]
        self.assertEqual(j[0]["result"], "refused: not a camera that is switched on")
        self.assertEqual((j[1]["what"], j[1]["role"], j[1]["source"]), ("image", "douglas", "live-triggered"))
        h = self.app.status()["health"]
        self.assertTrue(h["last_success"] and h["last_access"] >= h["last_success"])
        self.assertNotIn(b"\xff\xd8", open(self.store.path, "rb").read())   # no picture in the state
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["api.sock", "state.json"])

    def test_401_refreshes_once_and_rotates(self):
        self.linked()
        self.fake.reject_next_access = True
        self.assertEqual(self.private("GET", "/cameras")[0], 200)
        acct = next(iter(self.store.data["accounts"].values()))
        self.assertEqual(acct["refresh_token"], "refresh-2")
        self.assertNotIn("refresh-1", self.fake.valid_refresh)

    def test_revoked_at_ring_stops_everything(self):
        self.ready()
        self.fake.revoked = True
        self.app._dev_cache = None
        self.assertNotEqual(self.image()[0], 200)
        self.assertEqual(self.app.status()["accounts"][0]["state"], "relink")

    def test_forget_drops_tokens(self):
        self.ready()
        self.assertEqual(self.private("POST", "/forget")[0], 200)
        self.assertEqual(self.store.data["accounts"], {})
        self.assertFalse(self.store.data["global_on"])
        self.assertNotIn("access-", open(self.store.path).read())
        self.assertEqual(self.image()[0], 403)

    def test_restart_keeps_link_and_switches(self):
        self.ready()
        self.private("POST", "/cameras/dev-front-001/name", {"name": "Driveway"})
        store2 = dougring.Store(self.store.path)
        app2 = dougring.DougRing(CREDS, store2, self.app.ring)
        self.assertTrue(store2.data["global_on"])
        img, info = app2.image("douglas", "Driveway")
        self.assertEqual((img, info["camera_name"]), (JPEG, "Driveway"))

    # what the review found (F1, F2, H1-H10)
    def test_client_id_required(self):
        st, _ = self.http("POST", "/ring/token", b"code=good-code",
                          {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 400)
        self.assertFalse([c for c in self.fake.calls if c[1] == "/oauth/token"])

    def test_exchanges_are_rate_capped(self):
        codes = [self.send_code(code=f"bad{i}")[0] for i in range(dougring.EXCHANGES_PER_MIN + 3)]
        self.assertEqual(codes.count(429), 3)
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/oauth/token"]), dougring.EXCHANGES_PER_MIN)
        self.assertEqual(self.send_code(code="x" * 600)[0], 400)

    def test_forged_nonce_answers_at_once_without_exchange(self):
        t = int(time.time() * 1000)
        t0 = time.time()
        self.assertEqual(self.http("GET", f"/ring/link?nonce={ring_nonce(t, 'x')}&time={t}")[0], 400)
        self.assertLess(time.time() - t0, 0.5)

    def test_odd_time_values(self):
        for v in ("\u0661\u0662\u0663", "9" * 40, "-5", "1e9"):
            q = urllib.parse.urlencode({"nonce": "AAAA", "time": v})
            self.assertEqual(self.http("GET", f"/ring/link?{q}")[0], 400, v)

    def test_reload_shows_success_again(self):
        self.send_code()
        t = int(time.time() * 1000)
        self.fake.link_time = t
        url = f"/ring/link?nonce={ring_nonce(t, ACCOUNT_ID)}&time={t}"
        self.assertEqual(self.http("GET", url)[0], 200)
        st, page = self.http("GET", url)
        self.assertEqual(st, 200)
        self.assertIn("linked", page)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "PATCH"]), 1)

    def test_in_flight_cap_closes_extra_connections(self):
        srv = dougring.BoundedHTTPServer(("127.0.0.1", 0), dougring.public_handler(self.app), limit=2)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        held = []
        for _ in range(2):   # two slow callers that never finish their headers
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"GET /ring/home HTTP/1.1\r\n")
            held.append(s)
        time.sleep(0.3)
        third = socket.create_connection(("127.0.0.1", port))
        third.sendall(b"GET /ring/home HTTP/1.0\r\n\r\n")
        third.settimeout(3)
        self.assertEqual(third.recv(100), b"")   # closed without an answer
        for s in held + [third]:
            s.close()
        time.sleep(0.3)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ring/home", timeout=5) as r:
            self.assertEqual(r.status, 200)       # slots come back
        srv.shutdown()
        srv.server_close()

    def test_trickled_body_is_cut_off(self):
        old = dougring.RECV_DEADLINE_S
        dougring.RECV_DEADLINE_S = 1
        try:
            s = socket.create_connection(self.pub.server_address)
            s.sendall(b"POST /ring/token HTTP/1.1\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                      b"Content-Length: 1000\r\n\r\ncode=")
            t0 = time.time()
            s.settimeout(5)
            try:
                while time.time() - t0 < 4:
                    s.sendall(b"x")
                    time.sleep(0.2)
            except OSError:
                pass
            self.assertLess(time.time() - t0, 3)          # cut off near the deadline
            s.close()
            self.assertEqual(self.http("GET", "/ring/home")[0], 200)
        finally:
            dougring.RECV_DEADLINE_S = old

    def test_negative_length_refused(self):
        s = socket.create_connection(self.pub.server_address)
        s.sendall(b"POST /ring/token HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: -1\r\n\r\n")
        s.settimeout(3)
        self.assertIn(b" 413 ", s.recv(200))
        s.close()

    def test_presigned_must_be_https_image(self):
        strict = dougring.Ring(CREDS, oauth_url=self.fake.base + "/oauth/token", api_url=self.fake.base, timeout=5)
        with self.assertRaises(dougring.RingError):
            strict.fetch_presigned(self.fake.base + "/presigned")
        with self.assertRaises(dougring.RingError):
            strict.fetch_presigned("file:///etc/passwd")
        loose = dougring.Ring(CREDS, oauth_url=self.fake.base + "/oauth/token", api_url=self.fake.base, timeout=5, allow_http=True)
        with self.assertRaises(dougring.RingError):
            loose.fetch_presigned(self.fake.base + "/v1/users/me")   # JSON, not a picture

    def test_removal_switches_its_cameras_off(self):
        self.linked()
        self.private("POST", "/cameras/dev-front-001/enable")
        rm = json.dumps({"meta": {"request_id": "r9", "account_id": ACCOUNT_ID},
                         "data": {"type": "app_integration_removed", "attributes": {}}}).encode()
        sig = "sha256=" + hmac.new(HMAC_KEY.encode(), rm, hashlib.sha256).hexdigest()
        self.http("POST", "/ring/webhook", rm, {"X-Signature": sig})
        self.assertEqual(self.store.data["enabled"], [])

    def test_public_answers_say_nothing(self):
        req = urllib.request.Request(self.url + "/ring/home")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
            self.assertNotIn("DougRing", r.headers.get("Server", ""))
        for word in ("Douglas", "Ring camera", "household"):
            self.assertNotIn(word, body)

    def test_nonce_matches_python_reference(self):
        # Ring's documented Python: base64.urlsafe_b64encode(mac).rstrip(b'=')
        self.assertEqual(dougring.compute_nonce("1771130906289", "abc", HMAC_KEY), ring_nonce(1771130906289, "abc"))
        self.assertNotIn("=", dougring.compute_nonce("1", "a", HMAC_KEY))

    def test_mask_email(self):
        self.assertEqual(dougring.mask_email("frankie@example.com"), "f***e@example.com")
        self.assertEqual(dougring.mask_email("ab@x.io"), "a***@x.io")
        self.assertEqual(dougring.mask_email(""), "***")


if __name__ == "__main__":
    unittest.main()
