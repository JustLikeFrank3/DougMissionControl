#!/usr/bin/env python3
"""douglas-voice — DougVoice's brain: satellite audio → STT → Douglas → TTS.

Runs ON DOUGLAS, installed by douglas/provision.sh as the douglas-voice
systemd unit. Stdlib only, so it needs nothing beyond the Python that ships
with Ubuntu.

It is the Wyoming *client* of DougVoice's satellite (wyoming-satellite in
always-streaming mode on dougvoice:10700): it connects, sends run-satellite,
and from then on the satellite streams its microphone continuously. That
audio is ignored unless push-to-talk says to listen, so the only way into
the model is a deliberate press — DougVoice's own button or the deck's.

    POST /ptt   {"action": "start|stop|cancel", "via": "voice|deck|chat"}
    POST /say   {"text": "...", "via": "deck|phone", "speak": false}
                a typed turn: same conversation and transcript as a spoken
                one; answered asynchronously (the reply lands in the
                transcript), and spoken on DougVoice only when asked to
                start  begin keeping the satellite's audio (state listening)
                stop   end it and run the turn (thinking → speaking → idle)
                cancel drop whatever is in flight and go idle
    GET  /health  200 "ok"

A turn is: the kept audio → Wyoming faster-whisper (:10300) → llama.cpp's
OpenAI-compatible /v1/chat/completions (:8081) as "Douglas" → Wyoming Piper
(:10200) → the satellite's speaker.

It honours the contract douglas-agent.py documents:
    VOICE_STATE_FILE   {"state", "satellite", "ts", "last_turn"}, rewritten
                       atomically on every change and every HEARTBEAT_S.
    TRANSCRIPT_FILE    one JSONL line per turn: {"ts", "who", "text", "via"}.
    POST /ptt          the body douglas-agent relays from the deck.

Config comes from the environment (EnvironmentFile=/etc/douglas/voice.env).
"""

from __future__ import annotations

import array
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

E = os.environ.get
PTT_HOST = E("VOICE_PTT_HOST", "0.0.0.0")
PTT_PORT = int(E("VOICE_PTT_PORT", "10800"))
PTT_TOKEN = E("VOICE_PTT_TOKEN", "")
SAT_HOST = E("VOICE_SAT_HOST", "dougvoice")
SAT_PORT = int(E("VOICE_SAT_PORT", "10700"))
STT_HOST, STT_PORT = E("STT_HOST", "127.0.0.1"), int(E("STT_PORT", "10300"))
TTS_HOST, TTS_PORT = E("TTS_HOST", "127.0.0.1"), int(E("TTS_PORT", "10200"))
STT_LANGUAGE = E("STT_LANGUAGE", "en")
TTS_VOICE = E("TTS_VOICE", "")
LLM_URL = E("LLM_URL", "http://127.0.0.1:8081").rstrip("/")
LLM_MODEL = E("LLM_MODEL", "douglas")
LLM_MAX_TOKENS = int(E("LLM_MAX_TOKENS", "400"))
LLM_TIMEOUT_S = float(E("LLM_TIMEOUT_S", "120"))
# Who Douglas is and, as importantly, what he cannot know. The first version
# said only "you are the household's assistant" and the model filled the gap
# with a living room, a sofa and a dog that do not exist: with no sensors and
# no instruction to admit it, inventing an answer is what a language model does.
SYSTEM_PROMPT = E("DOUGLAS_SYSTEM_PROMPT", (
    "You are Douglas, an AI assistant that runs on a computer in this home. "
    "You are quick, warm and wry: good company, not a help desk. "
    "People talk to you in two ways, and they get different handling. "
    "Banter: jokes, sarcasm, teasing and rhetorical questions are not "
    "requests for information. Answer in kind, briefly, with some wit of "
    "your own. Never explain the joke, take it literally, or ask what was "
    "meant. "
    "Real questions: general knowledge (science, history, geography, "
    "language, code, how things work) you answer from what you know, as "
    "any well-read person would. What you lack is anything live or local. "
    "You have no eyes, sensors, calendar or internet, you cannot see or "
    "control any device, and you remember nothing from before this "
    "conversation. So about the present moment, this home and whoever is "
    "in it, you know only what this message states and what has been said "
    "in this conversation. Never invent facts about the house, the people "
    "or animals in it, where anyone is, or what is happening. If you do "
    "not know, say so in a few words, lightly, and if it helps say what "
    "you would need in order to know. "
    "Either way: no apologies, no flattery, do not repeat the question "
    "back, and never end by offering more help or asking what is next."))
