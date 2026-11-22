#!/usr/bin/env python3
"""douglas-tts against a stand-in for the Kokoro model. Runs anywhere: it needs
neither kokoro-onnx nor the model files, only the protocol around them."""
import importlib.util
import io
import pathlib
import socket
import sys
import threading
import time

here = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tts", here / "douglas-tts.py")
tts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tts)

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(label)


class Model:
    """What Kokoro looks like from the server's side."""
    names = ["am_michael", "bm_lewis"]

    def __init__(self):
        self.said, self.busy, self.most, self.slow = [], 0, 0, 0.0

    def say(self, text, voice, speed):
        self.busy += 1
        self.most = max(self.most, self.busy)
        time.sleep(self.slow)
        self.busy -= 1
        if text == "break":
            raise RuntimeError("the model fell over")
        self.said.append((text, voice, speed))
        return (voice[:2].encode() * (10000 if text == "long" else len(text))), 24000


model = Model()
server = tts.Server(("127.0.0.1", 0), model, default="bm_lewis", speed=1.0)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


def ask(etype, data=None, raw=None):
    """Every event that comes back for one event sent."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        f = s.makefile("rwb")
        if raw is not None:
            f.write(raw)
            f.flush()
        else:
            tts.write_event(f, etype, data)
        s.shutdown(socket.SHUT_WR)
        out = []
        while (ev := tts.read_event(f)) is not None:
            out.append(ev)
        return out


def heard(events):
    return b"".join(p for t, _, p in events if t == "audio-chunk" and p)


journal, real = io.StringIO(), sys.stderr
sys.stderr = journal

print("-- a reply, spoken --")
ev = ask("synthesize", {"text": "The garden shed is not a tall building."})
check("it starts, plays and stops", [t for t, _, _ in ev], ["audio-start", "audio-chunk", "audio-stop"])
check("as 16-bit mono at the model's rate", ev[0][1], {"rate": 24000, "width": 2, "channels": 1})
check("in his own voice when none is named", model.said[-1][1:], ("bm_lewis", 1.0))
check("with the audio the model made", heard(ev), b"bm" * 39)

print("-- which voice --")
ask("synthesize", {"text": "howdy", "voice": {"name": "am_michael"}})
check("a voice that is named is used", model.said[-1][1], "am_michael")
ask("synthesize", {"text": "hello", "voice": {"name": "en_GB-alan-medium"}})
check("a name this model lacks falls back to his own", model.said[-1][1], "bm_lewis")
ask("synthesize", {"text": "hello", "voice": "bm_lewis"})
check("a voice given as the wrong kind of thing is ignored", model.said[-1][1], "bm_lewis")
check("British voices are read as British", tts.language_of("bm_lewis"), "en-gb")
check("American as American", tts.language_of("am_michael"), "en-us")
check("anything unknown as American", tts.language_of("xx_nobody"), "en-us")

print("-- the shape of what is sent --")
ev = ask("synthesize", {"text": "long"})
chunks = [p for t, _, p in ev if t == "audio-chunk"]
check("a long reply comes in pieces", (len(chunks), max(map(len, chunks)) <= tts.CHUNK), (3, True))
check("that add up to all of it", heard(ev) == b"bm" * 10000, True)
before = len(model.said)
ev = ask("synthesize", {"text": "   \n  "})
check("nothing to say is silence, not an error", ([t for t, _, _ in ev], len(model.said) - before),
      (["audio-start", "audio-stop"], 0))
ask("synthesize", {"text": "  two   words\n"})
check("stray spacing is tidied before it is read", model.said[-1][0], "two words")
ask("synthesize", {"text": "x" * 9000})
check("a wall of text is cut off", len(model.said[-1][0]), tts.MAX_CHARS)

print("-- when it goes wrong --")
ev = ask("synthesize", {"text": "break"})
check("a model failure is an error event", (ev[0][0], ev[0][1]["text"]), ("error", "RuntimeError: the model fell over"))
ev = ask("synthesize", {"text": "still here"})
check("and the next reply is spoken all the same", [t for t, _, _ in ev][-1], "audio-stop")
check("nonsense on the wire gets no answer", ask(None, raw=b"this is not an event\n"), [])
check("nor does half an event", ask(None, raw=b'{"type": "synthesize", "data_length": 500}\n{"te'), [])
ev = ask("synthesize", {"text": "after the nonsense"})
check("and the service is still up", [t for t, _, _ in ev][-1], "audio-stop")

print("-- one mouth --")
model.slow, model.most = 0.15, 0
pair = [threading.Thread(target=ask, args=("synthesize", {"text": f"reply {i}"})) for i in range(3)]
[t.start() for t in pair]
[t.join() for t in pair]
model.slow = 0.0
check("three replies at once are spoken one at a time", model.most, 1)

print("-- describe --")
ev = ask("describe")
voices = ev[0][1]["tts"][0]["voices"]
check("it names its voices and their languages", [(v["name"], v["languages"]) for v in voices],
      [("am_michael", ["en-us"]), ("bm_lewis", ["en-gb"])])

sys.stderr = real
log = journal.getvalue()
print("-- the journal --")
check("has a line for each reply, with sizes and timings",
      "douglas-tts: 39 chars as bm_lewis: 0.0 s of speech in 0.00 s" in log, True)
check("and for a voice it did not have", "no voice called 'en_GB-alan-medium' here - using bm_lewis" in log, True)
check("but never what he said", "garden shed" in log or "still here" in log, False)

print("-- a default voice the model does not have --")
try:
    tts.Server(("127.0.0.1", 0), model, default="bm_nobody")
    check("is refused at start-up", "started", "refused")
except SystemExit as e:
    check("is refused at start-up", str(e), "douglas-tts: the model has no voice called 'bm_nobody'")

server.shutdown()
print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("all passed")
