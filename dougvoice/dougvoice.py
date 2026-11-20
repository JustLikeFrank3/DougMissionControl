#!/usr/bin/env python3
"""dougvoice — the Pi Zero 2 W + PiSugar Whisplay HAT that is Douglas's ears.

Runs ON DOUGVOICE, installed by dougvoice/setup.sh. System python3 only (the
satellite itself lives in a venv). Three subcommands, one per systemd unit:

    dougvoice.py satellite   exec wyoming-satellite on :10700, argv from the env
    dougvoice.py wake        exec wyoming-openwakeword (only with WAKE_WORD set)
    dougvoice.py face        the LCD, the RGB LED and the button

Nothing heavy runs here. The satellite moves raw audio: mic → Douglas for
speech-to-text, Douglas's text-to-speech → speaker. The LLM, STT and TTS all
run on Douglas, which connects *to* the satellite (Wyoming's direction).

The face shows one of five states — offline, idle, listening, thinking,
speaking — from three sources, newest wins:
  * the satellite's own events (--event-uri → 127.0.0.1:10500 here),
  * Douglas's voice state, polled from douglas-agent's /status, and
  * the button, which says "listening" the instant it is pressed.

The button is push-to-talk. With no wake word (LISTEN_MODE stream, the
default) the satellite streams the mic to Douglas whenever Douglas is
connected and the button goes to douglas-agent's /ptt, exactly like the
deck's on-screen button — Douglas's pipeline does the gating. With a local
wake word (WAKE_WORD=ok_nabu), the satellite only streams after a detection,
so the face also serves the satellite's wake endpoint: it proxies the real
openWakeWord and injects a detection of its own when the button is pressed.

Everything that decides anything is a plain function or a small class below
the hardware line, so test_dougvoice.py runs it on any machine.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shlex
import sys
import time
import urllib.error
import urllib.request

STATES = ("offline", "idle", "listening", "thinking", "speaking")

LCD_W, LCD_H = 240, 280            # the Whisplay's 1.69" ST7789, portrait

# A local hint (button, satellite event) that Douglas never confirms falls
# back to idle after this long, so a lost "played" cannot pin the face.
HINT_TTL_S = {"listening": 30.0, "thinking": 45.0, "speaking": 90.0}
DOUGLAS_TIMEOUT_S = 10.0           # no good poll for this long = offline

# Satellite → face events (wyoming-satellite --event-uri). streaming-start is
# deliberately absent: in always-streaming mode it fires once on connect,
# which is not the user talking.
EVENT_STATES = {
    "detection": "listening",
    "voice-started": "listening",
    "voice-stopped": "thinking",
    "transcript": "thinking",
    "synthesize": "speaking",
    "tts-start": "speaking",
    "played": "idle",
    "tts-played": "idle",
    "error": "idle",
}
EVENT_CONNECTION = {
    "satellite-connected": True,
    "run-satellite": True,
    "satellite-disconnected": False,
}


# ── configuration → argv ────────────────────────────────────────────────

def _env(env: dict, key: str, default: str) -> str:
    v = env.get(key)
    return v if v not in (None, "") else default


def listen_mode(env: dict) -> str:
    return "wake" if _env(env, "WAKE_WORD", "") else "stream"


def satellite_argv(env: dict) -> list[str]:
    """wyoming-satellite's command line. The WM8960 captures stereo at 48 kHz;
    plughw lets ALSA hand the satellite the 16 kHz mono it wants."""
    dev = _env(env, "AUDIO_DEVICE", "plughw:CARD=whisplaysound,DEV=0")
    argv = [_env(env, "SATELLITE_PYTHON", "/opt/dougvoice/venv/bin/python3"),
            "-m", "wyoming_satellite",
            "--name", _env(env, "SATELLITE_NAME", "DougVoice"),
            "--uri", f"tcp://0.0.0.0:{_env(env, 'SATELLITE_PORT', '10700')}",
            "--mic-command", f"arecord -D {dev} -r 16000 -c 1 -f S16_LE -t raw",
            "--snd-command", f"aplay -D {dev} -r 22050 -c 1 -f S16_LE -t raw",
            "--snd-command-rate", "22050",
            "--event-uri", f"tcp://127.0.0.1:{_env(env, 'EVENT_PORT', '10500')}"]
    if listen_mode(env) == "wake":
        argv += ["--wake-uri", f"tcp://127.0.0.1:{_env(env, 'WAKE_SHIM_PORT', '10400')}",
                 "--wake-word-name", env["WAKE_WORD"]]
    for key, flag in (("MIC_VOLUME", "--mic-volume-multiplier"),
                      ("SND_VOLUME", "--snd-volume-multiplier")):
        if env.get(key):
            argv += [flag, env[key]]
    return argv + shlex.split(env.get("SATELLITE_EXTRA_ARGS", ""))


def wake_argv(env: dict) -> list[str]:
    """openWakeWord, bound to loopback behind the face's wake shim."""
    return [_env(env, "WAKE_PYTHON", "/opt/dougvoice/wake-venv/bin/python3"),
            "-m", "wyoming_openwakeword",
            "--uri", f"tcp://127.0.0.1:{_env(env, 'WAKE_PORT', '10401')}",
            "--preload-model", _env(env, "WAKE_WORD", "ok_nabu")]


