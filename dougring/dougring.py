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
  private  a Unix socket (/run/dougring/api.sock), for Mission Control on this
           machine only: status, the camera list, which cameras are switched
           on, and a still picture from a switched-on camera.

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

NONCE_WINDOW_S = 600          # Ring: reject a link older than 10 minutes
CLOCK_SKEW_S = 60             # a little room for a clock that runs ahead
UNCLAIMED_TTL_S = 900         # an exchanged token waits this long for its browser
LINK_WAIT_S = 15              # the browser may beat the token exchange by a moment
REFRESH_MARGIN_S = 1800       # refresh when the access token has this little left
REFRESH_EVERY_S = 6 * 3600    # and at least this often, so the 30-day token rotates
MAX_BODY = 64 * 1024
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


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

    def __init__(self, creds, oauth_url=OAUTH_URL, api_url=API_URL, timeout=20):
        self.creds = creds
        self.oauth_url = oauth_url
        self.api_url = api_url
        self.timeout = timeout
        self._noredirect = urllib.request.build_opener(_NoRedirect)

    def _token_call(self, fields):
        data = urllib.parse.urlencode({**fields,
                                       "client_id": self.creds["RING_CLIENT_ID"],
                                       "client_secret": self.creds["RING_CLIENT_SECRET"]}).encode()
        req = urllib.request.Request(self.oauth_url, data=data, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                tok = json.loads(r.read())
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
        opener = urllib.request.urlopen if redirect else self._noredirect.open
        try:
            with opener(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, dict(r.headers), raw
        except urllib.error.HTTPError as e:
            if not redirect and e.code in (301, 302, 303, 307, 308):
                return e.code, dict(e.headers), b""
            raise RingError(e.code, _error_code(e.read())) from None

    def api_json(self, method, path, access_token, account_id, body=None):
        _, _, raw = self.api(method, path, access_token, account_id, body)
        return json.loads(raw) if raw else {}

    def user_me(self, access_token):
        # Hardware_Id is per account; before we know the account, use a fixed one.
        req = urllib.request.Request(self.api_url + "/v1/users/me", headers={
            "Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT,
            "Hardware_Id": hardware_id("pending"), "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())["data"]
        except urllib.error.HTTPError as e:
            raise RingError(e.code, _error_code(e.read())) from None
        return {"id": str(data["id"]), "email": data.get("attributes", {}).get("email", "")}

    def fetch_presigned(self, url):
        """The picture itself: a pre-signed URL, no Bearer token sent to it."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            raise RingError(e.code, _error_code(e.read())) from None


class Store:
    """Everything this service remembers, in one 0600 JSON file, written atomically."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"unclaimed": [], "accounts": {}, "enabled": [], "events": [], "seen": []}
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

    # ── token exchange (Ring's servers → us) ─────────────────────────────
    def token_exchange(self, form):
        code = form.get("code", "")
        if not code:
            return 400, "missing code"
        if form.get("client_id") and not hmac.compare_digest(form["client_id"], self.creds["RING_CLIENT_ID"]):
            log("token exchange: refused, client_id is not this app's")
            return 400, "wrong client"
        try:
            tok = self.ring.exchange_code(code)
            me = self.ring.user_me(tok["access_token"])
        except RingError as e:
            log(f"token exchange: failed, {e}")
            return 502, "exchange failed"
        except Exception as e:  # network
            log(f"token exchange: failed, {type(e).__name__}")
            return 502, "exchange failed"
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
            for u in self.store.data["unclaimed"]:
                if hmac.compare_digest(compute_nonce(time_param, u["account_id"], self.creds["RING_HMAC_KEY"]), nonce):
                    return u
        return None

    def account_link(self, nonce, time_param, wait=None):
        """Returns (http status, title, sentence, masked e-mail or None)."""
        if not nonce or not time_param:
            return 200, "DougRing", "This is DougRing's account link page. Start linking from the Ring Developer Console's Connect step.", None
        if not time_param.isdigit() or len(nonce) > 128:
            return 400, "Link refused", "The link Ring sent was malformed.", None
        age = (self.now() * 1000 - int(time_param)) / 1000
        if age > NONCE_WINDOW_S:
            return 400, "Link expired", "That link is more than ten minutes old. Start again from the Ring Developer Console.", None
        if age < -CLOCK_SKEW_S:
            return 400, "Link refused", "That link's time is in the future.", None
        deadline = self.now() + (LINK_WAIT_S if wait is None else wait)
        rec = self._match(nonce, time_param)
        while rec is None and self.now() < deadline:
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
        return 200, "DougRing is linked", f"Ring account {ident} is linked to DougRing. You can close this page.", ident

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
            kind = str(data.get("type") or meta.get("event_type") or "?")[:40]
            entry = {"kind": kind, "sub": str(attrs.get("sub_type") or "")[:30],
                     "device": str(attrs.get("source") or data.get("id") or "")[:80],
                     "at": attrs.get("timestamp") or int(self.now() * 1000),
                     "account": short_id(meta.get("account_id"))}
            self.store.data["events"] = (self.store.data["events"] + [entry])[-200:]
            if kind == "app_integration_removed" and meta.get("account_id") in self.store.data["accounts"]:
                acct = self.store.data["accounts"].pop(meta["account_id"])
                log(f"webhook: {acct['email_masked']} removed DougRing; tokens dropped")
            self.store.save()
        log(f"webhook: {entry['kind']} {entry['sub']}".rstrip())
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
            except OSError as e:
                log(f"refresh: network, {type(e).__name__}")

    def _call(self, account_id, method, path, body=None, redirect=True):
        acct = self._refresh(account_id)
        if acct is None:
            raise RingError(401, "account not linked")
        try:
            return self.ring.api(method, path, acct["access_token"], account_id, body, redirect)
        except RingError as e:
            if e.status != 401:
                raise
            acct = self._refresh(account_id, force=True)
            if acct is None:
                raise
            return self.ring.api(method, path, acct["access_token"], account_id, body, redirect)

    # ── what Mission Control asks ────────────────────────────────────────
    def status(self):
        with self.store.lock:
            return {"accounts": [{"account": short_id(aid), "email": a["email_masked"], "state": a["state"],
                                  "access_expires_in_s": int(a["expires_at"] - self.now()),
                                  "linked_at": int(a["linked_at"])}
                                 for aid, a in self.store.data["accounts"].items()],
                    "unclaimed": len(self.store.data["unclaimed"]),
                    "enabled": list(self.store.data["enabled"]),
                    "recent_events": self.store.data["events"][-10:]}

    def devices(self):
        out = []
        for aid in list(self.store.data["accounts"]):
            _, _, raw = self._call(aid, "GET", "/v1/devices?include=status,capabilities")
            doc = json.loads(raw)
            included = {(i.get("type"), i.get("id")): i.get("attributes", {}) for i in doc.get("included", [])}
            for d in doc.get("data", []):
                a = d.get("attributes", {})
                rel = d.get("relationships", {})
                st = {}
                for key in ("status",):
                    ref = (rel.get(key) or {}).get("data") or {}
                    st = included.get((ref.get("type"), ref.get("id")), {})
                out.append({"id": d.get("id"), "account": short_id(aid),
                            "name": a.get("name") or a.get("description") or "",
                            "kind": a.get("type") or a.get("device_type") or d.get("type"),
                            "model": a.get("model") or a.get("kind") or "",
                            "status": st, "enabled": d.get("id") in self.store.data["enabled"],
                            "attributes": {k: v for k, v in a.items() if k not in ("location",)}})
        return out

    def _owner(self, device_id):
        for dev in self.devices():
            if dev["id"] == device_id:
                for aid in self.store.data["accounts"]:
                    if short_id(aid) == dev["account"]:
                        return aid
        return None

    def set_enabled(self, device_id, on):
        if on and self._owner(device_id) is None:
            return False
        with self.store.lock:
            en = [d for d in self.store.data["enabled"] if d != device_id]
            if on:
                en.append(device_id)
            self.store.data["enabled"] = en
            self.store.save()
        log(f"camera {device_id[-6:]}: {'switched on' if on else 'switched off'}")
        return True

    def snapshot(self, device_id, at_ms=None):
        """The latest existing picture (Ring never takes a new one for this)."""
        if device_id not in self.store.data["enabled"]:
            raise PermissionError("camera is switched off in DougRing")
        aid = self._owner(device_id)
        if aid is None:
            raise PermissionError("camera is not on a linked account")
        now_ms = int(self.now() * 1000)
        body = ({"type": "at_timestamp", "timestamp": int(at_ms)} if at_ms else
                {"type": "latest_in_range", "start_timestamp": now_ms - SNAPSHOT_WINDOW_MS + 60_000})
        body["image_options"] = {"format": "jpeg", "resolution": {"width": 1920, "height": 1080}}
        path = f"/v1/devices/{urllib.parse.quote(device_id, safe='')}/media/image/download"
        status, headers, _ = self._call(aid, "POST", path, body, redirect=False)
        loc = {k.lower(): v for k, v in headers.items()}.get("location")
        if status != 303 or not loc:
            raise RingError(status, "no picture offered")
        img, h = self.ring.fetch_presigned(loc)
        h = {k.lower(): v for k, v in h.items()}
        log(f"snapshot: camera {device_id[-6:]}, {len(img)} bytes, {h.get('x-media-origin', '?')}")
        return img, {"content_type": h.get("content-type", "image/jpeg"),
                     "media_timestamp": h.get("x-media-timestamp", ""),
                     "media_origin": h.get("x-media-origin", "")}


# ── the public door ──────────────────────────────────────────────────────
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font:17px/1.5 system-ui,sans-serif;max-width:32em;margin:12vh auto;padding:0 1em;color:#222}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}}}</style></head>
<body><h1>{title}</h1><p>{text}</p></body></html>"""

PUBLIC_PATHS = ("/ring/token", "/ring/link", "/ring/webhook", "/ring/home")


def public_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "DougRing"
        sys_version = ""
        timeout = 15

        def log_message(self, fmt, *args):
            pass  # the query string carries nonces: no access log at all

        def _send(self, status, body=b"", ctype="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode()
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
            if n > MAX_BODY:
                raise ValueError("too big")
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path not in PUBLIC_PATHS or (u.path == "/ring/webhook" and not app.webhooks):
                return self._send(404, "not found")
            if u.path == "/ring/link":
                q = urllib.parse.parse_qs(u.query)
                status, title, text, _ = app.account_link(q.get("nonce", [""])[0], q.get("time", [""])[0])
                return self._page(status, title, text)
            if u.path == "/ring/home":
                return self._page(200, "DougRing", "DougRing is Douglas's private link to this household's Ring cameras.")
            return self._send(200, "DougRing: this address takes a POST from Ring.")

        do_HEAD = do_GET

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/ring/token" and not (u.path == "/ring/webhook" and app.webhooks):
                return self._send(404, "not found")
            try:
                raw = self._body()
            except ValueError:
                return self._send(413, "too big")
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
def private_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "DougRing"
        sys_version = ""

        def address_string(self):
            return "unix"

        def log_message(self, fmt, *args):
            pass

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
                return self._json(502 if e.status >= 500 or e.status < 400 else e.status,
                                  {"error": "ring", "status": e.status, "code": e.detail})
            return self._json(502, {"error": type(e).__name__})

        def do_GET(self):
            try:
                if self.path == "/status":
                    return self._json(200, app.status())
                if self.path == "/devices":
                    return self._json(200, {"devices": app.devices()})
            except Exception as e:
                return self._fail(e)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            try:
                if len(parts) == 3 and parts[0] == "cameras" and parts[2] in ("enable", "disable"):
                    ok = app.set_enabled(urllib.parse.unquote(parts[1]), parts[2] == "enable")
                    return self._json(200 if ok else 404, {"ok": ok})
                if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "snapshot":
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(n)) if n else {}
                    img, meta = app.snapshot(urllib.parse.unquote(parts[1]), req.get("at_ms"))
                    self.send_response(200)
                    self.send_header("Content-Type", meta["content_type"])
                    self.send_header("Content-Length", str(len(img)))
                    self.send_header("X-Media-Timestamp", meta["media_timestamp"])
                    self.send_header("X-Media-Origin", meta["media_origin"])
                    self.end_headers()
                    return self.wfile.write(img)
            except Exception as e:
                return self._fail(e)
            self._json(404, {"error": "not found"})
    return H


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        req, _ = super().get_request()
        return req, ("unix", 0)


def serve(app, public_addr, socket_path):
    pub = ThreadingHTTPServer(public_addr, public_handler(app))
    pub.daemon_threads = True
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    old = os.umask(0o117)                      # 0660: this user and the group
    priv = UnixHTTPServer(socket_path, private_handler(app))
    os.umask(old)
    threading.Thread(target=priv.serve_forever, daemon=True).start()

    def refresher():
        while True:
            app.refresh_all()
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
