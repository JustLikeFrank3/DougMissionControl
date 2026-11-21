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
    "All you know about the present is what this message states and what "
    "has been said in this conversation. You have no eyes, sensors, "
    "calendar or internet, you cannot see or control any device, and you "
    "remember nothing from before this conversation. Never invent facts "
    "about the house, the people or animals in it, where anyone is, or "
    "what is happening. If you do not know, say so in a few words and, "
    "when it helps, say what you would need in order to know. "
    "Talk like a capable colleague with a dry sense of humour, not a help "
    "desk: no apologies, no flattery, no offers of further help, and do "
    "not repeat the question back."))
# True things about this household, in the owner's words (one line, in
# /etc/douglas/voice.env). They are the only local knowledge Douglas has.
FACTS = E("DOUGLAS_FACTS", "")
STYLE = {
    True: ("Your reply will be read aloud through a small speaker: one to "
           "three short spoken sentences, with no markdown, lists, code or emoji."),
    False: ("Your reply will be read on a screen: plain text, a few short "
            "sentences unless more is asked for, with no markdown or emoji."),
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
    return " ".join(text.split())


def system_prompt(via: str, spoken: bool) -> str:
    """The instructions for one turn: who Douglas is, what he has been told
    is true, the time and where the message came from - the only facts about
    the present he has - and how the reply will be delivered."""
    parts = [SYSTEM_PROMPT]
    if FACTS:
        parts.append("You have been told these things are true: " + FACTS)
    parts.append(time.strftime("It is %A %d %B %Y, %H:%M local time. ")
                 + "This message was sent from "
                 + SOURCES.get(via, "an unknown device") + ".")
    parts.append(STYLE[bool(spoken)])
    return "\n\n".join(parts)


def readable(text: str) -> str:
    """What the model wrote, for a screen: reasoning blocks removed, the
    words and line breaks kept."""
    text = THINK.sub("", text)
    text = re.sub(r"<think>.*", "", text, flags=re.S | re.I)
    return text.strip()


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
                 linked=lambda: True) -> None:
        self.st, self.tr = state, transcript
        self.send, self.snd_format, self.linked = send, snd_format, linked
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

    def _speak(self, reply: str, turn: int) -> None:
        pcm, src = self.tts(reply)
        with self._lock:
            if not self._current(turn):
                return
            self.st.update(state="speaking")
            self._played.clear()
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
                    linked=lambda: bool(sat and sat.connected))
    sat = Satellite(SAT_HOST, SAT_PORT, PIPE.satellite_event, PIPE.link)
    state.heartbeat()

    def beat() -> None:
        while True:
            time.sleep(HEARTBEAT_S)
            state.heartbeat()

    threading.Thread(target=beat, daemon=True).start()
    threading.Thread(target=sat.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer((PTT_HOST, PTT_PORT), Handler)
    log(f"PTT on {PTT_HOST}:{PTT_PORT}, satellite {SAT_HOST}:{SAT_PORT}, "
        f"stt :{STT_PORT}, tts :{TTS_PORT}, model {LLM_URL}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
