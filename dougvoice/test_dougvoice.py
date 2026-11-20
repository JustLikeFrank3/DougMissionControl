#!/usr/bin/env python3
"""dougvoice's logic without a Pi, a HAT or a Douglas. Runs anywhere.

    python3 dougvoice/test_dougvoice.py
"""

import asyncio
import importlib.util
import os
import sys

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))


spec = importlib.util.spec_from_file_location(
    "dougvoice", os.path.join(os.path.dirname(os.path.abspath(__file__)), "dougvoice.py"))
dv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dv)


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


# ── satellite command line ───────────────────────────────────────────────
print("satellite argv")
a = dv.satellite_argv({})
check("listens on :10700 for Douglas", flag(a, "--uri"), "tcp://0.0.0.0:10700")
check("mic from the Whisplay card, 16 kHz mono",
      flag(a, "--mic-command"),
      "arecord -D plughw:CARD=whisplaysound,DEV=0 -r 16000 -c 1 -f S16_LE -t raw")
check("events go to the face", flag(a, "--event-uri"), "tcp://127.0.0.1:10500")
check("no wake word = no wake service (stream mode)", "--wake-uri" in a, False)
check("mode with no wake word", dv.listen_mode({"WAKE_WORD": ""}), "stream")
w = dv.satellite_argv({"WAKE_WORD": "ok_nabu", "MIC_VOLUME": "2.0",
                       "SATELLITE_EXTRA_ARGS": "--debug --awake-wav '/a b.wav'"})
check("wake word points the satellite at the face's shim, not openWakeWord",
      flag(w, "--wake-uri"), "tcp://127.0.0.1:10400")
check("wake word name passed", flag(w, "--wake-word-name"), "ok_nabu")
check("mic volume passed", flag(w, "--mic-volume-multiplier"), "2.0")
check("extra args shell-split", w[-3:], ["--debug", "--awake-wav", "/a b.wav"])
check("openWakeWord stays on loopback behind the shim",
      flag(dv.wake_argv({"WAKE_WORD": "hey_jarvis"}), "--uri"), "tcp://127.0.0.1:10401")
check("douglas-agent url",
      dv.douglas_url({"DOUGLAS_AGENT_TOKEN": "t0k"}, "/ptt"), "http://douglas:9111/ptt?token=t0k")

# ── state ────────────────────────────────────────────────────────────────
print("state tracker")
T = dv.StateTracker()
check("nothing heard yet = idle (no token, satellite only)", T.current(0), "idle")
T.event("satellite-disconnected", 1)
check("satellite dropped = offline", T.current(1), "offline")
T.event("satellite-connected", 2)
T.event("streaming-start", 2.1)
check("streaming-start is not the user talking", T.current(2.1), "idle")
T.event("detection", 3)
check("detection = listening", T.current(3), "listening")
T.event("transcript", 5)
check("transcript = thinking", T.current(5), "thinking")
T.event("synthesize", 7)
check("synthesize = speaking", T.current(7), "speaking")
T.event("played", 9)
check("played = idle", T.current(9), "idle")
T.event("transcript", 10)
check("a lost 'played' cannot pin the face", T.current(10 + 46), "idle")

print("state tracker with Douglas")
T = dv.StateTracker()
T.douglas(None, 0)
check("token but Douglas never answered = offline", T.current(0), "offline")
T.douglas({"state": "idle", "satellite": True}, 1)
check("Douglas idle", T.current(1), "idle")
T.hint("listening", 2)                          # button press
T.douglas({"state": "idle", "satellite": True}, 2.5)
check("Douglas's unchanged idle does not overrule a fresh press", T.current(2.5), "listening")
T.douglas({"state": "thinking", "satellite": True}, 4)
check("Douglas's change wins", T.current(4), "thinking")
T.douglas({"state": "speaking", "satellite": True}, 5)
T.event("played", 6)
check("newer local event wins over older remote", T.current(6), "idle")
T.douglas({"state": "listening", "satellite": True}, 7)
T.hint("thinking", 8)
T.douglas({"state": "listening", "satellite": True}, 47)
check("unconfirmed hint still newer", T.current(47), "thinking")
T.douglas({"state": "listening", "satellite": True}, 54)
check("expired hint falls back to Douglas, not idle", T.current(54), "listening")
check("polls stop answering = offline", T.current(54 + dv.DOUGLAS_TIMEOUT_S + 1), "offline")
T.douglas({"state": "absent", "satellite": False}, 60)
check("no pipeline on Douglas = offline", T.current(60), "offline")
T.douglas({"state": "idle", "satellite": False}, 61)
check("pipeline not connected to the satellite = offline", T.current(61), "offline")
T.douglas({"state": "idle", "satellite": True}, 62)
check("and back", T.current(62), "idle")
T.douglas({"state": "weird", "satellite": True}, 63)
check("unknown state from Douglas is ignored", T.current(63), "idle")

# ── button ───────────────────────────────────────────────────────────────
print("button")
B = dv.Button("stream")
check("press while idle = start", B.press("idle", 0), "start")
check("release after a real hold = stop", B.release(2), "stop")
check("press", B.press("idle", 10), "start")
check("a brush shorter than MIN_HOLD = cancel", B.release(10.1), "cancel")
check("press while speaking = cancel (barge-in)", B.press("speaking", 20), "cancel")
check("its release sends nothing", B.release(21), None)
check("press while offline = nothing", B.press("offline", 30), None)
check("its release sends nothing", B.release(31), None)
check("release with no press = nothing", B.release(32), None)
W = dv.Button("wake")
check("wake mode: press = wake", W.press("idle", 0), "wake")
check("wake mode: release = nothing (VAD ends the turn)", W.release(3), None)
check("wake mode: offline = nothing", W.press("offline", 4), None)

