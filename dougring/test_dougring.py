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
                    if body.get("type") not in ("latest_in_range", "at_timestamp"):
                        return self._send(400, {"errors": [{"code": "INVALID_PAYLOAD"}]})
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
                                                              "X-Media-Timestamp": "1764000000000",
                                                              "X-Media-Origin": "recording"})
                if not self._auth():
                    return self._send(401, {})
                if self.path == "/v1/users/me":
                    return self._send(200, {"data": {"type": "users", "id": ACCOUNT_ID,
                                                     "attributes": {"first_name": "F", "last_name": "M", "email": EMAIL}}})
                if self.path.startswith("/v1/devices"):
                    return self._send(200, {
                        "data": [{"type": "devices", "id": "dev-front-001", "attributes": {"name": "Front Yard", "type": "camera"},
                                  "relationships": {"status": {"data": {"type": "status", "id": "s1"}}}},
                                 {"type": "devices", "id": "dev-door-002", "attributes": {"name": "Front Door", "type": "doorbell"}}],
                        "included": [{"type": "status", "id": "s1", "attributes": {"online": True}}]})
                self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"


class DougRingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeRing()
        ring = dougring.Ring(CREDS, oauth_url=self.fake.base + "/oauth/token", api_url=self.fake.base, timeout=5)
        self.store = dougring.Store(os.path.join(self.tmp.name, "state.json"))
        self.app = dougring.DougRing(CREDS, self.store, ring, webhooks=True)
        self.log = io.StringIO()
        self._stderr = redirect_stderr(self.log)
        self._stderr.__enter__()
        self.old_wait = dougring.LINK_WAIT_S
        dougring.LINK_WAIT_S = 1
        self.pub = ThreadingHTTPServer(("127.0.0.1", 0), dougring.public_handler(self.app))
        threading.Thread(target=self.pub.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.pub.server_address[1]}"
        self.sock = os.path.join(self.tmp.name, "api.sock")
        self.priv = dougring.UnixHTTPServer(self.sock, dougring.private_handler(self.app))
        threading.Thread(target=self.priv.serve_forever, daemon=True).start()

    def tearDown(self):
        self._stderr.__exit__(None, None, None)
        dougring.LINK_WAIT_S = self.old_wait
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

    def private(self, method, path, body=b""):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.sock)
        s.sendall(f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
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

    def test_devices_enable_snapshot(self):
        self.linked()
        st, _, body = self.private("GET", "/devices")
        self.assertEqual(st, 200)
        devs = json.loads(body)["devices"]
        self.assertEqual([d["name"] for d in devs], ["Front Yard", "Front Door"])
        self.assertEqual(self.private("POST", "/cameras/dev-front-001/snapshot")[0], 403)
        self.assertEqual(self.private("POST", "/cameras/nope/enable")[0], 404)
        self.assertEqual(self.private("POST", "/cameras/dev-front-001/enable")[0], 200)
        st, head, img = self.private("POST", "/cameras/dev-front-001/snapshot")
        self.assertEqual(st, 200)
        self.assertEqual(img, JPEG)
        self.assertIn("X-Media-Origin: recording", head)
        self.assertEqual(self.private("POST", "/cameras/dev-door-002/snapshot")[0], 403)

    def test_401_refreshes_once_and_rotates(self):
        self.linked()
        self.fake.reject_next_access = True
        st, _, _ = self.private("GET", "/devices")
        self.assertEqual(st, 200)
        acct = next(iter(self.store.data["accounts"].values()))
        self.assertEqual(acct["refresh_token"], "refresh-2")     # the new one is kept
        self.assertNotIn("refresh-1", self.fake.valid_refresh)   # the old one is spent

    def test_revoked_access_stops_everything(self):
        self.linked()
        self.private("POST", "/cameras/dev-front-001/enable")
        self.fake.revoked = True
        st, _, body = self.private("POST", "/cameras/dev-front-001/snapshot")
        self.assertNotEqual(st, 200)
        self.assertEqual(self.app.status()["accounts"][0]["state"], "relink")
        self.assertNotEqual(self.private("GET", "/devices")[0], 200)

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
