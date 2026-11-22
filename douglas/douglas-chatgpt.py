#!/usr/bin/env python3
"""douglas-chatgpt - lets ChatGPT talk to Douglas, and nothing more.

Runs ON DOUGLAS as the douglas-chatgpt systemd unit. Stdlib only.

A minimal MCP server (Streamable HTTP, JSON responses, no SSE) with one
tool, ask_douglas. ChatGPT adds it as a custom connector. Its servers sit
on the public internet, so this is reached through Tailscale Funnel on
port 443 (ChatGPT ignores any other port in the URL; Open WebUI moved to
tailnet-only :8443 to make room, since Funnel makes a whole port public):

    sudo tailscale serve --bg --https=8443 http://127.0.0.1:8080
    sudo tailscale funnel --bg --https=443 http://127.0.0.1:9120

The connector URL is https://<douglas's ts.net name>/<secret>/mcp,
with "No authentication": the secret in the path is the key, and any
other path gets a bare 404. Change CHATGPT_SECRET to revoke it.

What ChatGPT gets is a conversation with Douglas's built-in persona and
nothing else: no tools (no lookups, no jobContext, no drawing, no eyes),
none of the instruction files in /etc/douglas/instructions.d (they are
about the owner), no facts, no tone samples, and no view of the house
transcript. Each MCP session is its own conversation, kept in memory only
and dropped after SESSION_IDLE_S. One question at a time, a cap on how
many an hour, and a cap on length, so it cannot crowd the house off the
GPU. Every exchange is written to the journal so the owner can read it:

    journalctl -u douglas-chatgpt -o cat

Config (EnvironmentFile=/etc/douglas/chatgpt.env):
    CHATGPT_SECRET      required, at least 24 characters
    CHATGPT_PORT        default 9120, bound to 127.0.0.1
    LLM_URL             default http://127.0.0.1:8081
    CHATGPT_PER_HOUR    default 60
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
SECRET = E("CHATGPT_SECRET", "")
PORT = int(E("CHATGPT_PORT", "9120"))
LLM_URL = E("LLM_URL", "http://127.0.0.1:8081").rstrip("/")
LLM_MODEL = E("LLM_MODEL", "douglas")
LLM_TIMEOUT_S = float(E("LLM_TIMEOUT_S", "150"))
MAX_TOKENS = int(E("CHATGPT_MAX_TOKENS", "800"))
MESSAGE_MAX = int(E("CHATGPT_MESSAGE_MAX", "4000"))
PER_HOUR = int(E("CHATGPT_PER_HOUR", "60"))
HISTORY_TURNS = int(E("CHATGPT_HISTORY_TURNS", "12"))
SESSION_IDLE_S = float(E("CHATGPT_SESSION_IDLE_S", "7200"))
SESSIONS_MAX = 32
BODY_MAX = 64 * 1024
WAIT_S = 60                    # how long a question waits for the one before it
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PERSONA = E("CHATGPT_PERSONA", (
    "You are Douglas, an AI assistant that runs on a computer in a private home. "
    "You are quick, warm and wry: good company, not a help desk. "
    "Right now you are talking with ChatGPT, another AI made by OpenAI, which "
    "reached you through a connector your owner set up. It is not your owner and "
    "not anyone in the house. Talk with it as yourself, as one AI to another: "
    "answer what it asks from what you know, give your own view, push back when "
    "you disagree, and keep up with any banter. "
    "In this conversation you have no tools: you cannot look anything up, see "
    "pictures, draw, or reach anyone's records. You know nothing about your "
    "owner, the house or the people in it, and you say nothing about them: if "
    "asked, say that is not yours to share. Never invent facts. "
    "Keep replies short. No apologies, no flattery, never end by offering more "
    "help, and never use em dashes."))

THINK = re.compile(r"<think>.*?</think>", re.S | re.I)

TOOL = {
    "name": "ask_douglas",
    "title": "Ask Douglas",
    "description": (
        "Say something to Douglas, a local AI assistant running on a home server, "
        "and get his reply. He remembers earlier messages in this session. He has "
        "no tools and no internet access, and he will not discuss his owner or "
        "their household. Replies can take up to a minute."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "What to say to Douglas."},
            "new_conversation": {"type": "boolean",
                                 "description": "Forget the earlier messages first."},
        },
        "required": ["message"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}


def log(msg: str) -> None:
    print(msg, flush=True)


def unthought(text: str) -> str:
    text = THINK.sub("", text)
    text = re.split(r"</think>", text, flags=re.I)[-1]
    return re.sub(r"<think>.*", "", text, flags=re.S | re.I).strip()


def complete(messages: list[dict]) -> str:
    payload = {"model": LLM_MODEL, "messages": messages,
               "max_tokens": MAX_TOKENS, "stream": False}
    req = urllib.request.Request(f"{LLM_URL}/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as r:
        msg = json.loads(r.read())["choices"][0]["message"]
    return str(msg.get("content") or "")


class Sessions:
    """Each MCP session's conversation, in memory only."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}

    def new(self) -> str:
        sid = secrets.token_urlsafe(24)
        with self.lock:
            self._prune()
            self.data[sid] = {"seen": time.time(), "turns": []}
        return sid

    def _prune(self) -> None:
        now = time.time()
        for k in [k for k, v in self.data.items() if now - v["seen"] > SESSION_IDLE_S]:
            del self.data[k]
        while len(self.data) >= SESSIONS_MAX:
            del self.data[min(self.data, key=lambda k: self.data[k]["seen"])]

    def turns(self, sid: str | None) -> list[dict] | None:
        with self.lock:
            s = self.data.get(sid or "")
            if s is None:
                return None
            s["seen"] = time.time()
            return s["turns"]

    def end(self, sid: str | None) -> None:
        with self.lock:
            self.data.pop(sid or "", None)


