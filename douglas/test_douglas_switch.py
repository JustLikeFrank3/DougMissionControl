#!/usr/bin/env python3
"""Tests for douglas-switch.py: the file it writes, what it will and will not
write, and that the voice pipeline reads the same thing out of it.

    python3 douglas/test_douglas_switch.py
"""
import importlib.util
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import json

here = os.path.dirname(os.path.abspath(__file__))
FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


d = tempfile.mkdtemp()
os.environ["SWITCHES_FILE"] = os.path.join(d, "capabilities")
os.environ["DOUGLAS_SWITCHES"] = os.environ["SWITCHES_FILE"]
sw = load("sw", "douglas-switch.py")
journal, real_err = io.StringIO(), sys.stderr
sys.stderr = journal

print("== what it writes")
check("with no file nothing is off", (sw.answer({"op": "show"}), os.path.exists(sw.FILE)), ({"ok": True, "off": []}, False))
check("a tool switched off is a line in the file, and it says what is off now",
      (sw.answer({"op": "set", "tool": "search", "on": False}), [l for l in open(sw.FILE).read().splitlines() if not l.startswith("#")],
       oct(os.stat(sw.FILE).st_mode & 0o777)), ({"ok": True, "off": ["search"]}, ["search=off"], "0o644"))
sw.answer({"op": "set", "tool": "repo", "on": False})
sw.answer({"op": "set", "tool": "search", "on": False})
check("another, and the same one again: each is there once, in the order the tools are known in",
      sw.answer({"op": "show"})["off"], ["search", "repo"])
check("switched on again, its line is gone and the other stays",
      (sw.answer({"op": "set", "tool": "search", "on": True})["off"],
       [l for l in open(sw.FILE).read().splitlines() if not l.startswith("#")]), (["repo"], ["repo=off"]))
check("nothing is left beside the file", sorted(os.listdir(d)), ["capabilities"])

print("== what it will not write")
before = open(sw.FILE).read()
refused = [sw.answer(q) for q in (
    {"op": "set", "tool": "shell", "on": False}, {"op": "set", "tool": "search\nrepo=on", "on": True},
    {"op": "set", "tool": "../../etc/passwd", "on": False}, {"op": "set", "tool": "search", "on": "false"},
    {"op": "set", "tool": "search", "on": 0}, {"op": "set", "tool": "search"}, {"op": "set", "on": True},
    {"op": "write", "text": "search=on"}, {"op": "clear"}, "show", None, ["set"], {"tool": "search", "on": True})]
check("a tool it does not know, an on that is not true or false, and anything that is not one of its two "
      "questions: refused, and the file is as it was",
      ([r["ok"] for r in refused], open(sw.FILE).read() == before), ([False] * 13, True))
open(sw.FILE, "w").write("repo=off\nsearch = off\nshell=off\nweather=on\nrepo=off\n\x00garbage\n")
check("a file somebody else has been at is read for what it knows, and written back clean",
      (sw.answer({"op": "show"})["off"], sw.answer({"op": "set", "tool": "weather", "on": False})["off"],
       [l for l in open(sw.FILE).read().splitlines() if not l.startswith("#")]),
      (["search", "repo"], ["weather", "search", "repo"], ["weather=off", "search=off", "repo=off"]))

print("== and what the pipeline makes of it")
voice = load("voice", "douglas-voice.py")
voice.SWITCHED.update(stamp="never", off=frozenset(), offered=frozenset())
check("the pipeline reads out of the file what this wrote into it",
      (voice.SWITCHES_FILE == sw.FILE, sorted(voice.switched_off())), (True, ["repo", "search", "weather"]))
check("every tool the pipeline could be given is one this knows how to switch, and the other way round",
      sorted(voice.TOOL_FACTS) == sorted(sw.TOOLS), True)
for t in sw.TOOLS:
    sw.answer({"op": "set", "tool": t, "on": True})
check("all switched on again, the pipeline has nothing off", (sw.answer({"op": "show"})["off"], voice.switched_off()),
      ([], frozenset()))

print("== at its door")
path = os.path.join(d, "switch.sock")
door = socket.socket(socket.AF_UNIX)
door.bind(path)
door.listen(4)
proc = subprocess.Popen([sys.executable, "-I", "-c", (
    "import os, sys, importlib.util\n"
    "os.dup2(int(sys.argv[2]), 3); os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS='1')\n"
    "spec = importlib.util.spec_from_file_location('sw', sys.argv[1]); m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m); m.serve()\n"), os.path.join(here, "douglas-switch.py"), str(door.fileno())],
    pass_fds=[door.fileno()], env=dict(os.environ), stderr=subprocess.DEVNULL)


def knock(line: bytes) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    s.sendall(line + b"\n")
    with s.makefile("rb") as f:
        return json.loads(f.readline() or b"null")


check("handed its socket, it answers one question a caller: a switch, then what is off",
      (knock(b'{"op": "set", "tool": "draw", "on": false}'), knock(b'{"op": "show"}'), knock(b"not json")["ok"],
       knock(b'{"op": "show", "pad": "' + b"x" * 9000 + b'"}')["ok"], knock(b'{"op": "set", "tool": "draw", "on": true}')),
      ({"ok": True, "off": ["draw"]}, {"ok": True, "off": ["draw"]}, False, False, {"ok": True, "off": []}))
proc.kill()
r = subprocess.run([sys.executable, "-I", os.path.join(here, "douglas-switch.py"), "serve"], capture_output=True, text=True,
                   env={k: v for k, v in os.environ.items() if not k.startswith("LISTEN_")})
check("started by hand with no socket handed over, it does not make one of its own", (r.returncode, "no socket was handed over" in r.stderr),
      (1, True))
sys.stderr = real_err
check("its journal says which tool went which way, and nothing else",
      [l for l in journal.getvalue().splitlines() if "switched" in l][:2],
      ["douglas-switch: search switched off", "douglas-switch: repo switched off"])
print("douglas-switch tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