def douglas_url(env: dict, path: str) -> str:
    host = _env(env, "DOUGLAS_HOST", "douglas")
    port = _env(env, "DOUGLAS_AGENT_PORT", "9111")
    return f"http://{host}:{port}{path}?token={env.get('DOUGLAS_AGENT_TOKEN', '')}"


# ── state ────────────────────────────────────────────────────────────────

class StateTracker:
    """Which of STATES the face shows. Newest source wins; offline trumps all."""

    def __init__(self) -> None:
        self.local: tuple[str, float] | None = None       # (state, since)
        self.remote: tuple[str, float] | None = None      # Douglas, (state, changed)
        self.last_ok: float | None = None                 # last good poll
        self.polling = False                              # have we a token at all
        self.connected: bool | None = None                # satellite ↔ Douglas

    def hint(self, state: str, now: float) -> None:
        if state in STATES:
            self.local = (state, now)

    def event(self, etype: str, now: float) -> None:
        if etype in EVENT_CONNECTION:
            self.connected = EVENT_CONNECTION[etype]
            if self.connected is False:
                self.local = None
        elif etype in EVENT_STATES:
            self.hint(EVENT_STATES[etype], now)

    def douglas(self, voice: dict | None, now: float) -> None:
        """One poll result: douglas-agent's "voice" object, or None when the
        poll failed. A state only gets a fresh timestamp when it changes, so
        a stale "idle" from Douglas never overrules a button press."""
        self.polling = True
        if voice is None:
            return
        self.last_ok = now
        state = str(voice.get("state") or "absent")
        if self.remote is None or self.remote[0] != state:
            self.remote = (state, now)
        if "satellite" in voice:
            self.connected = bool(voice["satellite"]) if state != "absent" else self.connected

    def current(self, now: float) -> str:
        if self.polling:
            if self.last_ok is None or now - self.last_ok > DOUGLAS_TIMEOUT_S:
                return "offline"
            if self.remote and self.remote[0] == "absent":
                return "offline"
        if self.connected is False:
            return "offline"
        remote = self.remote if self.remote and self.remote[0] in STATES else None
        local = self.local
        if local and local[0] != "idle" and now - local[1] > HINT_TTL_S.get(local[0], 0):
            local = None                         # expired: Douglas, else idle
        pick = max((s for s in (local, remote) if s), key=lambda s: s[1], default=None)
        return pick[0] if pick else "idle"