# ── what it looks like ───────────────────────────────────────────────────
print("face")
check("rgb565 white", dv.rgb565(255, 255, 255), b"\xff\xff")
check("rgb565 pure red, high byte first", dv.rgb565(255, 0, 0), b"\xf8\x00")
check("rgb565 pure blue", dv.rgb565(0, 0, 255), b"\x00\x1f")
for s in dv.STATES:
    f = dv.render(s)
    check(f"{s}: full 240x280 RGB565 frame", len(f), 240 * 280 * 2)
    centre = (130 * 240 + 120) * 2
    want = dv.rgb565(0, 0, 0) if s == "offline" else dv.rgb565(*dv.COLOURS[s])
    check(f"{s}: centre pixel ({'ring' if s == 'offline' else 'disc'})",
          f[centre:centre + 2], want)
check("every state's word is in the font",
      all(c in dv.FONT for s in dv.STATES for c in s.upper() + "DOUGVOICE"), True)
check("frames differ per state", len({dv.render(s) for s in dv.STATES}), len(dv.STATES))
check("listening LED is steady full", dv.led_rgb("listening", 0), dv.led_rgb("listening", 0.4))
check("thinking LED breathes", dv.led_rgb("thinking", 0.3) != dv.led_rgb("thinking", 0.9), True)
check("thinking LED never goes dark",
      min(max(dv.led_rgb("thinking", t / 10)) for t in range(13)) > 0, True)
check("unknown state LED = off", dv.led_rgb("nope", 0), (0, 0, 0))


# ── Wyoming framing ──────────────────────────────────────────────────────
print("wyoming framing")


async def roundtrip(blob):
    r = asyncio.StreamReader()
    r.feed_data(blob)
    r.feed_eof()
    out = []
    while (ev := await dv.read_event(r)) is not None:
        out.append(ev)
    return out


blob = (dv.encode_event("transcript", {"text": "hello Douglas"})
        + dv.encode_event("audio-chunk", {"rate": 16000}, b"\x01\x02\x03\x04")
        + dv.encode_event("played"))
evs = asyncio.run(roundtrip(blob))
check("three events back", [e["type"] for e in evs], ["transcript", "audio-chunk", "played"])
check("data survives", evs[0]["data"], {"text": "hello Douglas"})
check("payload survives", evs[1]["payload"], b"\x01\x02\x03\x04")
check("re-encoding is lossless", b"".join(dv.raw_event(e) for e in evs), blob)
inline = b'{"type": "detection", "data": {"name": "ok_nabu"}}\n'
check("inline data (older peers) is read too",
      asyncio.run(roundtrip(inline))[0]["data"], {"name": "ok_nabu"})


# ── the face's event server, end to end on a real socket ────────────────
async def event_server():
    face = dv.Face.__new__(dv.Face)                  # no board, no env needed
    face.tracker = dv.StateTracker()
    srv = await asyncio.start_server(face.serve_events, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    _, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(dv.encode_event("satellite-connected") + dv.encode_event("voice-started"))
    await w.drain()
    await asyncio.sleep(0.1)
    w.close()
    srv.close()
    return face.tracker.current(face.tracker.local[1])


print("event server")
check("satellite events over TCP move the face", asyncio.run(event_server()), "listening")

async def wake_shim(with_oww):
    """satellite ⇄ shim ⇄ fake openWakeWord, then a button press."""
    got_by_oww = []

    async def oww(r, w):
        while (ev := await dv.read_event(r)) is not None:
            got_by_oww.append(ev["type"])
            if ev["type"] == "describe":
                w.write(dv.encode_event("info", {"wake": [{"name": "openwakeword"}]}))
                await w.drain()

    up = await asyncio.start_server(oww, "127.0.0.1", 0)
    face = dv.Face.__new__(dv.Face)
    face.env = {"WAKE_WORD": "ok_nabu",
                "WAKE_PORT": str(up.sockets[0].getsockname()[1] if with_oww else 1)}
    face.wake_clients = set()
    shim = await asyncio.start_server(face.serve_wake, "127.0.0.1", 0)
    r, w = await asyncio.open_connection("127.0.0.1", shim.sockets[0].getsockname()[1])
    w.write(dv.encode_event("describe"))
    await w.drain()
    info = await asyncio.wait_for(dv.read_event(r), 2)
    w.write(dv.encode_event("audio-chunk", {"rate": 16000}, b"\0\0"))
    await w.drain()
    await asyncio.sleep(0.1)
    face.inject_detection()
    det = await asyncio.wait_for(dv.read_event(r), 2)
    w.close()
    shim.close()
    up.close()
    return info["data"]["wake"][0]["name"], det["type"], det["data"]["name"], got_by_oww


print("wake shim")
check("proxies openWakeWord, button injects a detection", asyncio.run(wake_shim(True)),
      ("openwakeword", "detection", "ok_nabu", ["describe", "audio-chunk"]))
check("without openWakeWord it answers describe itself; button still wakes",
      asyncio.run(wake_shim(False))[:3], ("dougvoice-button", "detection", "ok_nabu"))

print("dougvoice tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