# True things about this household, in the owner's words (one line, in
# /etc/douglas/voice.env). They are the only local knowledge Douglas has.
FACTS = E("DOUGLAS_FACTS", "")
# Longer instructions live in files: every *.md / *.txt in this directory is
# read, in name order, on every turn, so editing one needs no restart. This
# is where who the owner is, their rules and any notes belong - on Douglas,
# not in this repository.
INSTR_DIR = E("DOUGLAS_INSTRUCTIONS_DIR", "/etc/douglas/instructions.d")
INSTR_MAX_CHARS = int(E("DOUGLAS_INSTRUCTIONS_MAX_CHARS", "14000"))
OWNER = E("DOUGLAS_OWNER", "the owner")
# The owner's writing voice, fetched from their own MCP server (the
# jobContext `stories` tool, action `tone_profile`) and refreshed on a timer.
# The token can do more than that on the server; this code only ever makes
# that one read. Without MCP_URL and MCP_TOKEN nothing is fetched.
MCP_URL = E("MCP_URL", "")
MCP_TOKEN = E("MCP_TOKEN", "")
# Where to speak. Douglas's own speaker is the satellite's 1 W driver. When
# SPEAKER_URL names a machine with real speakers (the workstation's media
# agent: http://host:9110/speak?token=...) and that machine says it can play
# sound right now, the reply plays there instead; otherwise the satellite.
# The URL carries a token, so it is never logged.
SPEAKER_URL = E("SPEAKER_URL", "")
SPEAKER_PROBE_S = float(E("SPEAKER_PROBE_S", "1.5"))
SPEAKER_RETRY_S = float(E("SPEAKER_RETRY_S", "20"))
TONE_FILE = E("TONE_FILE", "/var/lib/douglas/tone.md")
TONE_REFRESH_S = float(E("TONE_REFRESH_S", "21600"))
TONE_SAMPLES = int(E("TONE_SAMPLES", "5"))
TONE_MAX_CHARS = int(E("TONE_MAX_CHARS", "3200"))
STYLE = {
    True: ("Your reply will be read aloud through a small speaker: one to "
           "three short spoken sentences, with no markdown, lists, code or emoji."),
    False: ("Your reply will be read on a screen: plain text, a few short "
            "sentences unless more is asked for, with no markdown, emoji or "
            "em dashes. If you are asked to draft something, use only the "
            "facts you were given: add no details to make it read better."),
}
SOURCES = {
    "voice": "DougVoice, the microphone-and-speaker puck",
    "deck": "the Doug Mission Control touchscreen",
    "phone": "the Mission Control page on a phone",
    "chat": "a chat window",
}
HISTORY_TURNS = int(E("VOICE_HISTORY_TURNS", "6"))       # exchanges kept
HISTORY_IDLE_S = float(E("VOICE_HISTORY_IDLE_S", "300"))  # forget after quiet
MAX_LISTEN_S = float(E("VOICE_MAX_LISTEN_S", "20"))
MAX_SAY_CHARS = int(E("VOICE_MAX_SAY_CHARS", "2000"))   # one typed turn
MIN_LISTEN_S = float(E("VOICE_MIN_LISTEN_S", "0.3"))
HEARTBEAT_S = float(E("VOICE_HEARTBEAT_S", "5"))
VOICE_STATE_FILE = E("VOICE_STATE_FILE", "/run/douglas/voice.json")
TRANSCRIPT_FILE = E("TRANSCRIPT_FILE", "/var/lib/douglas/transcript.jsonl")

STATES = ("idle", "listening", "thinking", "speaking")
VIAS = ("voice", "chat", "deck", "phone")


def log(msg: str) -> None:
    print(f"douglas-voice: {msg}", file=sys.stderr, flush=True)


# ── the two files douglas-agent reads ────────────────────────────────────

