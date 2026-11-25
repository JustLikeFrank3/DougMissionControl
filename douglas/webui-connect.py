#!/usr/bin/env python3
"""douglas/webui-connect.py: put Douglas among Open WebUI's connections, or take him out.

    webui-connect.py <webui.db> <voice.env> <open-webui.env> <url> on|off|back

Run by webui-setup.sh, as root, with Open WebUI stopped. The connection is
the address of the pipeline's line for other programs and the token for it,
which is read from voice.env by this program and written to where Open WebUI
keeps its connections: its database once it has run (a row for each
setting; three rows are read and written and nothing else is looked at), its
settings file before that. The token is never printed.
"""
import json
import os
import sqlite3
import sys
import time

if len(sys.argv) != 6 or sys.argv[5] not in ("on", "off", "back"):
    sys.exit(__doc__.split("\n\n")[1].strip())
db_path, voice_env, env_path, url, mode = sys.argv[1:6]
on = mode == "on"
token = next((l.split("=", 1)[1].strip() for l in open(voice_env).read().splitlines()
              if l.startswith("DOUGLAS_API_TOKEN=")), "")
if on and len(token) < 20:
    sys.exit("there is no token for the line in voice.env: nothing was connected")


def settings_file():
    """Before Open WebUI has run it takes its connections from its settings file."""
    lines = open(env_path).read().splitlines()
    get = lambda k: next((l.split("=", 1)[1] for l in lines if l.startswith(k + "=")), "")   # noqa: E731
    urls = [u for u in (get("OPENAI_API_BASE_URLS") or get("OPENAI_API_BASE_URL")).split(";") if u]
    keys = ((get("OPENAI_API_KEYS") or get("OPENAI_API_KEY")).split(";") + ["none"] * len(urls))[:len(urls)]
    if url in urls:
        i = urls.index(url)
        del urls[i], keys[i]
    if on:
        urls.append(url)
        keys.append(token)
    lines = [l for l in lines if not l.startswith(("OPENAI_API_BASE_URLS=", "OPENAI_API_KEYS=",
                                                   "OPENAI_API_BASE_URL=", "OPENAI_API_KEY="))]
    lines += [f"OPENAI_API_BASE_URLS={';'.join(urls)}", f"OPENAI_API_KEYS={';'.join(keys)}"]
    open(env_path, "w").write("\n".join(lines) + "\n")
    return len(urls)


def database():
    """Once it has run it keeps them in its database, a row for each setting."""
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(db_path, timeout=20)
    if "key" not in [r[1] for r in db.execute("PRAGMA table_info(config)")]:
        if db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'config'").fetchone():
            sys.exit("this Open WebUI keeps its settings in a way this script does not know. Add the connection "
                     "by hand, in its Admin Settings under Connections: " + url + ", with DOUGLAS_API_TOKEN from "
                     + voice_env + " as the key.")
        return None
    got = {k: db.execute("SELECT value FROM config WHERE key = ?", (k,)).fetchone()
           for k in ("openai.api_base_urls", "openai.api_keys", "openai.api_configs")}
    if not got["openai.api_base_urls"]:
        return None
    read = lambda r, empty: (json.loads(r[0]) if isinstance(r[0], str) else r[0]) if r else empty   # noqa: E731
    urls, keys = read(got["openai.api_base_urls"], []), read(got["openai.api_keys"], [])
    configs = read(got["openai.api_configs"], {})
    if not isinstance(urls, list) or not isinstance(keys, list) or not isinstance(configs, dict):
        sys.exit("Open WebUI's saved connections are not what was expected: nothing was changed")
    keys = (keys + [""] * len(urls))[:len(urls)]
    if url in urls:                              # out first, with what was set for it; the others move up
        i = urls.index(url)
        del urls[i], keys[i]
        configs = {(str(int(k) - 1) if k.isdigit() and int(k) > i else k): v
                   for k, v in configs.items() if k not in (str(i), url)}
    if on:
        urls.append(url)
        keys.append(token)
    now = int(time.time())
    with db:
        for k, v in (("openai.api_base_urls", urls), ("openai.api_keys", keys), ("openai.api_configs", configs)):
            if got[k]:
                db.execute("UPDATE config SET value = ?, updated_at = ? WHERE key = ?", (json.dumps(v), now, k))
            else:
                db.execute("INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)", (k, json.dumps(v), now))
    db.close()
    return len(urls)


n = database()
where = "its database"
if n is None:
    n, where = settings_file(), "its settings file"
print(f"Douglas is {'among' if on else 'out of'} Open WebUI's connections, in {where}: {n} in all now")
