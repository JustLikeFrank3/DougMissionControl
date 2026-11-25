#!/usr/bin/env python3
"""dougring — Douglas's line to Frank's Ring cameras, through Ring's official
developer platform (a Private app, one-way account linking).

It holds the Ring app's credentials and the account's tokens, and nothing else
in the house does. Two doors, never the same one:

  public   127.0.0.1:9130, the only thing Tailscale Funnel reaches. It answers
           four paths and nothing else:
             POST /ring/token    Ring's servers hand over an authorization code
                                 (form-encoded); it is exchanged within 60 s
             GET  /ring/link     the browser arrives with ?nonce=&time=; the
                                 nonce is matched against a token that came in
                                 above, the account is confirmed with Ring
                                 (POST, then PATCH status=completed)
             POST /ring/webhook  signed events from Ring (X-Signature); checked,
                                 acknowledged, only kind/device/time kept.
                                 404 unless DOUGRING_WEBHOOKS=1 (owner's yes)
             GET  /ring/home     a plain page, for the optional homepage field
           A GET or HEAD on any open one answers 200, for the console.
  private  a Unix socket (/run/dougring/api.sock), for deck-api (Mission
           Control) on this machine only, which says who asks: the owner
           (status, cameras, journal, the global and per-camera switches,
           names, forget, a picture) or Douglas (his switched-on cameras and
           a picture from one). The rules are kept here, not in a screen.

Ring's facts it relies on (docs read 2026-11-25 from Ring's docs MCP server):
amazon_vision_api/authentication/{account_linking,access_tokens,refresh_tokens}.md,
architecture/app-deployment.md, amazon_vision_api/{users,image_download,
error_handling}.md, dev_portal/configure.md.

Never logged, never answered: tokens, codes, nonces, the client secret, the
HMAC key, a full e-mail address. Credentials come from systemd's
LoadCredential (the file is root's; this user never reads it), tokens live in
/var/lib/dougring/state.json (0600, this service's own user).
"""
import base64
import ssl
import hashlib
import hmac
import html
import json
import os
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1"
USER_AGENT = f"DougRing/{VERSION}"
OAUTH_URL = "https://oauth.ring.com/oauth/token"
API_URL = "https://api.amazonvision.com"
RTSP_HOST = "video.rtsp.amazonvision.com"
RTSP_PORT = 322

NONCE_WINDOW_S = 600          # Ring: reject a link older than 10 minutes
CLOCK_SKEW_S = 60             # a little room for a clock that runs ahead
UNCLAIMED_TTL_S = 900         # an exchanged token waits this long for its browser
LINK_WAIT_S = 15              # the browser may beat the token exchange by a moment
REFRESH_MARGIN_S = 1800       # refresh when the access token has this little left
REFRESH_EVERY_S = 6 * 3600    # and at least this often, so the 30-day token rotates
MAX_BODY = 64 * 1024
RECV_DEADLINE_S = 8           # a public request must have arrived whole by then
MAX_IN_FLIGHT = 16            # public requests served at once; over it, closed at once
EXCHANGES_PER_MIN = 6         # a real link needs one
MAX_CODE_LEN = 512
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DONE_TTL_S = 600              # a finished link's page can be shown again this long
JOURNAL_KEEP = 500            # camera journal entries kept (words only, never pictures)
DEVICES_TTL_S = 60            # Ring asks for device information to be cached
CAPTURE_WAIT_S = 90           # how long a second request for the same camera waits its turn
LIVE_HOLD_S = 6               # a live session held this long makes the camera record
FRESH_WAIT_S = 60             # how long Ring may take to have that recording's picture ready
FRESH_POLL_S = 3
MAX_ACCOUNTS = 5              # Ring's limit for a private app
SNAPSHOT_WINDOW_MS = 24 * 3600 * 1000


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def mask_email(email):
    """u***r@example.com — the account_identifier Ring wants, and all we show."""
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return "***"
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def short_id(account_id):
    return f"…{str(account_id)[-4:]}" if account_id else "?"