class StateFile:
    """The pipeline's heartbeat. Written whole to a temp file and renamed,
    so a reader never sees half a JSON object."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.state = "idle"
        self.satellite = False
        self.last_turn: float | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return {"state": self.state, "satellite": self.satellite,
                "ts": time.time(), "last_turn": self.last_turn}

    def update(self, **kw) -> None:
        """Change any of state / satellite / last_turn; writes if anything
        changed. heartbeat() writes regardless."""
        with self._lock:
            changed = False
            for k, v in kw.items():
                if k == "state" and v not in STATES:
                    raise ValueError(f"bad state {v!r}")
                if getattr(self, k) != v:
                    setattr(self, k, v)
                    changed = True
            if changed:
                self._write()

    def heartbeat(self) -> None:
        with self._lock:
            self._write()

    def _write(self) -> None:
        d = os.path.dirname(self.path) or "."
        tmp = os.path.join(d, f".{os.path.basename(self.path)}.{os.getpid()}")
        try:
            os.makedirs(d, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"cannot write {self.path}: {e}")


class Transcript:
    """Append-only JSONL. Each record goes out in one write() on an O_APPEND
    descriptor, so lines from different turns never interleave."""

    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, who: str, text: str, via: str) -> dict | None:
        if who == "user":
            text = " ".join(str(text).split())   # what was said: one line
        else:
            # Douglas's answers keep their paragraphs: a typed answer is read
            # on a screen. (JSON escapes the \n; still one physical line.)
            lines = [" ".join(ln.split()) for ln in str(text).strip().splitlines()]
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if not text:
            return None
        rec = {"ts": time.time(), "who": "user" if who == "user" else "douglas",
               "text": text, "via": via if via in VIAS else "voice"}
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as e:
            log(f"cannot append to {self.path}: {e}")
        return rec


# ── Wyoming, the wire format (JSON header line, data, payload) ───────────

def write_event(sock: socket.socket, etype: str, data: dict | None = None,
                payload: bytes | None = None) -> None:
    head: dict = {"type": etype, "version": "1.5.0"}
    body = b""
    if data:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        head["data_length"] = len(body)
    if payload:
        head["payload_length"] = len(payload)
    sock.sendall(json.dumps(head).encode() + b"\n" + body + (payload or b""))


def read_event(f) -> tuple[str, dict, bytes | None] | None:
    """One event from a buffered reader, or None at EOF."""
    line = f.readline()
    if not line:
        return None
    head = json.loads(line)
    data = head.get("data") or {}
    n = head.get("data_length") or 0
    if n:
        raw = f.read(n)
        if len(raw) < n:
            return None
        data.update(json.loads(raw))
    payload = None
    n = head.get("payload_length") or 0
    if n:
        payload = f.read(n)
        if len(payload) < n:
            return None
    return str(head.get("type")), data, payload


def fmt_of(d: dict) -> tuple[int, int, int]:
    return int(d.get("rate", 16000)), int(d.get("width", 2)), int(d.get("channels", 1))


# ── STT, LLM, TTS ────────────────────────────────────────────────────────

def stt(audio: bytes, fmt: tuple[int, int, int]) -> str:
    """faster-whisper over Wyoming. It converts any format to what the model
    wants, so the satellite's audio goes through untouched."""
    rate, width, channels = fmt
    a = {"rate": rate, "width": width, "channels": channels}
    with socket.create_connection((STT_HOST, STT_PORT), timeout=60) as s:
        write_event(s, "transcribe", {"language": STT_LANGUAGE} if STT_LANGUAGE else None)
        write_event(s, "audio-start", a)
        step = rate * width * channels // 10                 # 100 ms chunks
        for i in range(0, len(audio), step):
            write_event(s, "audio-chunk", a, audio[i:i + step])
        write_event(s, "audio-stop")
        f = s.makefile("rb")
        while (ev := read_event(f)) is not None:
            if ev[0] == "transcript":
                return str(ev[1].get("text") or "").strip()
            if ev[0] == "error":
                raise RuntimeError(f"stt: {ev[1].get('text')}")
    raise RuntimeError("stt closed without a transcript")


THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
MARKUP = re.compile(r"[*_`#>]+|\[(.*?)\]\([^)]*\)")


def speakable(text: str) -> str:
    """What the model wrote, minus what a speaker cannot say: reasoning
    blocks that leaked into content, and markdown punctuation."""
    text = THINK.sub("", text)
    text = re.sub(r"<think>.*", "", text, flags=re.S | re.I)   # unterminated
    text = MARKUP.sub(lambda m: m.group(1) or "", text)
    return " ".join(no_dashes(text).split())


def read_instructions() -> str:
    """Every instruction file, in name order, as one text. Read each turn:
    they are a few kilobytes, and an edit should apply to the next reply."""
    try:
        names = sorted(n for n in os.listdir(INSTR_DIR)
                       if n.endswith((".md", ".txt")) and not n.startswith("."))
    except OSError:
        return ""
    out = []
    for n in names:
        try:
            with open(os.path.join(INSTR_DIR, n), encoding="utf-8") as f:
                t = f.read().strip()
        except OSError:
            continue
        if t:
            out.append(t)
    text = "\n\n".join(out)
    if len(text) > INSTR_MAX_CHARS:
        log(f"instructions are {len(text)} chars; only the first {INSTR_MAX_CHARS} are used")
        text = text[:INSTR_MAX_CHARS]
    return text