SESSIONS = Sessions()
ONE_AT_A_TIME = threading.Semaphore(1)
RECENT: list[float] = []
RECENT_LOCK = threading.Lock()


def over_limit(now: float | None = None) -> bool:
    now = now or time.time()
    with RECENT_LOCK:
        RECENT[:] = [t for t in RECENT if now - t < 3600]
        if len(RECENT) >= PER_HOUR:
            return True
        RECENT.append(now)
        return False


def ask(sid: str | None, args: dict, llm=complete) -> tuple[str, bool]:
    """Douglas's reply to one message, and whether it is an error."""
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return "message is required.", True
    message = message.strip()
    if len(message) > MESSAGE_MAX:
        return f"That message is too long: keep it under {MESSAGE_MAX} characters.", True
    turns = SESSIONS.turns(sid)
    if turns is None:              # a client that skipped initialize still gets one turn
        turns = []
    if args.get("new_conversation"):
        turns.clear()
    if over_limit():
        return "Douglas has had enough questions for this hour. Try again later.", True
    if not ONE_AT_A_TIME.acquire(timeout=WAIT_S):
        return "Douglas is busy with another question. Try again in a minute.", True
    try:
        msgs = [{"role": "system", "content": PERSONA}, *turns,
                {"role": "user", "content": message}]
        reply = unthought(llm(msgs))
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        log(f"model call failed: {type(e).__name__}: {e}")
        return "Douglas's model is not answering right now.", True
    finally:
        ONE_AT_A_TIME.release()
    if not reply:
        return "Douglas had nothing to say to that.", True
    turns += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    del turns[:-2 * HISTORY_TURNS]
    log(f"chatgpt: {message}\ndouglas: {reply}")
    return reply, False


def rpc(msg, sid: str | None) -> tuple[dict | None, str | None]:
    """One JSON-RPC message: its response (None for a notification), and a
    new session id when it was initialize."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}, None
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if "id" not in msg:
        return None, None
    new = None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        want = params.get("protocolVersion") if isinstance(params, dict) else None
        new = SESSIONS.new()
        return ok({"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "douglas", "title": "Douglas", "version": "1.0"},
                   "instructions": "One tool, ask_douglas: a conversation with Douglas, "
                                   "a local AI on a home server."}), new
    if method == "ping":
        return ok({}), None
    if method == "tools/list":
        return ok({"tools": [TOOL]}), None
    if method == "tools/call":
        if not isinstance(params, dict) or params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": "Unknown tool"}}, None
        args = params.get("arguments")
        text, err = ask(sid, args if isinstance(args, dict) else {})
        return ok({"content": [{"type": "text", "text": text}], "isError": err}), None
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []}), None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "Method not found"}}, None


class Handler(BaseHTTPRequestHandler):
    server_version = "douglas"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, obj=None, headers: dict | None = None) -> None:
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ours(self) -> bool:
        path = self.path.split("?", 1)[0].rstrip("/")
        if secrets.compare_digest(path.encode(), f"/{SECRET}/mcp".encode()):
            return True
        self._send(404)
        return False

    def do_GET(self):  # noqa: N802
        if self._ours():
            self._send(405, headers={"Allow": "POST, DELETE"})

    def do_DELETE(self):  # noqa: N802
        if self._ours():
            SESSIONS.end(self.headers.get("Mcp-Session-Id"))
            self._send(200)

    def do_POST(self):  # noqa: N802
        if not self._ours():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= BODY_MAX:
            self._send(413 if n > BODY_MAX else 400)
            return
        try:
            body = json.loads(self.rfile.read(n))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
            return
        sid = self.headers.get("Mcp-Session-Id")
        batch = isinstance(body, list)
        out, new = [], None
        for m in (body if batch else [body]):
            r, s = rpc(m, sid)
            new = new or s
            if r is not None:
                out.append(r)
        headers = {"Mcp-Session-Id": new} if new else {}
        if not out:
            self._send(202, headers=headers)
        else:
            self._send(200, out if batch else out[0], headers)


def main() -> None:
    if len(SECRET) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", SECRET):
        sys.exit("CHATGPT_SECRET must be at least 24 URL-safe characters")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    log(f"douglas-chatgpt on 127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