def compute_nonce(time_param, account_id, hmac_key):
    """Ring's nonce: base64url, unpadded, of HMAC-SHA256(key, "<time>:<account_id>")."""
    mac = hmac.new(hmac_key.encode("utf-8"),
                   f"{time_param}:{account_id}".encode("utf-8"),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def webhook_signature_ok(raw_body, header, hmac_key):
    """X-Signature: sha256=<hex HMAC-SHA256 of the raw body> (same key, hex)."""
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(hmac_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[len("sha256="):].strip().lower())


def hardware_id(account_id):
    """A stable UUID per linked account, for Ring's required Hardware_Id header."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dougring:{account_id}"))


def load_credentials(path):
    """KEY=VALUE lines: RING_CLIENT_ID, RING_CLIENT_SECRET, RING_HMAC_KEY."""
    creds = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    missing = [k for k in ("RING_CLIENT_ID", "RING_CLIENT_SECRET", "RING_HMAC_KEY") if not creds.get(k)]
    if missing:
        raise SystemExit(f"dougring: credentials file lacks {', '.join(missing)}")
    return creds


class RingError(Exception):
    def __init__(self, status, detail=""):
        super().__init__(f"Ring answered {status}{': ' + detail if detail else ''}")
        self.status = status
        self.detail = detail


def _error_code(body):
    """The `code` or `title` of a JSON:API error, never the whole body."""
    try:
        doc = json.loads(body)
        if isinstance(doc.get("error"), str):          # OAuth: {"error": "invalid_grant"}
            return doc["error"][:60]
        err = doc["errors"][0]
        return str(err.get("code") or err.get("title") or "")[:60]
    except Exception:
        return ""


class Ring:
    """Talks to Ring. Base URLs are parameters so the tests can stand in for Ring."""

    def __init__(self, creds, oauth_url=OAUTH_URL, api_url=API_URL, timeout=20, allow_http=False):
        self.allow_http = allow_http   # tests only: the stand-in for Ring is plain http
        self.rtsp_host, self.rtsp_port, self.rtsp_plain = RTSP_HOST, RTSP_PORT, False
        self.creds = creds
        self.oauth_url = oauth_url
        self.api_url = api_url
        self.timeout = timeout
        # No redirects anywhere: a redirect would carry the Bearer token to another
        # host. Only http(s) handlers, so no file:// or ftp:// can be opened.
        self._open = urllib.request.OpenerDirector()
        for h in (urllib.request.HTTPHandler, urllib.request.HTTPSHandler,
                  urllib.request.HTTPErrorProcessor, urllib.request.HTTPDefaultErrorHandler):
            self._open.add_handler(h())

    def _token_call(self, fields):
        data = urllib.parse.urlencode({**fields,
                                       "client_id": self.creds["RING_CLIENT_ID"],
                                       "client_secret": self.creds["RING_CLIENT_SECRET"]}).encode()
        req = urllib.request.Request(self.oauth_url, data=data, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with self._open.open(req, timeout=self.timeout) as r:
                tok = json.loads(r.read(MAX_BODY))
        except urllib.error.HTTPError as e:
            raise RingError(e.code, _error_code(e.read())) from None
        if not tok.get("access_token") or not tok.get("refresh_token"):
            raise RingError(502, "token response without tokens")
        return tok

    def exchange_code(self, code):
        return self._token_call({"grant_type": "authorization_code", "code": code})

    def refresh(self, refresh_token):
        return self._token_call({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _headers(self, access_token, account_id):
        return {"Authorization": f"Bearer {access_token}",
                "User-Agent": USER_AGENT,
                "Hardware_Id": hardware_id(account_id),
                "Accept": "application/json"}

    def api(self, method, path, access_token, account_id, body=None, redirect=True):
        data = None
        headers = self._headers(access_token, account_id)
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api_url + path, data=data, method=method, headers=headers)
        try:
            with self._open.open(req, timeout=self.timeout) as r:
                raw = r.read(4 * 1024 * 1024)
                return r.status, dict(r.headers), raw
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                if not redirect:          # the image download's 303 is expected
                    return e.code, dict(e.headers), b""
                raise RingError(e.code, "unexpected redirect") from None
            err = RingError(e.code, _error_code(e.read(MAX_BODY)))
            try:
                err.retry_after = int(e.headers.get("Retry-After") or 0)
            except ValueError:
                pass
            raise err from None

    def api_json(self, method, path, access_token, account_id, body=None):
        _, _, raw = self.api(method, path, access_token, account_id, body)
        return json.loads(raw) if raw else {}

    def user_me(self, access_token):
        # Hardware_Id is per account; before we know the account, use a fixed one.
        req = urllib.request.Request(self.api_url + "/v1/users/me", headers={
            "Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT,
            "Hardware_Id": hardware_id("pending"), "Accept": "application/json"})
        try:
            with self._open.open(req, timeout=self.timeout) as r:
                data = json.loads(r.read(MAX_BODY))["data"]
        except urllib.error.HTTPError as e:
            raise RingError(e.code, _error_code(e.read(MAX_BODY))) from None
        return {"id": str(data["id"]), "email": data.get("attributes", {}).get("email", "")}

    def live_session(self, device_id, access_token, hold_s, host=None, port=None):
        """Start a live session on Ring's RTSP bridge so the camera records,
        hold it hold_s seconds, end it. Returns when it started (epoch ms).
        The media (H.265 in AES-GCM SRTP) is received and thrown away: it is
        only the session that is wanted. The token goes in X-Auth-Token, as
        Ring prefers, never in a URL."""
        host, port = host or self.rtsp_host, port or self.rtsp_port
        url = f"rtsps://{RTSP_HOST}:{RTSP_PORT}/v1/devices/{urllib.parse.quote(device_id, safe='')}/stream"
        raw = socket.create_connection((host, port), timeout=self.timeout)
        sock = (raw if self.rtsp_plain else
                ssl.create_default_context().wrap_socket(raw, server_hostname=host))
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("0.0.0.0", 0))
        udp.settimeout(0.5)
        rtp = udp.getsockname()[1]
        state = {"cseq": 0, "session": None}
        buf = sock.makefile("rb")

        def call(method, headers):
            state["cseq"] += 1
            lines = [f"{method} {url} RTSP/1.0", f"CSeq: {state['cseq']}", f"User-Agent: {USER_AGENT}"]
            if state["session"]:
                lines.append(f"Session: {state['session']}")
            lines += [f"{k}: {v}" for k, v in headers.items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            status_line = buf.readline(1024).decode("latin-1").split()
            hdrs = {}
            while True:
                line = buf.readline(4096).decode("latin-1").strip()
                if not line:
                    break
                k, _, v = line.partition(":")
                hdrs[k.strip().lower()] = v.strip()
            n = int(hdrs.get("content-length") or 0)
            if 0 < n <= 65536:
                buf.read(n)
            code = int(status_line[1]) if len(status_line) > 1 and status_line[1].isdigit() else 0
            if code != 200:
                raise RingError(code or 502, f"RTSP {method}")
            return hdrs
        try:
            got = call("DESCRIBE", {"Accept": "application/sdp", "X-Auth-Token": access_token})
            state["session"] = (got.get("session") or "").split(";")[0] or None
            got = call("SETUP", {"Transport": f"RTP/SAVP;unicast;client_port={rtp}-{rtp + 1}"})
            parts = dict(p.split("=", 1) for p in got.get("transport", "").split(";") if "=" in p)
            src = parts.get("source", "").strip()
            sport = int((parts.get("server_port") or "0").split("-")[0] or 0)
            if src and sport:
                udp.sendto(b"\x00", (src, sport))        # NAT hole punch, as Ring's docs say
            call("PLAY", {})
            started = int(time.time() * 1000)
            end = time.time() + hold_s
            packets = 0
            while time.time() < end:
                try:
                    udp.recvfrom(2048)
                    packets += 1
                except socket.timeout:
                    pass
            log(f"live session: {packets} packets in {hold_s} s")
            return started
        finally:
            try:
                if state["session"]:
                    call("TEARDOWN", {})
            except Exception:
                pass
            udp.close()
            sock.close()

    def fetch_presigned(self, url):
        """The picture itself: a pre-signed URL, no Bearer token sent to it."""
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme != "https" and not (self.allow_http and scheme == "http"):
            raise RingError(502, "picture offered on a non-https address")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._open.open(req, timeout=self.timeout) as r:
                img = r.read(MAX_IMAGE_BYTES + 1)
                headers = dict(r.headers)
        except urllib.error.HTTPError as e:
            raise RingError(e.code, _error_code(e.read(MAX_BODY))) from None
        if len(img) > MAX_IMAGE_BYTES:
            raise RingError(502, "picture too large")
        ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        if not ctype.startswith("image/"):
            raise RingError(502, "not a picture")
        return img, headers


class Store:
    """Everything this service remembers, in one 0600 JSON file, written atomically."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"unclaimed": [], "accounts": {}, "enabled": [], "events": [], "seen": [],
                     "global_on": False, "names": {}, "owners": {}, "journal": [],
                     "health": {}, "telemetry": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))

    def save(self):
        with self.lock:
            tmp = f"{self.path}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)


