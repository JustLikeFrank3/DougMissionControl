#!/usr/bin/env python3
"""douglas-tts - Douglas's speaking voice: Kokoro behind the Wyoming protocol.

Piper, the voice he started with, is quick and clear and sounds like a machine
reading. Kokoro is a small neural voice (82M parameters) that sounds like a
person and still runs on the CPU, about ten times faster than real time on
this box, so it needs none of the graphics memory the language models fill.

It answers the same Wyoming `synthesize` the voice pipeline already sends to
Piper, so the pipeline needs a different port and nothing else: text in,
16-bit mono PCM out (audio-start, audio-chunk ..., audio-stop). `describe` is
answered with `info`, so any other Wyoming client can list the voices.

One utterance at a time: Douglas has one mouth, and the model is not something
to share between threads. What he says is nobody's business in a journal, so
the log has sizes and timings, never the words.

Runs from its own virtualenv (/opt/kokoro: kokoro-onnx, which brings
onnxruntime), as the service user, with the two model files beside each other
in /srv/models/kokoro. Nothing here leaves the machine.

Environment (/etc/douglas/tts.env):
  TTS_BIND, TTS_LISTEN_PORT     where to listen                   (127.0.0.1, 10210)
  KOKORO_MODEL, KOKORO_VOICES   the model and its voices file
  KOKORO_VOICE                  the voice when a request names none, or names
                                one this model does not have       (bm_lewis)
  KOKORO_SPEED                  pace; 1.0 is the voice's own       (1.0)
"""
import json
import os
import socketserver
import sys
import threading
import time

# onnxruntime, which kokoro-onnx runs the model on, uploads usage telemetry to
# Microsoft from Linux too unless this is set before it loads. The unit sets
# it and blocks the network besides; this is for a run by hand.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

E = os.environ.get
BIND, PORT = E("TTS_BIND", "127.0.0.1"), int(E("TTS_LISTEN_PORT", "10210"))
MODEL = E("KOKORO_MODEL", "/srv/models/kokoro/kokoro-v1.0.onnx")
VOICES = E("KOKORO_VOICES", "/srv/models/kokoro/voices-v1.0.bin")
VOICE = E("KOKORO_VOICE", "bm_lewis")
SPEED = min(2.0, max(0.5, float(E("KOKORO_SPEED", "1.0"))))
CHUNK = 8192                    # bytes of PCM per audio-chunk: about a sixth of a second
MAX_CHARS = 4000                # a reply, not a novel

# A Kokoro voice name starts with its language: a for American English, b for
# British, and so on. The second letter is f or m.
LANGUAGES = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
             "i": "it", "j": "ja", "p": "pt-br", "z": "cmn"}


def log(msg: str) -> None:
    print(f"douglas-tts: {msg}", file=sys.stderr, flush=True)


def language_of(voice: str) -> str:
    return LANGUAGES.get(voice[:1], "en-us")


# ── the Wyoming wire format ──────────────────────────────────────────────
# One JSON line, then optionally a JSON body and a binary payload whose
# lengths the line gives. The same few lines the pipeline has on its side.

def write_event(f, etype: str, data: dict | None = None, payload: bytes | None = None) -> None:
    head: dict = {"type": etype, "version": "1.5.0"}
    body = b""
    if data:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        head["data_length"] = len(body)
    if payload:
        head["payload_length"] = len(payload)
    f.write(json.dumps(head).encode() + b"\n" + body + (payload or b""))
    f.flush()


def read_event(f) -> tuple[str, dict, bytes | None] | None:
    """One event from a buffered reader, or None at the end or on nonsense."""
    line = f.readline(65536)
    if not line:
        return None
    try:
        head = json.loads(line)
        data = dict(head.get("data") or {})
        n = int(head.get("data_length") or 0)
        if n:
            raw = f.read(n)
            if len(raw) < n:
                return None
            data.update(json.loads(raw))
        payload = None
        n = int(head.get("payload_length") or 0)
        if n:
            payload = f.read(n)
            if len(payload) < n:
                return None
        return str(head.get("type")), data, payload
    except (ValueError, TypeError, AttributeError):
        return None


