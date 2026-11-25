#!/usr/bin/env python3
"""Tests for webui-connect.py: against a throwaway database laid out the way
Open WebUI lays its own out, and throwaway settings files. No Open WebUI.

    python3 douglas/test_webui_connect.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
prog = os.path.join(here, "webui-connect.py")
FAILS = 0
TOKEN = "tok_" + "z9" * 20
URL = "http://127.0.0.1:10800/v1"
BARE = "http://127.0.0.1:8081/v1"


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))


def place(rows=None, legacy=False, voice=f"VOICE_PTT_PORT=10800\nDOUGLAS_API_TOKEN={TOKEN}\n",
          env="OPEN_WEBUI_PORT=8080\nOPENAI_API_BASE_URL=" + BARE + "\nOPENAI_API_KEY=none\nWEBUI_NAME=Douglas\n"):
    d = tempfile.mkdtemp()
    paths = {k: os.path.join(d, k) for k in ("webui.db", "voice.env", "open-webui.env")}
    open(paths["voice.env"], "w").write(voice)
    open(paths["open-webui.env"], "w").write(env)
    if rows is not None or legacy:
        db = sqlite3.connect(paths["webui.db"])
        if legacy:
            db.execute("CREATE TABLE config (id INTEGER PRIMARY KEY, data JSON, version INTEGER)")
            db.execute("INSERT INTO config (data, version) VALUES (?, 0)", (json.dumps({"openai": {}}),))
        else:
            db.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at BIGINT)")
            db.execute("CREATE TABLE chat (id TEXT, chat JSON)")
            db.execute("INSERT INTO chat VALUES ('c1', ?)", (json.dumps({"title": "private words"}),))
            for k, v in rows.items():
                db.execute("INSERT INTO config VALUES (?, ?, 1)", (k, json.dumps(v)))
        db.commit()
        db.close()
    return paths


def run(paths, mode):
    r = subprocess.run([sys.executable, "-I", prog, paths["webui.db"], paths["voice.env"], paths["open-webui.env"],
                        URL, mode], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def kept(paths):
    db = sqlite3.connect(paths["webui.db"])
    out = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM config")}
    out["chat"] = db.execute("SELECT chat FROM chat").fetchall() if "chat" in [
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] else None
    db.close()
    return out


print("== Douglas among Open WebUI's connections, where it keeps them")
p = place({"openai.api_base_urls": [BARE], "openai.api_keys": ["none"], "openai.api_configs": {},
           "ui.default_models": None, "task.model.default": ""})
code, said = run(p, "on")
got = kept(p)
check("added beside the bare model, with the token as its key; nothing else in the database is touched",
      (code, got["openai.api_base_urls"], got["openai.api_keys"], got["openai.api_configs"], got["ui.default_models"],
       got["task.model.default"], got["chat"]),
      (0, [BARE, URL], ["none", TOKEN], {}, None, "", [(json.dumps({"title": "private words"}),)]))
check("what it says is what it did and where, and never the token",
      (said, TOKEN in said), ("Douglas is among Open WebUI's connections, in its database: 2 in all now", False))
run(p, "on")
check("run again, it is there once", kept(p)["openai.api_base_urls"], [BARE, URL])
open(p["voice.env"], "w").write("DOUGLAS_API_TOKEN=tok_" + "q7" * 20 + "\n")
run(p, "on")
check("with a new token, the connection has the new one and the bare model keeps its own",
      kept(p)["openai.api_keys"], ["none", "tok_" + "q7" * 20])
code, said = run(p, "off")
check("taken out, the bare model is as it was",
      (code, said, kept(p)["openai.api_base_urls"], kept(p)["openai.api_keys"]),
      (0, "Douglas is out of Open WebUI's connections, in its database: 1 in all now", [BARE], ["none"]))
check("taken out when it is not there changes nothing", (run(p, "off")[0], kept(p)["openai.api_base_urls"]), (0, [BARE]))

p = place({"openai.api_base_urls": [BARE, URL, "https://third.example/v1"], "openai.api_keys": ["none", "old", "k3"],
           "openai.api_configs": {"0": {"enable": True}, "1": {"tags": ["douglas"]}, "2": {"prefix_id": "third"},
                                  URL: {"legacy": True}}})
run(p, "off")
got = kept(p)
check("taken out of the middle, the connections after it move up, each with what was set for it, and what was "
      "set for it goes with it",
      (got["openai.api_base_urls"], got["openai.api_keys"], got["openai.api_configs"]),
      ([BARE, "https://third.example/v1"], ["none", "k3"], {"0": {"enable": True}, "1": {"prefix_id": "third"}}))
run(p, "on")
check("and put back, it goes to the end, where nothing has to move",
      (kept(p)["openai.api_base_urls"], kept(p)["openai.api_keys"]),
      ([BARE, "https://third.example/v1", URL], ["none", "k3", TOKEN]))

p = place({"openai.api_base_urls": [BARE], "openai.api_keys": [], "openai.api_configs": {}})
run(p, "on")
check("fewer keys than connections: each connection keeps its place", kept(p)["openai.api_keys"], ["", TOKEN])

p = place({"openai.api_base_urls": [BARE], "openai.api_keys": ["none"]})
run(p, "on")
check("with no row for what is set for each connection, one is made, empty", kept(p)["openai.api_configs"], {})

print("== before Open WebUI has run: its settings file")
p = place()
code, said = run(p, "on")
env = open(p["open-webui.env"]).read().splitlines()
check("with no database yet the connection goes into its settings file, as the two lists it reads",
      (code, said, [l for l in env if l.startswith("OPENAI")], [l for l in env if not l.startswith("OPENAI")]),
      (0, "Douglas is among Open WebUI's connections, in its settings file: 2 in all now",
       [f"OPENAI_API_BASE_URLS={BARE};{URL}", f"OPENAI_API_KEYS=none;{TOKEN}"], ["OPEN_WEBUI_PORT=8080", "WEBUI_NAME=Douglas"]))
run(p, "on")
check("once", [l for l in open(p["open-webui.env"]).read().splitlines() if "BASE_URLS" in l],
      [f"OPENAI_API_BASE_URLS={BARE};{URL}"])
run(p, "back")
check("and out again", [l for l in open(p["open-webui.env"]).read().splitlines() if l.startswith("OPENAI")],
      [f"OPENAI_API_BASE_URLS={BARE}", "OPENAI_API_KEYS=none"])
p = place({"ui.default_models": None})
run(p, "on")
check("a database that has no connections saved in it yet is left alone: the settings file it is",
      ("openai.api_base_urls" in kept(p), [l for l in open(p["open-webui.env"]).read().splitlines() if "BASE_URLS" in l]),
      (False, [f"OPENAI_API_BASE_URLS={BARE};{URL}"]))

print("== what it will not do")
p = place({"openai.api_base_urls": [BARE], "openai.api_keys": ["none"]}, voice="VOICE_PTT_PORT=10800\n")
code, said = run(p, "on")
check("without a token for the line nothing is connected",
      (code, said, kept(p)["openai.api_base_urls"]),
      (1, "there is no token for the line in voice.env: nothing was connected", [BARE]))
check("taking it out needs none", run(p, "off")[0], 0)
p = place(legacy=True)
code, said = run(p, "on")
check("a database laid out another way is not guessed at: it says how to add the connection by hand, without "
      "the token", (code, "Add the connection by hand" in said, URL in said, TOKEN in said), (1, True, True, False))
p = place({"openai.api_base_urls": "not a list", "openai.api_keys": ["none"]})
code, said = run(p, "on")
check("saved connections that are not a list are not written over",
      (code, said, kept(p)["openai.api_base_urls"]),
      (1, "Open WebUI's saved connections are not what was expected: nothing was changed", "not a list"))
check("asked wrongly, it says how it is run", subprocess.run([sys.executable, "-I", prog, "x"], capture_output=True,
                                                            text=True).returncode, 1)

print("webui-connect tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