class DougRing:
    def __init__(self, creds, store, ring, now=time.time, webhooks=False):
        self.webhooks = webhooks       # off until the owner approves events
        self.creds = creds
        self.store = store
        self.ring = ring
        self.now = now
        self.refresh_lock = threading.Lock()   # refresh tokens rotate: one at a time
        self.ex_lock = threading.Lock()
        self.ex_times = []                     # when recent exchanges started (rate cap)
        self.ex_inflight = 0
        self.ex_last_end = 0.0
        self.done = {}                         # nonce -> (expires, page) for a reload
        self.cooldown_until = 0.0              # Ring said 429: nothing is asked until then
        self._dev_cache = None
        self._cam_locks = {}

    # ── token exchange (Ring's servers → us) ─────────────────────────────
    def token_exchange(self, form):
        code = form.get("code", "")
        if not code or len(code) > MAX_CODE_LEN:
            return 400, "missing code"
        # Ring sends `code` and `client_id` (dev_portal/configure.md); both required.
        if not hmac.compare_digest(form.get("client_id", "").encode(), self.creds["RING_CLIENT_ID"].encode()):
            log("token exchange: refused, client_id missing or not this app's")
            return 400, "wrong client"
        with self.ex_lock:
            t = self.now()
            self.ex_times = [x for x in self.ex_times if t - x < 60]
            if len(self.ex_times) >= EXCHANGES_PER_MIN:
                log("token exchange: refused, too many this minute")
                return 429, "slow down"
            self.ex_times.append(t)
            self.ex_inflight += 1
        try:
            tok = self.ring.exchange_code(code)
            me = self.ring.user_me(tok["access_token"])
        except RingError as e:
            log(f"token exchange: failed, {e}")
            return 502, "exchange failed"
        except Exception as e:  # network, or an answer that is not what Ring documents
            log(f"token exchange: failed, {type(e).__name__}")
            return 502, "exchange failed"
        finally:
            with self.ex_lock:
                self.ex_inflight -= 1
                self.ex_last_end = self.now()
        with self.store.lock:
            t = self.now()
            pool = [u for u in self.store.data["unclaimed"]
                    if t - u["received_at"] < UNCLAIMED_TTL_S and u["account_id"] != me["id"]]
            pool.append({"account_id": me["id"], "email": me["email"],
                         "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                         "expires_at": t + int(tok.get("expires_in", 14400)), "received_at": t})
            self.store.data["unclaimed"] = pool[-10:]
            self.store.save()
        log(f"token exchange: ok, account {short_id(me['id'])} waits for its link")
        return 200, "ok"

    # ── account link (the browser → us) ──────────────────────────────────
    def _match(self, nonce, time_param):
        with self.store.lock:
            t = self.now()
            for u in self.store.data["unclaimed"]:
                if t - u["received_at"] >= UNCLAIMED_TTL_S:
                    continue
                if hmac.compare_digest(compute_nonce(time_param, u["account_id"], self.creds["RING_HMAC_KEY"]), nonce):
                    return u
        return None

    def account_link(self, nonce, time_param, wait=None):
        """Returns (http status, title, sentence, masked e-mail or None)."""
        if not nonce or not time_param:
            return 200, "OK", "Start linking from the Ring Developer Console.", None
        if not (time_param.isascii() and time_param.isdigit() and len(time_param) <= 15) \
                or len(nonce) > 128 or not nonce.isascii():
            return 400, "Link refused", "The link Ring sent was malformed.", None
        age = (self.now() * 1000 - int(time_param)) / 1000
        if age > NONCE_WINDOW_S:
            return 400, "Link expired", "That link is more than ten minutes old. Start again from the Ring Developer Console.", None
        if age < -CLOCK_SKEW_S:
            return 400, "Link refused", "That link's time is in the future.", None
        with self.ex_lock:
            prev = self.done.get(nonce)
            self.done = {k: v for k, v in self.done.items() if v[0] > self.now()}
        if prev and prev[0] > self.now():
            return prev[1]                    # a reload of a link that already worked
        deadline = self.now() + (LINK_WAIT_S if wait is None else wait)
        rec = self._match(nonce, time_param)

        def exchange_near():                  # wait only if Ring's code may be arriving
            with self.ex_lock:
                return self.ex_inflight > 0 or self.now() - self.ex_last_end < 5
        while rec is None and self.now() < deadline and exchange_near():
            time.sleep(0.5)
            rec = self._match(nonce, time_param)
        if rec is None:
            log("account link: no token matches this nonce")
            return 400, "Link not matched", "Ring's code never reached DougRing, or it was for another account. Start again from the Ring Developer Console.", None
        # Passwordless: the identity is the Ring e-mail fetched server-side with
        # the matched token, never anything the browser sent.
        try:
            me = self.ring.user_me(rec["access_token"])
        except (RingError, OSError) as e:
            log(f"account link: profile lookup failed, {e}")
            return 502, "Ring did not answer", "Ring did not confirm the account. Try again from the console.", None
        if me["id"] != rec["account_id"]:
            log("account link: token's account changed under us; refused")
            return 400, "Link refused", "The account did not match.", None
        ident = mask_email(me["email"])
        with self.store.lock:
            accounts = self.store.data["accounts"]
            if me["id"] not in accounts and len(accounts) >= MAX_ACCOUNTS:
                return 400, "Too many accounts", "DougRing already holds five Ring accounts.", None
        try:
            self.ring.api_json("POST", "/v1/accounts/me/app-integrations", rec["access_token"], me["id"],
                               {"account_identifier": ident, "nonce": nonce})
            self.ring.api_json("PATCH", "/v1/accounts/me/app-integrations", rec["access_token"], me["id"],
                               {"account_identifier": ident, "status": "completed"})
        except (RingError, OSError) as e:
            log(f"account link: app-integrations call failed, {e}")
            return 502, "Ring refused the link", "Ring did not accept the link confirmation. Start again from the console.", None
        with self.store.lock:
            self.store.data["accounts"][me["id"]] = {"email_masked": ident,
                                  "access_token": rec["access_token"], "refresh_token": rec["refresh_token"],
                                  "expires_at": rec["expires_at"], "linked_at": self.now(),
                                  "refreshed_at": rec["received_at"], "state": "linked"}
            self.store.data["unclaimed"] = [u for u in self.store.data["unclaimed"] if u is not rec]
            self.store.save()
        log(f"account link: completed for {ident} ({short_id(me['id'])})")
        result = (200, "DougRing is linked", f"Ring account {ident} is linked to DougRing. You can close this page.", ident)
        with self.ex_lock:
            self.done[nonce] = (self.now() + DONE_TTL_S, result)
        return result

    # ── webhooks (Ring's servers → us) ───────────────────────────────────
    def webhook(self, raw, signature):
        if not webhook_signature_ok(raw, signature, self.creds["RING_HMAC_KEY"]):
            log("webhook: bad signature, dropped")
            return 401
        try:
            ev = json.loads(raw)
        except ValueError:
            return 400
        meta = ev.get("meta") or {}
        data = ev.get("data") or {}
        attrs = data.get("attributes") or {}
        rid = str(meta.get("request_id") or "")
        with self.store.lock:
            if rid and rid in self.store.data["seen"]:
                return 200
            if rid:
                self.store.data["seen"] = (self.store.data["seen"] + [rid])[-500:]
            # Events are phase 4 (docs/DOUGRING.md), after the owner's yes: until
            # then nothing of an event is kept but its id, to drop repeats.
            kind = str(data.get("type") or meta.get("event_type") or "?")[:40]
            if kind == "app_integration_removed" and meta.get("account_id") in self.store.data["accounts"]:
                acct = self.store.data["accounts"].pop(meta["account_id"])
                self.store.data["enabled"] = [d for d in self.store.data["enabled"]
                                              if self.store.data.get("owners", {}).get(d) != meta["account_id"]]
                log(f"webhook: {acct['email_masked']} removed DougRing; tokens dropped, its cameras off")
            self.store.save()
        log(f"webhook: {kind} acknowledged")
        return 200

    # ── tokens kept fresh ────────────────────────────────────────────────
    def _refresh(self, account_id, force=False):
        with self.refresh_lock:
            with self.store.lock:
                acct = self.store.data["accounts"].get(account_id)
                if not acct or acct.get("state") != "linked":
                    return None
                t = self.now()
                if not force and acct["expires_at"] - t > REFRESH_MARGIN_S and t - acct.get("refreshed_at", 0) < REFRESH_EVERY_S:
                    return acct
                old_refresh = acct["refresh_token"]
            try:
                tok = self.ring.refresh(old_refresh)
            except RingError as e:
                with self.store.lock:
                    if e.status in (400, 401):
                        acct["state"] = "relink"
                        self.store.save()
                log(f"refresh: {acct['email_masked']} failed, {e}")
                return None
            with self.store.lock:
                acct.update(access_token=tok["access_token"], refresh_token=tok["refresh_token"],
                            expires_at=self.now() + int(tok.get("expires_in", 14400)), refreshed_at=self.now())
                self.store.save()
            log(f"refresh: {acct['email_masked']} ok")
            return acct

    def refresh_all(self):
        for aid in list(self.store.data["accounts"]):
            try:
                self._refresh(aid)
            except Exception as e:  # never let the refresher die: the token lapses in 30 days
                log(f"refresh: failed, {type(e).__name__}")

    # ── what Mission Control and Douglas ask ─────────────────────────────
    # Two roles reach the private door, both through deck-api: "owner" (the
    # panel and the phone) and "douglas" (his camera tool). Only the owner
    # changes anything. Every rule below is enforced here, not in a screen.

    def _journal(self, role, what, camera="", result="ok", **extra):
        entry = {"at": int(self.now() * 1000), "role": role, "what": what,
                 "camera": camera, "result": str(result)[:80], **extra}
        with self.store.lock:
            self.store.data["journal"] = (self.store.data.get("journal", []) + [entry])[-JOURNAL_KEEP:]
            if what == "image":
                h = self.store.data.setdefault("health", {})
                h["last_access"] = entry["at"]
                if result == "ok":
                    h["last_success"] = entry["at"]
            self.store.save()
        log(f"journal: {role} {what} {camera[-6:] if camera else ''} {result}".rstrip())

    def _note_error(self, e):
        h = self.store.data.setdefault("health", {})
        h["last_error"] = {"at": int(self.now() * 1000), "status": getattr(e, "status", 0),
                           "code": getattr(e, "detail", type(e).__name__)}
        t = self.store.data.setdefault("telemetry", {})
        t["ring_errors"] = t.get("ring_errors", 0) + 1
        if getattr(e, "status", 0) == 429:
            t["throttled"] = t.get("throttled", 0) + 1

    def _call(self, account_id, method, path, body=None, redirect=True):
        if self.now() < self.cooldown_until:
            raise RingError(429, "waiting out Ring's rate limit")
        acct = self._refresh(account_id)
        if acct is None:
            raise RingError(401, "account not linked")
        with self.store.lock:
            t = self.store.data.setdefault("telemetry", {})
            t["ring_calls"] = t.get("ring_calls", 0) + 1
        try:
            try:
                return self.ring.api(method, path, acct["access_token"], account_id, body, redirect)
            except RingError as e:
                if e.status != 401:
                    raise
                acct = self._refresh(account_id, force=True)
                if acct is None:
                    raise
                return self.ring.api(method, path, acct["access_token"], account_id, body, redirect)
        except RingError as e:
            if e.status == 429:
                self.cooldown_until = self.now() + max(5, min(300, int(getattr(e, "retry_after", 0) or 30)))
            with self.store.lock:
                self._note_error(e)
            raise

    def status(self):
        with self.store.lock:
            d = self.store.data
            return {"version": VERSION, "global_on": bool(d.get("global_on")),
                    "accounts": [{"account": short_id(aid), "email": a["email_masked"], "state": a["state"],
                                  "access_expires_in_s": int(a["expires_at"] - self.now()),
                                  "linked_at": int(a["linked_at"]), "refreshed_at": int(a.get("refreshed_at", 0))}
                                 for aid, a in d["accounts"].items()],
                    "unclaimed": len(d["unclaimed"]),
                    "enabled": list(d["enabled"]), "names": dict(d.get("names", {})),
                    "health": dict(d.get("health", {})), "telemetry": dict(d.get("telemetry", {})),
                    "throttled_for_s": max(0, int(self.cooldown_until - self.now())),
                    "webhooks": self.webhooks}

    def devices(self, fresh=False):
        """The cameras the linked accounts share, as Ring says now (cached a
        minute: Ring asks for device information to be cached). Sensors and
        chimes are left out: no picture can come from them."""
        if not fresh and self._dev_cache and self.now() - self._dev_cache[0] < DEVICES_TTL_S:
            return [dict(c) for c in self._dev_cache[1]]
        out, owners = [], {}
        for aid in list(self.store.data["accounts"]):
            if self.store.data["accounts"][aid].get("state") != "linked":
                continue
            _, _, raw = self._call(aid, "GET", "/v1/devices?include=status,capabilities")
            doc = json.loads(raw)
            inc = {(i.get("type"), i.get("id")): i.get("attributes") or {} for i in doc.get("included", [])}
            for d in doc.get("data", []):
                rel = d.get("relationships") or {}

                def related(key):
                    ref = (rel.get(key) or {}).get("data") or {}
                    return inc.get((ref.get("type"), ref.get("id")), {})
                st, caps = related("status"), related("capabilities")
                if "video" not in caps:
                    continue
                did = str(d.get("id"))
                owners[did] = aid
                out.append({"id": did, "ring_name": str((d.get("attributes") or {}).get("name") or "")[:60],
                            "online": bool(st.get("online")),
                            "battery": "battery_status" in st,
                            "doorbell": "button_press" in caps})
        with self.store.lock:
            self.store.data["owners"] = owners
            self.store.data["enabled"] = [c for c in self.store.data["enabled"] if c in owners]
            self.store.data.setdefault("health", {})["last_discovery"] = int(self.now() * 1000)
            self.store.save()
        self._dev_cache = (self.now(), out)
        return [dict(c) for c in out]

    def cameras(self, role):
        """Every shared camera for the owner; for Douglas only those he may
        look through, and none while the global switch is off."""
        names = self.store.data.get("names", {})
        enabled = set(self.store.data["enabled"])
        on = bool(self.store.data.get("global_on"))
        out = []
        for c in self.devices():
            c["name"] = names.get(c["id"]) or c["ring_name"] or "camera"
            c["enabled"] = c["id"] in enabled
            if role == "douglas" and not (on and c["enabled"]):
                continue
            out.append(c)
        return out

    def _find(self, role, ref):
        """A camera by its id or its friendly name, as this role may see it."""
        ref = str(ref or "").strip().lower()
        for c in self.cameras(role):
            if ref in (c["id"].lower(), c["name"].lower(), c["ring_name"].lower()):
                return c
        return None

    # the owner's switches
    def set_global(self, on):
        with self.store.lock:
            self.store.data["global_on"] = bool(on)
            self.store.save()
        self._journal("owner", "global " + ("on" if on else "off"))
        return True

    def set_enabled(self, device_id, on):
        if on and device_id not in {c["id"] for c in self.devices()}:
            return False
        with self.store.lock:
            en = [d for d in self.store.data["enabled"] if d != device_id]
            if on:
                en.append(device_id)
            self.store.data["enabled"] = en
            self.store.save()
        self._journal("owner", "camera " + ("on" if on else "off"), device_id)
        return True

    def set_name(self, device_id, name):
        name = " ".join(str(name or "").split())[:40]
        if device_id not in {c["id"] for c in self.devices()}:
            return False
        with self.store.lock:
            names = self.store.data.setdefault("names", {})
            if name:
                names[device_id] = name
            else:
                names.pop(device_id, None)
            self.store.save()
        self._journal("owner", "rename", device_id, name or "(Ring's name)")
        return True

    def forget(self):
        """Drop every token and switch every camera off. Linking again is the
        only way back. Ring's console ("Disconnect all") ends it at Ring too."""
        with self.store.lock:
            n = len(self.store.data["accounts"])
            self.store.data.update(accounts={}, unclaimed=[], enabled=[], global_on=False, owners={})
            self.store.save()
        self._dev_cache = None
        self._journal("owner", "forget", result=f"{n} account(s) dropped")
        return True

    # a picture
    def image(self, role, ref, fresh=True):
        """A picture from a camera this role may use, and what is known of it.
        fresh: open a short live session so the camera records now, then take
        Ring's picture of that moment; failing that, the latest existing one,
        marked as such. Nothing is written to disk."""
        retrieved_at = int(self.now() * 1000)
        if not self.store.data.get("global_on"):
            self._journal(role, "image", str(ref)[:80], "refused: cameras are switched off")
            raise PermissionError("cameras are switched off in Mission Control")
        cam = self._find(role, ref) if role == "douglas" else (self._find("owner", ref))
        if cam is None or not cam["enabled"]:
            self._journal(role, "image", str(ref)[:80], "refused: not a camera that is switched on")
            raise PermissionError("not a camera that is switched on")
        aid = self.store.data["owners"].get(cam["id"])
        if not cam["online"]:
            self._journal(role, "image", cam["id"], "camera offline")
            raise RingError(503, "camera offline")
        lock = self._cam_locks.setdefault(cam["id"], threading.Lock())
        if not lock.acquire(timeout=CAPTURE_WAIT_S):
            raise RingError(409, "another picture from this camera is being taken")
        try:
            img = meta = None
            source = "latest existing"
            if fresh:
                try:
                    started = self._live_trigger(aid, cam)
                    img, meta = self._download_after(aid, cam["id"], started)
                    source = "live-triggered"
                except (RingError, OSError) as e:
                    log(f"image: live picture failed ({e}); taking the latest existing one")
            if img is None:
                img, meta = self._download(aid, cam["id"],
                                           {"type": "latest_in_range",
                                            "start_timestamp": int(self.now() * 1000) - SNAPSHOT_WINDOW_MS + 60_000})
        except Exception as e:
            self._journal(role, "image", cam["id"], f"failed: {getattr(e, 'detail', '') or type(e).__name__}")
            raise
        finally:
            lock.release()
        captured = int(meta.get("media_timestamp") or 0) or None
        age = round((retrieved_at - captured) / 1000) if captured else None
        info = {"camera_id": cam["id"], "camera_name": cam["name"], "captured_ms": captured,
                "retrieved_ms": retrieved_at, "source": source, "age_s": age,
                "media_origin": meta.get("media_origin", ""), "status": "ok",
                "content_type": meta.get("content_type", "image/jpeg")}
        self._journal(role, "image", cam["id"], "ok", source=source, age_s=age)
        with self.store.lock:
            t = self.store.data.setdefault("telemetry", {})
            t["pictures"] = t.get("pictures", 0) + 1
            self.store.save()
        return img, info

    def _live_trigger(self, aid, cam):
        """Ring's way to a current picture: a live session makes the camera
        record. RTSP only to start it; the packets are not decoded."""
        acct = self._refresh(aid)
        if acct is None:
            raise RingError(401, "account not linked")
        return self.ring.live_session(cam["id"], acct["access_token"], LIVE_HOLD_S)

    def _download_after(self, aid, device_id, started_ms):
        """Ring's picture of the moment the session ran, once it is written
        (416/425 until then)."""
        deadline = self.now() + FRESH_WAIT_S
        want = {"type": "at_timestamp", "timestamp": started_ms + 2000}
        while True:
            try:
                return self._download(aid, device_id, want)
            except RingError as e:
                if e.status not in (416, 425) or self.now() >= deadline:
                    raise
            time.sleep(FRESH_POLL_S)

    def _download(self, aid, device_id, body):
        body = dict(body, image_options={"format": "jpeg", "resolution": {"width": 1920, "height": 1080}})
        path = f"/v1/devices/{urllib.parse.quote(device_id, safe='')}/media/image/download"
        status, headers, _ = self._call(aid, "POST", path, body, redirect=False)
        loc = {k.lower(): v for k, v in headers.items()}.get("location")
        if status != 303 or not loc:
            raise RingError(status, "no picture offered")
        img, h = self.ring.fetch_presigned(loc)
        h = {k.lower(): v for k, v in h.items()}
        return img, {"content_type": h.get("content-type", "image/jpeg"),
                     "media_timestamp": h.get("x-media-timestamp", ""),
                     "media_origin": h.get("x-media-origin", "")}

    def journal(self, n=100):
        return list(self.store.data.get("journal", [])[-n:])[::-1]


# ── the public door ──────────────────────────────────────────────────────
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font:17px/1.5 system-ui,sans-serif;max-width:32em;margin:12vh auto;padding:0 1em;color:#222}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}}}</style></head>
<body><h1>{title}</h1><p>{text}</p></body></html>"""

PUBLIC_PATHS = ("/ring/token", "/ring/link", "/ring/webhook", "/ring/home")


def public_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "-"
        sys_version = ""
        timeout = 15

        def log_message(self, fmt, *args):
            pass  # the query string carries nonces: no access log at all

        # A caller has RECV_DEADLINE_S, all told, to send its headers and
        # body: one that trickles them is cut off and its slot freed.
        def setup(self):
            super().setup()
            self._arrived = threading.Event()
            self._timer = threading.Timer(RECV_DEADLINE_S, self._too_slow)
            self._timer.daemon = True
            self._timer.start()

        def _too_slow(self):
            if not self._arrived.is_set():
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def _received(self):
            self._arrived.set()
            self._timer.cancel()

        def finish(self):
            self._timer.cancel()
            try:
                super().finish()
            except OSError:
                pass

        def _send(self, status, body=b"", ctype="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode()
            # One line a request: method, path, status, and for a POST its
            # content type and size. Never the query (it carries nonces) or
            # the body (it carries codes).
            path = urllib.parse.urlsplit(self.path).path[:60]
            extra = ""
            if self.command == "POST":
                extra = (f" {(self.headers.get('Content-Type') or '-').split(';')[0][:40]}"
                         f" {self.headers.get('Content-Length') or 0} B"
                         f"{' signed' if self.headers.get('X-Signature') else ''}")
            log(f"public: {self.command} {path} -> {status}{extra}"
                f"{' ua=' + (self.headers.get('User-Agent') or '-')[:40] if path.startswith('/ring/') else ''}")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _page(self, status, title, text):
            self._send(status, PAGE.format(title=html.escape(title), text=html.escape(text)),
                       "text/html; charset=utf-8")

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                raise ValueError("bad length")
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            self._received()
            u = urllib.parse.urlsplit(self.path)
            if u.path not in PUBLIC_PATHS or (u.path == "/ring/webhook" and not app.webhooks):
                return self._send(404, "not found")
            if u.path == "/ring/link":
                q = urllib.parse.parse_qs(u.query)
                status, title, text, _ = app.account_link(q.get("nonce", [""])[0], q.get("time", [""])[0])
                return self._page(status, title, text)
            if u.path == "/ring/home":
                return self._page(200, "OK", "OK")
            return self._send(200, "OK")

        do_HEAD = do_GET

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/ring/token" and not (u.path == "/ring/webhook" and app.webhooks):
                return self._send(404, "not found")
            try:
                raw = self._body()
            except ValueError:
                return self._send(413, "bad length")
            if len(raw) < int(self.headers.get("Content-Length") or 0):
                return None                    # cut off for sending too slowly
            self._received()
            if u.path == "/ring/token":
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/x-www-form-urlencoded":
                    return self._send(415, "form-encoded only")
                form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
                status, text = app.token_exchange(form)
                return self._send(status, text)
            return self._send(app.webhook(raw, self.headers.get("X-Signature")), "")

        def do_PUT(self):
            self._send(405, "no")
        do_DELETE = do_PATCH = do_PUT
    return H


# ── the private door (Unix socket) ───────────────────────────────────────
# Only deck-api opens it (group dougring-api). It says who is asking in
# X-DougRing-Role: "owner" (the panel, the phone) or "douglas" (his camera
# tool, on deck-api's camera token). Anything else is refused.
ROLES = ("owner", "douglas")


def private_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "-"
        sys_version = ""

        def address_string(self):
            return "unix"

        def log_message(self, fmt, *args):
            pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _json(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, e):
            if isinstance(e, PermissionError):
                return self._json(403, {"error": str(e)})
            if isinstance(e, RingError):
                code = e.status if e.status in (401, 403, 404, 409, 416, 429, 503) else 502
                return self._json(code, {"error": "ring", "status": e.status, "code": e.detail})
            log(f"private: {type(e).__name__}")
            return self._json(502, {"error": type(e).__name__})

        def _role(self):
            r = (self.headers.get("X-DougRing-Role") or "").strip()
            return r if r in ROLES else None

        def _req(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                raise ValueError("bad length")
            got = json.loads(self.rfile.read(n) or b"{}") if n else {}
            return got if isinstance(got, dict) else {}

        def do_GET(self):
            role = self._role()
            if role is None:
                return self._json(403, {"error": "no role"})
            try:
                if self.path == "/status":
                    st = app.status()
                    if role != "owner":
                        st = {"global_on": st["global_on"], "linked": bool(st["accounts"]),
                              "throttled_for_s": st["throttled_for_s"]}
                    return self._json(200, st)
                if self.path == "/cameras":
                    return self._json(200, {"global_on": bool(app.store.data.get("global_on")),
                                            "cameras": app.cameras(role)})
                if self.path == "/journal" and role == "owner":
                    return self._json(200, {"journal": app.journal()})
            except Exception as e:
                return self._fail(e)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            role = self._role()
            if role is None:
                return self._json(403, {"error": "no role"})
            parts = self.path.strip("/").split("/")
            try:
                req = self._req()
            except ValueError:
                return self._json(400, {"error": "bad body"})
            try:
                if parts == ["image"]:
                    img, info = app.image(role, req.get("camera", ""), fresh=req.get("fresh", True) is not False)
                    self.send_response(200)
                    self.send_header("Content-Type", info["content_type"])
                    self.send_header("Content-Length", str(len(img)))
                    self.send_header("X-Observation", json.dumps(info))
                    self.end_headers()
                    return self.wfile.write(img)
                if role != "owner":
                    return self._json(403, {"error": "the owner's alone"})
                if parts == ["switch"] and isinstance(req.get("on"), bool):
                    return self._json(200, {"ok": app.set_global(req["on"])})
                if parts == ["forget"]:
                    return self._json(200, {"ok": app.forget()})
                if len(parts) == 3 and parts[0] == "cameras" and parts[2] in ("enable", "disable", "name"):
                    did = urllib.parse.unquote(parts[1])
                    ok = (app.set_name(did, req.get("name", "")) if parts[2] == "name"
                          else app.set_enabled(did, parts[2] == "enable"))
                    return self._json(200 if ok else 404, {"ok": ok})
                if parts == ["devices", "refresh"]:
                    return self._json(200, {"cameras": len(app.devices(fresh=True))})
            except Exception as e:
                return self._fail(e)
            self._json(404, {"error": "not found"})
    return H


class BoundedHTTPServer(ThreadingHTTPServer):
    """At most MAX_IN_FLIGHT requests at once; the rest are closed at once.
    Errors (a caller who hung up) are one line, never a traceback."""
    daemon_threads = True

    def __init__(self, *a, limit=MAX_IN_FLIGHT, **k):
        self.slots = threading.BoundedSemaphore(limit)
        super().__init__(*a, **k)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        log(f"public: {sys.exc_info()[0].__name__}")


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        req, _ = super().get_request()
        return req, ("unix", 0)


def serve(app, public_addr, socket_path):
    pub = BoundedHTTPServer(public_addr, public_handler(app))
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    old = os.umask(0o117)                      # 0660: this user and the group
    priv = UnixHTTPServer(socket_path, private_handler(app))
    os.umask(old)
    threading.Thread(target=priv.serve_forever, daemon=True).start()

    def refresher():
        while True:
            try:
                app.refresh_all()
            except Exception as e:
                log(f"refresher: {type(e).__name__}")
            time.sleep(300)
    threading.Thread(target=refresher, daemon=True).start()
    log(f"dougring {VERSION}: public on {public_addr[0]}:{public_addr[1]}, private on {socket_path}")
    pub.serve_forever()


def main():
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    cred_path = os.path.join(cred_dir, "ring-app") if cred_dir else os.environ.get("DOUGRING_CREDENTIALS", "")
    if not cred_path or not os.path.exists(cred_path):
        raise SystemExit("dougring: no credentials (systemd LoadCredential=ring-app:...)")
    creds = load_credentials(cred_path)
    state_dir = os.environ.get("STATE_DIRECTORY", "/var/lib/dougring")
    store = Store(os.path.join(state_dir, "state.json"))
    app = DougRing(creds, store, Ring(creds), webhooks=os.environ.get("DOUGRING_WEBHOOKS") == "1")
    host = os.environ.get("DOUGRING_PUBLIC_HOST", "127.0.0.1")
    port = int(os.environ.get("DOUGRING_PUBLIC_PORT", "9130"))
    sock = os.environ.get("DOUGRING_SOCKET", "/run/dougring/api.sock")
    serve(app, (host, port), sock)


if __name__ == "__main__":
    main()