class Button:
    """Push-to-talk from one button. Returns what the press or release means:
    "start"/"stop"/"cancel" for Douglas's /ptt, "wake" for the wake shim,
    None for nothing. A tap shorter than MIN_HOLD is a cancel, not a turn —
    an accidental brush should not send Douglas half a syllable."""

    MIN_HOLD_S = 0.25

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._held: tuple[str, float] | None = None

    def press(self, state: str, now: float) -> str | None:
        self._held = None
        if state == "offline":
            return None
        if self.mode == "wake":
            return "wake"
        if state in ("thinking", "speaking"):
            self._held = ("cancel", now)
            return "cancel"
        self._held = ("start", now)
        return "start"

    def release(self, now: float) -> str | None:
        held, self._held = self._held, None
        if not held or held[0] != "start":
            return None
        return "cancel" if now - held[1] < self.MIN_HOLD_S else "stop"


# ── what each state looks like ───────────────────────────────────────────

COLOURS = {                      # LED and the LCD disc
    "offline": (160, 0, 0),
    "idle": (0, 40, 160),
    "listening": (0, 170, 255),
    "thinking": (255, 150, 0),
    "speaking": (0, 210, 90),
}
LED_PEAK = {"offline": 0.15, "idle": 0.08, "listening": 1.0, "thinking": 1.0, "speaking": 0.8}


def led_rgb(state: str, t: float) -> tuple[int, int, int]:
    """The LED at time t. Thinking breathes (1.2 s period) so a long answer
    still looks alive; everything else is steady."""
    level = LED_PEAK.get(state, 0.0)
    if state == "thinking":
        level *= 0.2 + 0.8 * (0.5 + 0.5 * math.sin(2 * math.pi * t / 1.2))
    r, g, b = COLOURS.get(state, (0, 0, 0))
    return (round(r * level), round(g * level), round(b * level))


def rgb565(r: int, g: int, b: int) -> bytes:
    """The ST7789's pixel, high byte first, as the Whisplay driver sends it."""
    v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return bytes((v >> 8, v & 0xFF))


# 5×7 glyphs for the only words the face ever writes.
FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    " ": ("00000",) * 7,
}


def _text(rows: list[bytearray], text: str, y: int, scale: int, px: bytes) -> None:
    width = len(text) * 6 * scale - scale
    x0 = (LCD_W - width) // 2
    for i, ch in enumerate(text):
        glyph = FONT[ch]
        for gy, line in enumerate(glyph):
            for gx, bit in enumerate(line):
                if bit != "1":
                    continue
                x = x0 + (i * 6 + gx) * scale
                for dy in range(scale):
                    rows[y + gy * scale + dy][x * 2:(x + scale) * 2] = px * scale


def render(state: str) -> bytes:
    """One full LCD frame (240×280 RGB565) for a state: the name at the top,
    a disc in the state's colour, the state's word underneath."""
    bg, fg = rgb565(0, 0, 0), rgb565(230, 230, 230)
    disc = rgb565(*COLOURS.get(state, (80, 80, 80)))
    rows = [bytearray(bg * LCD_W) for _ in range(LCD_H)]
    _text(rows, "DOUGVOICE", 18, 2, rgb565(120, 120, 120))
    cx, cy, r = LCD_W // 2, 130, 72
    if state == "listening":
        r = 84                                   # bigger: "I'm all ears"
    for y in range(cy - r, cy + r + 1):
        dx = math.isqrt(r * r - (y - cy) ** 2)
        if state == "offline" and abs(y - cy) < r - 10:   # a ring, not a disc
            inner = math.isqrt((r - 10) ** 2 - (y - cy) ** 2)
            rows[y][(cx - dx) * 2:(cx - inner) * 2] = disc * (dx - inner)
            rows[y][(cx + inner + 1) * 2:(cx + dx + 1) * 2] = disc * (dx - inner)
        else:
            rows[y][(cx - dx) * 2:(cx + dx + 1) * 2] = disc * (2 * dx + 1)
    _text(rows, state.upper(), 236, 3, fg)
    return b"".join(rows)