# ── the voice ────────────────────────────────────────────────────────────

class Kokoro:
    """The model, loaded once. Imported here rather than at the top so the
    protocol can be tested on a machine that has neither it nor the model."""

    def __init__(self, model: str, voices: str):
        import numpy
        from kokoro_onnx import Kokoro as Model
        self.np = numpy
        began = time.time()
        self.model = Model(model, voices)
        self.names = sorted(self.model.get_voices())
        log(f"model loaded in {time.time() - began:.1f} s, {len(self.names)} voices")

    def say(self, text: str, voice: str, speed: float) -> tuple[bytes, int]:
        """16-bit little-endian mono PCM and its sample rate."""
        samples, rate = self.model.create(text, voice=voice, speed=speed,
                                          lang=language_of(voice))
        pcm = (self.np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        return pcm.tobytes(), int(rate)


def info(names: list[str]) -> dict:
    """What `describe` is answered with: one program, its voices."""
    credit = {"name": "hexgrad", "url": "https://huggingface.co/hexgrad/Kokoro-82M"}
    return {"tts": [{
        "name": "kokoro", "description": "Kokoro, a small neural voice",
        "attribution": credit, "installed": True, "version": "1.0",
        "voices": [{"name": n, "description": n, "attribution": credit, "installed": True,
                    "version": None, "languages": [language_of(n)]} for n in names],
    }], "asr": [], "handle": [], "intent": [], "wake": [], "mic": [], "snd": []}


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, engine, default: str = VOICE, speed: float = SPEED):
        super().__init__(addr, Handler)
        self.engine, self.default, self.speed = engine, default, speed
        self.mouth = threading.Lock()
        if default not in engine.names:
            raise SystemExit(f"douglas-tts: the model has no voice called {default!r}")

    def voice_for(self, wanted: str) -> str:
        """The voice a request asked for, or Douglas's own if it asked for
        none or for one this model lacks. A stray name in a config file should
        change his voice back, not take it away."""
        if wanted and wanted not in self.engine.names:
            log(f"no voice called {wanted[:40]!r} here - using {self.default}")
        return wanted if wanted in self.engine.names else self.default


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            while (ev := read_event(self.rfile)) is not None:
                etype, data, _ = ev
                if etype == "describe":
                    write_event(self.wfile, "info", info(self.server.engine.names))
                elif etype == "synthesize":
                    self.speak(data)
        except OSError:
            pass                        # the caller hung up: nothing to tell it

    def speak(self, data: dict) -> None:
        srv = self.server
        text = " ".join(str(data.get("text") or "").split())[:MAX_CHARS]
        named = data.get("voice")
        voice = srv.voice_for(str((named or {}).get("name") or "") if isinstance(named, dict) else "")
        pcm, rate = b"", 24000
        if text:
            began = time.time()
            try:
                with srv.mouth:
                    waited = time.time() - began
                    pcm, rate = srv.engine.say(text, voice, srv.speed)
            except Exception as e:  # noqa: BLE001 - whatever it was, say so and stay up
                log(f"could not say {len(text)} chars as {voice}: {type(e).__name__}: {e}")
                write_event(self.wfile, "error", {"text": f"{type(e).__name__}: {e}", "code": "tts"})
                return
            took = time.time() - began - waited
            log(f"{len(text)} chars as {voice}: {len(pcm) / (2 * rate):.1f} s of speech "
                f"in {took:.2f} s" + (f", after waiting {waited:.1f} s" if waited > 0.2 else ""))
        fmt = {"rate": rate, "width": 2, "channels": 1}
        write_event(self.wfile, "audio-start", fmt)
        for i in range(0, len(pcm), CHUNK):
            write_event(self.wfile, "audio-chunk", fmt, pcm[i:i + CHUNK])
        write_event(self.wfile, "audio-stop")


def main() -> None:
    engine = Kokoro(MODEL, VOICES)
    server = Server((BIND, PORT), engine)
    engine.say("Ready.", server.default, server.speed)      # the first call pays for warm-up
    log(f"listening on {BIND}:{PORT}, voice {server.default}, speed {server.speed:g}")
    server.serve_forever()


if __name__ == "__main__":
    main()