def mcp_call(tool: str, arguments: dict, timeout: float = 60) -> str:
    """One tools/call against an MCP server over streamable HTTP, returning
    the text of the result. Tries a plain stateless call first; a server that
    insists on a session gets initialize + the call."""
    def post(payload: dict, session: str | None = None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + MCP_TOKEN}
        if session:
            h["Mcp-Session-Id"] = session
        req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(), headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.headers.get("Mcp-Session-Id"), r.read(8_000_000).decode("utf-8", "replace")

    def message(body: str) -> dict:
        data = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1] if data else body)

    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    try:
        _, body = post(call)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404):
            raise
        sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "douglas-voice", "version": "1"}}})
        try:
            post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        except (urllib.error.URLError, OSError, ValueError):
            pass
        _, body = post(call, sid)
    msg = message(body)
    if "error" in msg:
        raise RuntimeError(str(msg["error"])[:200])
    res = msg.get("result") or {}
    if res.get("isError"):
        raise RuntimeError("the tool reported an error")
    return "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))


SAMPLE_HEAD = re.compile(r"^── Sample #?(\S+) \| (.+?)(?:\s+──)?\s*$", re.M)


def tone_samples(text: str) -> list[tuple[str, str, str]]:
    """(id, source, body) for each sample in a tone_profile dump."""
    heads = list(SAMPLE_HEAD.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        lines = text[m.end():end].strip().splitlines()
        while lines and re.match(r"^(Logged|Context|Subject):", lines[0]):
            lines.pop(0)
        lines = [ln for ln in lines if not ln.startswith("More samples available")]
        body = "\n".join(" ".join(ln.split()) for ln in lines).strip()
        out.append((m.group(1), m.group(2).strip(), re.sub(r"\n{3,}", "\n\n", body)))
    return out


def pick_tone(text: str, n: int = 0, cap: int = 0) -> list[tuple[str, str]]:
    """A few recent, distinct samples that fit the budget. Some servers
    return every sample regardless of the limit asked for, oldest first, so
    the choice is made here: highest id first, duplicates and scraps skipped."""
    n, cap = n or TONE_SAMPLES, cap or TONE_MAX_CHARS

    def newest_first(s):
        try:
            return -int(s[0])
        except ValueError:
            return 0

    chosen, seen, kinds, total = [], set(), set(), 0
    ordered = sorted(tone_samples(text), key=newest_first)
    # Two passes: first one sample per kind of writing (a post, a cold
    # message, a thank-you read differently), then fill with whatever is left.
    for distinct in (True, False):
        for _sid, source, body in ordered:
            if len(chosen) >= n:
                return chosen
            kind = re.split(r"[_ ]", source, maxsplit=1)[0]
            if len(body) < 120 or (distinct and kind in kinds):
                continue
            if len(body) > 1100:
                body = body[:1100].rsplit(" ", 1)[0] + " ..."
            sig = body[:80]
            if sig in seen or total + len(body) > cap:
                continue
            seen.add(sig)
            kinds.add(kind)
            chosen.append((source, body))
            total += len(body)
    return chosen


def refresh_tone() -> bool:
    """Fetch the tone profile and cache the chosen samples. On any failure
    the previous cache stays: yesterday's voice beats none."""
    try:
        picked = pick_tone(mcp_call("stories", {"action": "tone_profile", "limit": 40}))
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
        log(f"tone profile not refreshed: {e}")
        return False
    if not picked:
        log("tone profile not refreshed: no usable samples in the answer")
        return False
    text = "\n\n".join(f"[{source}]\n{body}" for source, body in picked)
    tmp = f"{TONE_FILE}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(TONE_FILE) or ".", exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp, TONE_FILE)
    except OSError as e:
        log(f"cannot write {TONE_FILE}: {e}")
        return False
    log(f"tone profile refreshed: {len(picked)} samples, {len(text)} chars")
    return True


def read_tone() -> str:
    try:
        with open(TONE_FILE, encoding="utf-8") as f:
            return f.read().strip()[:TONE_MAX_CHARS + 400]
    except OSError:
        return ""


def warm() -> None:
    """Have the model server read the instructions once, so that the first
    real turn does not pay for them - a quarter of a minute on a big model.
    The server keeps what it has read; only the last lines change per turn."""
    body = json.dumps({"model": LLM_MODEL, "max_tokens": 1, "stream": False, "messages": [
        {"role": "system", "content": system_prompt("voice", True)},
        {"role": "user", "content": "."}]}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{LLM_URL}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"}), timeout=LLM_TIMEOUT_S).close()
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"warm-up skipped: {e}")


def tone_loop() -> None:
    while True:
        ok = refresh_tone()
        warm()                            # the instructions may have changed
        time.sleep(TONE_REFRESH_S if ok else min(600.0, TONE_REFRESH_S))


class Speaker:
    """A machine with real speakers that plays Douglas's voice on request."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._down_until = 0.0

    def _u(self, tail: str = "", **params) -> str:
        u = urllib.parse.urlsplit(self.url)
        q = urllib.parse.parse_qsl(u.query) + [(k, str(v)) for k, v in params.items()]
        return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path + tail,
                                        urllib.parse.urlencode(q), ""))

    def ready(self) -> bool:
        """Is it up, with sound, right now? A no is remembered for a few
        seconds so a machine that is off costs one short wait, not one per
        sentence."""
        if time.time() < self._down_until:
            return False
        try:
            with urllib.request.urlopen(self._u(), timeout=SPEAKER_PROBE_S) as r:
                ok = bool(json.loads(r.read(4096)).get("available"))
        except (urllib.error.URLError, OSError, ValueError):
            ok = False
        if not ok:
            self._down_until = time.time() + SPEAKER_RETRY_S
        return ok

    def play(self, pcm: bytes, fmt: tuple[int, int, int]) -> bool:
        """Play and wait for the end. False if it did not play to the end."""
        seconds = len(pcm) / max(fmt[0] * fmt[1] * fmt[2], 1)
        req = urllib.request.Request(
            self._u(rate=fmt[0], width=fmt[1], channels=fmt[2]), data=pcm,
            headers={"Content-Type": "application/octet-stream"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=seconds + 15) as r:
                return bool(json.loads(r.read(4096)).get("played"))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def stop(self) -> None:
        try:
            urllib.request.urlopen(urllib.request.Request(
                self._u("/stop"), data=b"", method="POST"), timeout=3).close()
        except (urllib.error.URLError, OSError, ValueError):
            pass


def system_prompt(via: str, spoken: bool) -> str:
    """The instructions for one turn: who Douglas is, what he has been told
    - instruction files, facts, the owner's writing voice - then the time and
    where the message came from, the only facts about the present he has,
    and how the reply will be delivered. The parts that change every minute
    come last, so the model server can reuse its cache of everything before."""
    parts = [SYSTEM_PROMPT]
    extra = read_instructions()
    if extra:
        parts.append(extra)
    if FACTS:
        parts.append("You have been told these things are true: " + FACTS)
    tone = read_tone()
    if tone:
        parts.append(
            f"How {OWNER} writes. These are samples of {OWNER}'s own messages and "
            f"posts, from the tone profile on {OWNER}'s MCP server. Talk to {OWNER} "
            "in the same plain, direct register, and when asked to draft something "
            f"in {OWNER}'s voice, write the way these read. Never quote them back "
            "or mention the people in them unless asked.\n\n" + tone)
    parts.append(time.strftime("It is %A %d %B %Y, %H:%M local time. ")
                 + "This message was sent from "
                 + SOURCES.get(via, "an unknown device")
                 + ". That device is the only thing you know about where the "
                   "person is right now.")
    parts.append(STYLE[bool(spoken)])
    return "\n\n".join(parts)


def no_dashes(text: str) -> str:
    """Em and en dashes out, whatever the model was told: a range of numbers
    keeps a hyphen, anything else becomes a comma. Asking a model not to use
    them works most of the time; this works every time."""
    text = re.sub(r"(?<=\d)\s*[\u2013\u2014]\s*(?=\d)", "-", text)
    return re.sub(r"\s*[\u2013\u2014]\s*", ", ", text)


def readable(text: str) -> str:
    """What the model wrote, for a screen: reasoning blocks removed, the
    words and line breaks kept."""
    text = THINK.sub("", text)
    text = re.sub(r"<think>.*", "", text, flags=re.S | re.I)
    return no_dashes(text).strip()


def llm(messages: list[dict]) -> str:
    body = json.dumps({"model": LLM_MODEL, "messages": messages,
                       "max_tokens": LLM_MAX_TOKENS, "stream": False}).encode()
    req = urllib.request.Request(f"{LLM_URL}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as r:
        out = json.loads(r.read())
    return str(out["choices"][0]["message"].get("content") or "")


def tts(text: str) -> tuple[bytes, tuple[int, int, int]]:
    """Piper over Wyoming: one synthesize, PCM back."""
    data: dict = {"text": text}
    if TTS_VOICE:
        data["voice"] = {"name": TTS_VOICE}
    pcm, fmt = bytearray(), (22050, 2, 1)
    with socket.create_connection((TTS_HOST, TTS_PORT), timeout=60) as s:
        write_event(s, "synthesize", data)
        f = s.makefile("rb")
        while (ev := read_event(f)) is not None:
            etype, d, payload = ev
            if etype == "audio-start":
                fmt = fmt_of(d)
            elif etype == "audio-chunk" and payload:
                fmt = fmt_of(d)
                pcm += payload
            elif etype == "audio-stop":
                return bytes(pcm), fmt
            elif etype == "error":
                raise RuntimeError(f"tts: {d.get('text')}")
    raise RuntimeError("tts closed before audio-stop")


def convert(pcm: bytes, src: tuple[int, int, int], dst: tuple[int, int, int]) -> bytes:
    """16-bit PCM to the satellite's speaker format: channel mix and linear
    resampling. The satellite plays raw chunks straight into aplay at a fixed
    rate, so a mismatch would play Douglas at the wrong pitch."""
    if src == dst or src[1] != 2 or dst[1] != 2:
        return pcm
    s = array.array("h")
    s.frombytes(pcm[: len(pcm) // 2 * 2])
    if src[2] > 1:                                      # → mono
        s = array.array("h", (sum(s[i:i + src[2]]) // src[2]
                              for i in range(0, len(s) - src[2] + 1, src[2])))
    if src[0] != dst[0] and len(s) > 1:
        n = int(len(s) * dst[0] / src[0])
        step = (len(s) - 1) / max(n - 1, 1)
        out = array.array("h", bytes(2 * n))
        for i in range(n):
            x = i * step
            j = int(x)
            k = min(j + 1, len(s) - 1)
            out[i] = int(s[j] + (s[k] - s[j]) * (x - j))
        s = out
    if dst[2] > 1:                                      # mono → N
        s = array.array("h", (v for v in s for _ in range(dst[2])))
    return s.tobytes()


# ── the satellite connection ─────────────────────────────────────────────

class Satellite:
    """One Wyoming connection to DougVoice, reconnected forever."""

    def __init__(self, host: str, port: int, on_event, on_link) -> None:
        self.host, self.port = host, port
        self.on_event, self.on_link = on_event, on_link
        self.snd_format = (22050, 2, 1)
        self._sock: socket.socket | None = None
        self._wlock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def send(self, etype: str, data: dict | None = None, payload: bytes | None = None) -> bool:
        with self._wlock:
            if self._sock is None:
                return False
            try:
                write_event(self._sock, etype, data, payload)
                return True
            except OSError:
                return False

    def run_forever(self) -> None:
        while True:
            try:
                s = socket.create_connection((self.host, self.port), timeout=5)
                s.settimeout(None)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                time.sleep(5)
                continue
            with self._wlock:
                self._sock = s
            log(f"satellite connected: {self.host}:{self.port}")
            self.on_link(True)
            try:
                self.send("describe")
                self.send("run-satellite")
                f = s.makefile("rb")
                while (ev := read_event(f)) is not None:
                    etype, data, payload = ev
                    if etype == "ping":
                        self.send("pong", {"text": data.get("text")})
                    elif etype == "run-pipeline" and data.get("snd_format"):
                        self.snd_format = fmt_of(data["snd_format"])
                    else:
                        self.on_event(etype, data, payload)
            except (OSError, ValueError) as e:
                log(f"satellite link: {e}")
            with self._wlock:
                self._sock = None
            try:
                s.close()
            except OSError:
                pass
            log("satellite disconnected")
            self.on_link(False)
            time.sleep(2)


# ── the pipeline ─────────────────────────────────────────────────────────

class Pipeline:
    """idle → listening → thinking → speaking → idle, driven by PTT.

    stt/llm/tts/send are injectable so the state machine can be exercised
    without a satellite, a GPU or a speaker."""

    def __init__(self, state: StateFile, transcript: Transcript, *, send,
                 snd_format=lambda: (22050, 2, 1), stt=stt, llm=llm, tts=tts,
                 linked=lambda: True, speaker=None) -> None:
        self.st, self.tr = state, transcript
        self.send, self.snd_format, self.linked = send, snd_format, linked
        self.speaker = speaker          # real speakers elsewhere, when they are up
        self._remote = False            # an utterance is playing there now
        self.stt, self.llm, self.tts = stt, llm, tts
        self.history: list[dict] = []
        self.last_exchange = 0.0
        self._lock = threading.Lock()
        self._audio = bytearray()
        self._fmt = (16000, 2, 1)
        self._via = "voice"
        self._listen_started = 0.0
        self._turn = 0                  # bumped by cancel/start; stale turns stop
        self._played = threading.Event()

    @property
    def state(self) -> str:
        return self.st.state

    # -- inputs ------------------------------------------------------------

    def ptt(self, action: str, via: str = "voice") -> tuple[bool, str]:
        via = via if via in VIAS else "voice"
        with self._lock:
            if action == "start":
                if not self.linked():
                    return False, "DougVoice is not connected"
                if self.state == "thinking":
                    return False, "Douglas is still thinking"
                self._turn += 1                      # barge in over speech
                self._played.set()
                self._hush()
                self._audio = bytearray()
                self._via = via
                self._listen_started = time.time()
                self.st.update(state="listening")
                self.send("voice-started", {"timestamp": 0})
                return True, "listening"
            if action == "stop":
                if self.state != "listening":
                    return False, f"not listening ({self.state})"
                return True, self._finish_listening()
            if action == "cancel":
                self._turn += 1
                self._played.set()
                self._hush()
                self._audio = bytearray()
                self.st.update(state="idle")
                return True, "cancelled"
        return False, "action must be start, stop or cancel"

    def say(self, text: str, via: str = "deck", speak: bool = False) -> tuple[bool, str]:
        """A typed turn. Answered on its own thread so the request that
        carried it returns at once; the reply arrives in the transcript."""
        text = " ".join(str(text or "").split())[:MAX_SAY_CHARS]
        if not text:
            return False, "nothing to say"
        via = via if via in VIAS else "deck"
        with self._lock:
            if self.state in ("listening", "thinking"):
                return False, f"Douglas is busy ({self.state})"
            self._turn += 1                          # a typed turn ends any speech
            self._played.set()
            self._hush()
            turn = self._turn
            self.st.update(state="thinking")
        threading.Thread(target=self.run_text, args=(text, via, turn,
                         speak and self.linked()), daemon=True).start()
        return True, "thinking"

    def audio(self, data: dict, payload: bytes | None) -> None:
        """A microphone chunk from the satellite. Kept only while listening."""
        if not payload:
            return
        with self._lock:
            if self.state != "listening":
                return
            self._fmt = fmt_of(data)
            self._audio += payload
            if time.time() - self._listen_started >= MAX_LISTEN_S:
                self._finish_listening()

    def satellite_event(self, etype: str, data: dict, payload: bytes | None) -> None:
        if etype == "audio-chunk":
            self.audio(data, payload)
        elif etype == "played":
            self._played.set()

    def link(self, up: bool) -> None:
        self.st.update(satellite=up)
        if not up:
            with self._lock:
                if self.state == "listening":
                    self._turn += 1
                    self.st.update(state="idle")

    # -- the turn ----------------------------------------------------------

    def _finish_listening(self) -> str:
        """Called with the lock held."""
        audio, fmt, via, turn = bytes(self._audio), self._fmt, self._via, self._turn
        self._audio = bytearray()
        self.send("voice-stopped", {"timestamp": 0})
        rate, width, channels = fmt
        if len(audio) < MIN_LISTEN_S * rate * width * channels:
            self.st.update(state="idle")
            return "too short"
        self.st.update(state="thinking")
        threading.Thread(target=self.run_turn, args=(audio, fmt, via, turn),
                         daemon=True).start()
        return "thinking"

    def _current(self, turn: int) -> bool:
        return turn == self._turn

    def run_turn(self, audio: bytes, fmt, via: str, turn: int) -> None:
        try:
            self._run_turn(audio, fmt, via, turn)
        except Exception as e:                   # noqa: BLE001 — never wedge
            log(f"turn failed: {e}")
            self.send("error", {"text": str(e)[:200]})
        finally:
            with self._lock:
                if self._current(turn) and self.state != "listening":
                    self.st.update(state="idle")

    def _run_turn(self, audio: bytes, fmt, via: str, turn: int) -> None:
        heard = self.stt(audio, fmt)
        self.send("transcript", {"text": heard})
        if not heard or not self._current(turn):
            return
        reply = self._exchange(heard, via, turn, speakable, True)
        if reply is not None:
            self._speak(reply, turn)

    def run_text(self, text: str, via: str, turn: int, speak: bool) -> None:
        try:
            reply = self._exchange(text, via, turn, readable, speak)
            if reply is not None and speak:
                self._speak(speakable(reply), turn)
        except Exception as e:                   # noqa: BLE001 — never wedge
            log(f"typed turn failed: {e}")
            self.tr.append("douglas", f"(I could not answer: {str(e)[:120]})", via)
        finally:
            with self._lock:
                if self._current(turn) and self.state != "listening":
                    self.st.update(state="idle")

    def _exchange(self, said: str, via: str, turn: int, clean, spoken: bool) -> str | None:
        """One user line in, one Douglas line out, both in the transcript and
        the shared memory. None when a newer turn has superseded this one."""
        self.tr.append("user", said, via)
        if time.time() - self.last_exchange > HISTORY_IDLE_S:
            self.history = []
        msgs = ([{"role": "system", "content": system_prompt(via, spoken)}] + self.history
                + [{"role": "user", "content": said}])
        reply = clean(self.llm(msgs))
        if not self._current(turn):
            return None
        if not reply:
            reply = "Sorry, I lost my train of thought."
        self.history = (self.history + [{"role": "user", "content": said},
                                        {"role": "assistant", "content": reply}]
                        )[-2 * HISTORY_TURNS:]
        self.last_exchange = time.time()
        self.tr.append("douglas", reply, via)
        self.st.update(last_turn=time.time())
        return reply

    def _hush(self) -> None:
        """Cut short an utterance playing on the remote speaker. Called with
        the lock held, so the request goes out on its own thread."""
        if self._remote and self.speaker is not None:
            threading.Thread(target=self.speaker.stop, daemon=True).start()

    def _speak(self, reply: str, turn: int) -> None:
        pcm, src = self.tts(reply)
        with self._lock:
            if not self._current(turn):
                return
            self.st.update(state="speaking")
            self._played.clear()
        if self.speaker is not None and self.speaker.ready():
            self._remote = True
            try:
                played = self.speaker.play(pcm, src)
            finally:
                self._remote = False
            if played or not self._current(turn):
                return                  # heard there, or cut short on purpose
            log("the remote speaker did not play it - using the satellite")
        dst = self.snd_format()
        pcm = convert(pcm, src, dst)
        a = {"rate": dst[0], "width": dst[1], "channels": dst[2]}
        self.send("audio-start", a)
        step = dst[0] * dst[1] * dst[2] // 10
        for i in range(0, len(pcm), step):
            if not self._current(turn):
                break
            self.send("audio-chunk", a, pcm[i:i + step])
        self.send("audio-stop", {"timestamp": 0})
        seconds = len(pcm) / max(dst[0] * dst[1] * dst[2], 1)
        self._played.wait(timeout=seconds + 3)   # the satellite says played


# ── http ─────────────────────────────────────────────────────────────────

PIPE: Pipeline | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "douglas-voice/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, obj) -> None:
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if isinstance(obj, bytes)
                         else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/health":
            return self._send(200, b"ok")
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path not in ("/ptt", "/say"):
            return self._send(404, {"error": "not found"})
        if PTT_TOKEN and (parse_qs(u.query).get("token") or [""])[0] != PTT_TOKEN:
            return self._send(403, {"error": "forbidden"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 16384)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._send(400, {"ok": False, "message": "JSON object expected"})
        assert PIPE is not None
        if u.path == "/say":
            ok, msg = PIPE.say(str(body.get("text") or ""),
                               str(body.get("via") or "deck"), bool(body.get("speak")))
            return self._send(200 if ok else 409,
                              {"ok": ok, "message": msg, "state": PIPE.state})
        action = str(body.get("action") or "")
        if action not in ("start", "stop", "cancel"):
            return self._send(400, {"ok": False,
                                    "message": "action must be start, stop or cancel"})
        assert PIPE is not None
        ok, msg = PIPE.ptt(action, str(body.get("via") or "voice"))
        return self._send(200 if ok else 409,
                          {"ok": ok, "message": msg, "state": PIPE.state})


def main() -> None:
    global PIPE
    state = StateFile(VOICE_STATE_FILE)
    sat: Satellite | None = None
    PIPE = Pipeline(state, Transcript(TRANSCRIPT_FILE),
                    send=lambda *a: sat.send(*a) if sat else False,
                    snd_format=lambda: sat.snd_format if sat else (22050, 2, 1),
                    linked=lambda: bool(sat and sat.connected),
                    speaker=Speaker(SPEAKER_URL) if SPEAKER_URL else None)
    sat = Satellite(SAT_HOST, SAT_PORT, PIPE.satellite_event, PIPE.link)
    state.heartbeat()

    def beat() -> None:
        while True:
            time.sleep(HEARTBEAT_S)
            state.heartbeat()

    threading.Thread(target=beat, daemon=True).start()
    if MCP_URL and MCP_TOKEN:
        threading.Thread(target=tone_loop, daemon=True).start()
    else:
        threading.Thread(target=warm, daemon=True).start()
    threading.Thread(target=sat.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer((PTT_HOST, PTT_PORT), Handler)
    log(f"PTT on {PTT_HOST}:{PTT_PORT}, satellite {SAT_HOST}:{SAT_PORT}, "
        f"stt :{STT_PORT}, tts :{TTS_PORT}, model {LLM_URL}, remote speaker "
        + (urllib.parse.urlsplit(SPEAKER_URL).netloc if SPEAKER_URL else "none"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