# ── Wyoming framing ──────────────────────────────────────────────────────
# One JSON header line; then data_length bytes of JSON merged into "data";
# then payload_length bytes of payload (audio).

async def read_event(reader: asyncio.StreamReader) -> dict | None:
    line = await reader.readline()
    if not line:
        return None
    hdr = json.loads(line)
    data = dict(hdr.get("data") or {})
    if hdr.get("data_length"):
        data.update(json.loads(await reader.readexactly(hdr["data_length"])))
    payload = None
    if hdr.get("payload_length"):
        payload = await reader.readexactly(hdr["payload_length"])
    return {"type": hdr.get("type"), "data": data, "payload": payload}


def encode_event(etype: str, data: dict | None = None, payload: bytes | None = None) -> bytes:
    hdr: dict = {"type": etype, "version": "1.0.0"}
    body = json.dumps(data).encode() if data else b""
    if body:
        hdr["data_length"] = len(body)
    if payload:
        hdr["payload_length"] = len(payload)
    return json.dumps(hdr).encode() + b"\n" + body + (payload or b"")


def raw_event(ev: dict) -> bytes:
    return encode_event(ev["type"], ev["data"] or None, ev["payload"])


# ── hardware line: everything below talks to the board or the network ────

class NullBoard:
    """No HAT (or no driver): log instead of draw, so the service still runs
    and the satellite still has its event and wake endpoints."""

    def set_backlight(self, _v): pass
    def set_rgb(self, *_rgb): pass
    def draw_image(self, *_a): pass
    def on_button_press(self, _cb): pass
    def on_button_release(self, _cb): pass


def open_board(env: dict):
    driver = _env(env, "WHISPLAY_DIR", "/opt/dougvoice/Whisplay") + "/runtime"
    sys.path.insert(0, driver)
    try:
        from whisplay import WhisplayBoard   # PiSugar's own driver
        board = WhisplayBoard()
        board.set_backlight(int(_env(env, "BACKLIGHT", "60")))
        return board
    except Exception as e:  # noqa: BLE001 - any failure means "no screen"
        print(f"dougvoice-face: no Whisplay board ({e}); running headless", flush=True)
        return NullBoard()


