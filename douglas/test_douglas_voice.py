#!/usr/bin/env python3
"""douglas-voice against fake STT, LLM, TTS and satellite. Runs anywhere.

    python3 douglas/test_douglas_voice.py

The pipeline's state file and transcript are read back the way
douglas-agent reads them, so the two halves of the contract are held to
each other rather than each to its own idea of it.
"""

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))


def until(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


tmp = tempfile.mkdtemp()
voice_file = os.path.join(tmp, "run", "voice.json")
transcript_file = os.path.join(tmp, "lib", "transcript.jsonl")
os.environ.update({
    "VOICE_STATE_FILE": voice_file, "TRANSCRIPT_FILE": transcript_file,
    "VOICE_MIN_LISTEN_S": "0.1", "VOICE_MAX_LISTEN_S": "60",
    "DOUGLAS_AGENT_TOKEN": "t", "VOICE_PTT_URL": "",
})
here = os.path.dirname(os.path.abspath(__file__))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


voice = load("voice", "douglas-voice.py")
agent = load("agent", "douglas-agent.py")      # the reader of what we write


def on_disk():
    with open(voice_file) as f:
        return json.load(f)


def lines():
    try:
        with open(transcript_file) as f:
            return [json.loads(x) for x in f]
    except OSError:
        return []


SECOND = b"\x01\x00" * 16000                    # 1 s of 16 kHz mono s16
FMT = {"rate": 16000, "width": 2, "channels": 1}

print("-- state file --")
st = voice.StateFile(voice_file)
st.heartbeat()
check("first write is idle, no satellite",
      {k: on_disk()[k] for k in ("state", "satellite", "last_turn")},
      {"state": "idle", "satellite": False, "last_turn": None})
check("agent reads it as idle", agent.voice_state()["state"], "idle")
st.update(satellite=True)
check("satellite change written", on_disk()["satellite"], True)
before = on_disk()["ts"]
time.sleep(0.02)
st.heartbeat()
check("heartbeat refreshes ts with no change", on_disk()["ts"] > before, True)
try:
    st.update(state="dreaming")
    check("unknown state refused", "accepted", "ValueError")
except ValueError:
    check("unknown state refused", "ValueError", "ValueError")
check("no temp files left beside it",
      sorted(os.listdir(os.path.dirname(voice_file))), ["voice.json"])

print("-- transcript --")
tr = voice.Transcript(transcript_file)
check("blank text writes nothing", tr.append("user", "   ", "voice"), None)
tr.append("user", "what's  the\nweather", "deck")
tr.append("robot", "Sunny.", "carrier-pigeon")
got = lines()
check("one line per turn", len(got), 2)
check("whitespace folded", got[0]["text"], "what's the weather")
check("fields", sorted(got[0]), ["text", "ts", "via", "who"])
check("unknown who is douglas, unknown via is voice",
      (got[1]["who"], got[1]["via"]), ("douglas", "voice"))
check("agent reads the same turns", [t["text"] for t in agent.transcript()],
      ["what's the weather", "Sunny."])
os.remove(transcript_file)

print("-- a whole turn --")
SENT = []
SEEN = {}


def fake_stt(audio, fmt):
    SEEN["stt"] = (len(audio), fmt)
    return "What time is it?"


def fake_llm(messages):
    SEEN["messages"] = messages
    return "<think>the user wants the time</think>It is **ten past** nine."


def fake_tts(text):
    SEEN["tts"] = text
    return b"\x00\x10" * 2205, (22050, 2, 1)       # 0.1 s


def sender(*a):
    SENT.append(a[0])
    if a[0] == "audio-stop":                       # the satellite plays it
        threading.Timer(0.05, pipe.satellite_event, ("played", {}, None)).start()
    return True


st = voice.StateFile(voice_file)
pipe = voice.Pipeline(st, voice.Transcript(transcript_file), send=sender,
                      snd_format=lambda: (16000, 2, 1),
                      stt=fake_stt, llm=fake_llm, tts=fake_tts)
pipe.satellite_event("audio-chunk", FMT, SECOND)
check("audio while idle is ignored", pipe._audio, bytearray())
check("stop while idle refused", pipe.ptt("stop", "deck")[0], False)
check("start", pipe.ptt("start", "deck"), (True, "listening"))
check("state file says listening", on_disk()["state"], "listening")
check("agent agrees", agent.voice_state()["state"], "listening")
pipe.satellite_event("audio-chunk", FMT, SECOND)
pipe.satellite_event("audio-chunk", FMT, SECOND)
check("stop", pipe.ptt("stop", "deck"), (True, "thinking"))
check("back to idle after speaking", until(lambda: on_disk()["state"] == "idle"), True)
check("stt got both seconds", SEEN["stt"], (64000, (16000, 2, 1)))
check("system prompt names Douglas", "Douglas" in SEEN["messages"][0]["content"], True)
check("reasoning and markdown not spoken", SEEN["tts"], "It is ten past nine.")
check("transcript: user then douglas, via deck",
      [(t["who"], t["text"], t["via"]) for t in lines()],
      [("user", "What time is it?", "deck"), ("douglas", "It is ten past nine.", "deck")])
check("last_turn stamped", isinstance(on_disk()["last_turn"], float), True)
check("satellite saw the turn in order",
      [e for e in SENT if e != "audio-chunk"],
      ["voice-started", "voice-stopped", "transcript", "audio-start", "audio-stop"])
check("history carried to the next turn", len(pipe.history), 2)

print("-- states along the way --")
GATE = threading.Event()
TRAIL = []


def slow_llm(messages):
    TRAIL.append(on_disk()["state"])
    GATE.wait(5)
    return "Done."


def watching_tts(text):
    TRAIL.append(on_disk()["state"])
    return b"\x00\x00" * 1600, (16000, 2, 1)


pipe.llm, pipe.tts = slow_llm, watching_tts
pipe.ptt("start", "voice")
pipe.satellite_event("audio-chunk", FMT, SECOND)
pipe.ptt("stop", "voice")
until(lambda: TRAIL)
check("start refused while thinking", pipe.ptt("start", "voice"),
      (False, "Douglas is still thinking"))
GATE.set()
until(lambda: on_disk()["state"] == "idle")
check("thinking during the model, still thinking during tts", TRAIL,
      ["thinking", "thinking"])

print("-- cancel and short presses --")
n = len(lines())
pipe.ptt("start", "voice")
pipe.satellite_event("audio-chunk", FMT, SECOND)
check("cancel", pipe.ptt("cancel", "voice"), (True, "cancelled"))
check("cancel is idle at once", on_disk()["state"], "idle")
pipe.ptt("start", "voice")
pipe.satellite_event("audio-chunk", FMT, b"\x00\x00" * 100)
check("a tap is too short to send", pipe.ptt("stop", "voice"), (True, "too short"))
check("nothing transcribed for either", len(lines()), n)

GATE.clear()
pipe.llm = slow_llm
pipe.ptt("start", "voice")
pipe.satellite_event("audio-chunk", FMT, SECOND)
pipe.ptt("stop", "voice")
until(lambda: len(TRAIL) >= 3)
pipe.ptt("cancel", "deck")
GATE.set()
time.sleep(0.2)
check("cancel mid-thought: no reply logged",
      [t["who"] for t in lines()][n:], ["user"])
check("and it stays idle", on_disk()["state"], "idle")

print("-- failures never wedge it --")


def broken_stt(audio, fmt):
    raise OSError("connection refused")


pipe.stt = broken_stt
SENT.clear()
pipe.ptt("start", "voice")
pipe.satellite_event("audio-chunk", FMT, SECOND)
pipe.ptt("stop", "voice")
check("stt down returns to idle", until(lambda: on_disk()["state"] == "idle"), True)
check("satellite told", "error" in SENT, True)
pipe.linked = lambda: False
check("start refused with no satellite", pipe.ptt("start", "deck"),
      (False, "DougVoice is not connected"))
pipe.linked = lambda: True
pipe.ptt("start", "voice")
pipe.link(False)
check("satellite drop while listening = idle", on_disk()["state"], "idle")
check("and satellite false", on_disk()["satellite"], False)

print("-- typed turns --")
SAID = []


def typed_llm(messages):
    SAID.append(messages)
    return "<think>short answer</think>Line one.\n\n**Two** lines."


before = len(lines())
tpipe = voice.Pipeline(st, voice.Transcript(transcript_file), send=sender,
                       snd_format=lambda: (16000, 2, 1),
                       stt=fake_stt, llm=typed_llm, tts=fake_tts)
tpipe.history = [{"role": "user", "content": "earlier"},
                 {"role": "assistant", "content": "spoken reply"}]
tpipe.last_exchange = time.time()
SENT.clear()
check("blank refused", tpipe.say("   ", "phone"), (False, "nothing to say"))
check("say accepted", tpipe.say("What's  the\nplan?", "phone"), (True, "thinking"))
check("back to idle", until(lambda: on_disk()["state"] == "idle"), True)
new = lines()[before:]
check("transcript: typed user then douglas, via phone",
      [(t["who"], t["via"]) for t in new], [("user", "phone"), ("douglas", "phone")])
check("typed text whitespace-normalised", new[0]["text"], "What's the plan?")
check("screen reply keeps words and lines, drops reasoning",
      new[1]["text"], "Line one.\n\n**Two** lines.")
check("shares the spoken conversation's memory",
      [m["content"] for m in SAID[0][1:3]], ["earlier", "spoken reply"])
check("not spoken unless asked", [e for e in SENT if e.startswith("audio")], [])
tpipe.st.update(state="thinking")
check("refused while thinking", tpipe.say("again", "deck")[0], False)
tpipe.st.update(state="idle")
check("unknown via falls back to deck", (tpipe.say("hi", "fax"), until(
    lambda: on_disk()["state"] == "idle"), lines()[-1]["via"])[2], "deck")
check("agent relays /say beside /ptt",
      agent.relay_say({"text": ""})[0], 400)

print("-- helpers --")
check("speakable drops an unterminated think", voice.speakable("Hi.<think>hmm"), "Hi.")
check("speakable keeps link text", voice.speakable("see [the docs](http://x)"), "see the docs")
mono = b"\x00\x01" * 2205
check("resample 22050 → 16000 keeps duration",
      len(voice.convert(mono, (22050, 2, 1), (16000, 2, 1))) // 2, 1600)
check("mono → stereo doubles", len(voice.convert(mono, (22050, 2, 1), (22050, 2, 2))),
      len(mono) * 2)
check("same format untouched", voice.convert(mono, (22050, 2, 1), (22050, 2, 1)), mono)

print("-- wyoming framing --")
a, b = socket.socketpair()
voice.write_event(a, "audio-chunk", FMT, b"\x01\x02\x03")
voice.write_event(a, "audio-stop")
fb = b.makefile("rb")
check("data and payload round trip", voice.read_event(fb), ("audio-chunk", FMT, b"\x01\x02\x03"))
check("event with no data", voice.read_event(fb), ("audio-stop", {}, None))
a.close()
check("EOF is None", voice.read_event(fb), None)
check("inline data (older peers) is read too",
      voice.read_event(io.BytesIO(b'{"type":"transcript","data":{"text":"hi"}}\n')),
      ("transcript", {"text": "hi"}, None))



def serve_once(handler):
    srv = socket.create_server(("127.0.0.1", 0))

    def run():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=handler, args=(c,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def fake_whisper(c):
    f = c.makefile("rb")
    got = 0
    while (ev := voice.read_event(f)) is not None:
        if ev[0] == "audio-chunk":
            got += len(ev[2])
        if ev[0] == "audio-stop":
            voice.write_event(c, "transcript-start", {})
            voice.write_event(c, "transcript", {"text": f" heard {got} bytes "})
            c.close()
            return


def fake_piper(c):
    ev = voice.read_event(c.makefile("rb"))
    a = {"rate": 22050, "width": 2, "channels": 1}
    voice.write_event(c, "audio-start", a)
    voice.write_event(c, "audio-chunk", a, ev[1]["text"].encode() * 2)
    voice.write_event(c, "audio-stop")
    c.close()


voice.STT_PORT = serve_once(fake_whisper)
voice.TTS_PORT = serve_once(fake_piper)
check("stt client against a Wyoming server", voice.stt(SECOND, (16000, 2, 1)),
      "heard 32000 bytes")
check("tts client against a Wyoming server", voice.tts("abcd"),
      (b"abcdabcd", (22050, 2, 1)))


class Llama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        out = json.dumps({"choices": [{"message": {
            "content": f"{len(body['messages'])} messages"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


ls = HTTPServer(("127.0.0.1", 0), Llama)
threading.Thread(target=ls.serve_forever, daemon=True).start()
voice.LLM_URL = f"http://127.0.0.1:{ls.server_address[1]}"
check("llm client against an OpenAI-style server",
      voice.llm([{"role": "user", "content": "hi"}]), "1 messages")

print("-- the satellite link --")
SAT_SEEN = []


def fake_satellite(c):
    f = c.makefile("rb")
    SAT_SEEN.append(voice.read_event(f)[0])
    SAT_SEEN.append(voice.read_event(f)[0])
    voice.write_event(c, "run-pipeline", {"start_stage": "wake", "end_stage": "tts",
                                          "snd_format": {"rate": 16000, "width": 2,
                                                         "channels": 1}})
    voice.write_event(c, "ping", {"text": "x"})
    SAT_SEEN.append(voice.read_event(f)[0])
    voice.write_event(c, "audio-chunk", FMT, b"\x00\x00" * 10)
    time.sleep(0.2)
    c.close()


LINK, EVENTS = [], []
sat = voice.Satellite("127.0.0.1", serve_once(fake_satellite),
                      lambda *e: EVENTS.append(e[0]), LINK.append)
threading.Thread(target=sat.run_forever, daemon=True).start()
until(lambda: len(LINK) >= 2)
check("describe then run-satellite, pong to a ping", SAT_SEEN,
      ["describe", "run-satellite", "pong"])
check("speaker format taken from run-pipeline", sat.snd_format, (16000, 2, 1))
check("mic audio handed on", EVENTS, ["audio-chunk"])
check("link up then down", LINK[:2], [True, False])

print("-- PTT endpoint (what douglas-agent relays to) --")
voice.PIPE = voice.Pipeline(voice.StateFile(voice_file), voice.Transcript(transcript_file),
                            send=lambda *a: True)
hs = ThreadingHTTPServer(("127.0.0.1", 0), voice.Handler)
threading.Thread(target=hs.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{hs.server_address[1]}/ptt"
agent.VOICE_PTT_URL = url
check("agent relay start", agent.relay_ptt({"action": "start", "via": "deck"}),
      (200, {"ok": True, "message": "start"}))
check("pipeline listening", voice.PIPE.state, "listening")
check("agent relay cancel", agent.relay_ptt({"action": "cancel"}),
      (200, {"ok": True, "message": "cancel"}))
check("agent relays the pipeline's refusal, not 'did not answer'",
      agent.relay_ptt({"action": "stop"}),
      (200, {"ok": False, "message": "not listening (idle)"}))


def post(body, q=""):
    req = urllib.request.Request(url + q, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


check("bad action 400", post(b'{"action":"jump"}'), 400)
check("not JSON 400", post(b"[1]"), 400)
voice.PTT_TOKEN = "s3cret"
check("token set: missing token 403", post(b'{"action":"cancel"}'), 403)
check("token set: right token 200", post(b'{"action":"cancel"}', "?token=s3cret"), 200)

hs.shutdown()
ls.shutdown()
print("douglas-voice tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