class Face:
    def __init__(self, env: dict) -> None:
        self.env = env
        self.tracker = StateTracker()
        self.button = Button(listen_mode(env))
        self.board = open_board(env)
        self.shown: str | None = None
        self.led: tuple[int, int, int] | None = None
        self.frames: dict[str, bytes] = {}
        self.wake_clients: set[asyncio.StreamWriter] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    # network ──
    def _http(self, url: str, body: dict | None = None) -> dict | None:
        req = urllib.request.Request(
            url, method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read(1_000_000) or b"{}")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    async def poll_douglas(self) -> None:
        if not self.env.get("DOUGLAS_AGENT_TOKEN"):
            print("dougvoice-face: no DOUGLAS_AGENT_TOKEN - state from the satellite only",
                  flush=True)
            return
        every = float(_env(self.env, "POLL_S", "1"))
        while True:
            reply = await asyncio.to_thread(self._http, douglas_url(self.env, "/status"))
            voice = reply.get("voice") if isinstance(reply, dict) else None
            self.tracker.douglas(voice if isinstance(voice, dict) else None, time.monotonic())
            await asyncio.sleep(every)

    async def ptt(self, action: str) -> None:
        if not self.env.get("DOUGLAS_AGENT_TOKEN"):
            return
        r = await asyncio.to_thread(self._http, douglas_url(self.env, "/ptt"),
                                    {"action": action, "via": "button"})
        if not (r and r.get("ok")):
            print(f"dougvoice-face: ptt {action}: {(r or {}).get('message', 'no answer')}",
                  flush=True)

    # button (called from the driver's polling thread) ──
    def _pressed(self) -> None:
        self.loop.call_soon_threadsafe(self._on_button, True)

    def _released(self) -> None:
        self.loop.call_soon_threadsafe(self._on_button, False)

    def _on_button(self, down: bool) -> None:
        now = time.monotonic()
        what = (self.button.press(self.tracker.current(now), now) if down
                else self.button.release(now))
        if what in ("start", "wake"):
            self.tracker.hint("listening", now)
        elif what == "stop":
            self.tracker.hint("thinking", now)
        elif what == "cancel":
            self.tracker.hint("idle", now)
        if what == "wake":
            self.inject_detection()
        elif what:
            asyncio.ensure_future(self.ptt(what))

    # satellite events ──
    async def serve_events(self, reader, writer) -> None:
        try:
            while (ev := await read_event(reader)) is not None:
                self.tracker.event(ev["type"], time.monotonic())
        except (ValueError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    # wake shim: satellite ⇄ (openWakeWord + the button) ──
    def inject_detection(self) -> None:
        msg = encode_event("detection", {"name": self.env.get("WAKE_WORD"),
                                         "timestamp": int(time.time() * 1000)})
        for w in list(self.wake_clients):
            w.write(msg)

    async def serve_wake(self, sat_r, sat_w) -> None:
        self.wake_clients.add(sat_w)
        up_r = up_w = None
        try:
            up_r, up_w = await asyncio.open_connection(
                "127.0.0.1", int(_env(self.env, "WAKE_PORT", "10401")))
        except OSError:
            print("dougvoice-face: openWakeWord not answering - button only", flush=True)

        async def down() -> None:               # openWakeWord → satellite, whole events
            while up_r and (ev := await read_event(up_r)) is not None:
                sat_w.write(raw_event(ev))      # whole events, so an injected
                await sat_w.drain()             # detection never lands mid-frame

        async def up() -> None:                 # satellite → openWakeWord
            while (ev := await read_event(sat_r)) is not None:
                if ev["type"] == "describe" and not up_w:
                    sat_w.write(encode_event("info", {"wake": [{
                        "name": "dougvoice-button", "installed": True, "attribution":
                        {"name": "", "url": ""}, "models": []}]}))
                elif up_w:
                    up_w.write(raw_event(ev))
                    await up_w.drain()
        try:
            await asyncio.gather(up(), down())
        except (ValueError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.wake_clients.discard(sat_w)
            for w in (sat_w, up_w):
                if w:
                    w.close()

    # drawing ──
    async def draw_loop(self) -> None:
        while True:
            now = time.monotonic()
            state = self.tracker.current(now)
            if state != self.shown:
                frame = self.frames.get(state) or self.frames.setdefault(state, render(state))
                await asyncio.to_thread(self.board.draw_image, 0, 0, LCD_W, LCD_H, frame)
                self.shown = state
            rgb = led_rgb(state, now)
            if rgb != self.led:                  # the driver's PWM restarts on every set
                self.board.set_rgb(*rgb)
                self.led = rgb
            await asyncio.sleep(1 / 15)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.board.on_button_press(self._pressed)
        self.board.on_button_release(self._released)
        servers = [await asyncio.start_server(
            self.serve_events, "127.0.0.1", int(_env(self.env, "EVENT_PORT", "10500")))]
        if listen_mode(self.env) == "wake":
            servers.append(await asyncio.start_server(
                self.serve_wake, "127.0.0.1", int(_env(self.env, "WAKE_SHIM_PORT", "10400"))))
        print(f"dougvoice-face: up, mode {listen_mode(self.env)}", flush=True)
        await asyncio.gather(self.draw_loop(), self.poll_douglas(),
                             *(s.serve_forever() for s in servers))


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ)
    if cmd == "satellite":
        args = satellite_argv(env)
    elif cmd == "wake":
        args = wake_argv(env)
    elif cmd == "face":
        asyncio.run(Face(env).run())
        return
    else:
        sys.exit("usage: dougvoice.py satellite|wake|face")
    print("exec", " ".join(shlex.quote(a) for a in args), flush=True)
    os.execv(args[0], args)


if __name__ == "__main__":
    main(sys.argv)
