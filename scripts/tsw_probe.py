#!/usr/bin/env python3
"""tsw_probe.py — Step 0 instrument for the Train Sim World 6 external API.

Runs ON THE WINDOWS WORKSTATION, next to the game. Read-only by design: it
never PATCHes anything, so it cannot move a control, and it can run while
you drive. This is the TSW counterpart of `flightdeck-sim-agent --probe` —
it exists to answer, with evidence, the questions the RAIL integration
depends on (docs/TSW6-INTEGRATION.md):

    1. Is TSW running with -HTTPAPI, and does the key in CommAPIKey.txt work?
    2. What locomotive is loaded, and what service is it driving?
    3. What controls does THIS locomotive expose, and which are writable?
    4. Do speed / brake gauges / ammeter / DriverAid answer, in what units?
    5. What do freshness and reconnection actually look like across a game
       restart or a menu round-trip?

Usage (Python 3.8+, stdlib only):

    python tsw_probe.py                 # detect, identify, then live telemetry
    python tsw_probe.py --controls      # dump the full control tree + writability
    python tsw_probe.py --hz 4          # live loop at 4 Hz (default 2)
    python tsw_probe.py --host 127.0.0.1 --port 31270
    python tsw_probe.py --key <base64>  # override CommAPIKey.txt discovery

The game must have been launched with `-HTTPAPI` in its Steam launch
options. The API only answers meaningfully once a session is loaded —
in the menus most nodes return "Node failed to return valid data", which
this probe reports as exactly that rather than an error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PORT = 31270

# Highest-numbered TrainSimWorld* wins: one machine can hold TSW5 and TSW6
# keys side by side and they differ. Dev builds keep the key in the install
# dir; that path is not guessed here — use --key.
KEY_GLOB = "My Games/TrainSimWorld*/Saved/Config/CommAPIKey.txt"

# Lever names seen across UK/DE/US stock, for the live-loop pick. The real
# integration discovers per-loco; this list only decides what the probe
# WATCHES, and an unmatched loco still prints speed and gauges.
LEVER_HINTS = {
    "throttle": ("Throttle", "MasterController", "CombinedThrottleBrake"),
    "train_brake": ("TrainBrake", "AutomaticBrake", "AutoBrake", "Westcode"),
    "loco_brake": ("LocoBrake", "IndependentBrake", "EngineBrake", "DirectBrake"),
    "dynamic_brake": ("DynamicBrake",),
    "reverser": ("Reverser",),
}


def find_key(explicit: str | None) -> tuple[str, str]:
    """(key, where-it-came-from). Re-callable: the game can re-mint the key."""
    if explicit:
        return explicit.strip(), "--key"
    docs = Path.home() / "Documents"
    hits = sorted(docs.glob(KEY_GLOB))
    if not hits:
        sys.exit(
            f"no CommAPIKey.txt under {docs / 'My Games'} — launch TSW once "
            "with -HTTPAPI in its Steam launch options, or pass --key"
        )
    path = hits[-1]  # TrainSimWorld6 sorts after TrainSimWorld5
    key = path.read_text(encoding="utf-8", errors="replace").strip()
    if not key:
        sys.exit(f"{path} exists but is empty — relaunch the game with -HTTPAPI")
    return key, str(path)


class Tsw:
    """Minimal read-only client. One instance per (host, port, key)."""

    def __init__(self, host: str, port: int, key: str) -> None:
        self.base = f"http://{host}:{port}"
        self.key = key

    def _call(self, method: str, path: str, timeout: float = 3.0) -> dict:
        # Path segments may hold parentheses etc.; encode per segment so the
        # slashes that separate nodes survive — but never touch the query.
        path, sep, query = path.partition("?")
        quoted = "/".join(urllib.parse.quote(seg, safe="().~-_")
                          for seg in path.split("/"))
        req = urllib.request.Request(
            f"{self.base}/{quoted}{sep}{query}", method=method,
            headers={"DTGCommKey": self.key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(4_000_000).decode("utf-8", "replace"))

    def get(self, path: str) -> dict:
        return self._call("GET", f"get/{path}")

    def list(self, path: str = "") -> dict:
        return self._call("GET", f"list/{path}" if path else "list")

    def info(self) -> dict:
        return self._call("GET", "info")

    def sub_add(self, sub_id: int, path: str) -> dict:
        return self._call("POST", f"subscription/{path}?Subscription={sub_id}")

    def sub_read(self, sub_id: int) -> dict:
        return self._call("GET", f"subscription?Subscription={sub_id}")

    def sub_delete(self, sub_id: int) -> None:
        try:
            self._call("DELETE", f"subscription?Subscription={sub_id}")
        except (urllib.error.URLError, OSError, ValueError):
            pass  # "no such subscription" on a fresh session is the good case


def values(reply: dict) -> dict:
    """The Values block of a Success reply, else {} — menus and unfitted
    nodes answer with Result=Error, which is data, not a fault."""
    if isinstance(reply, dict) and reply.get("Result") == "Success":
        v = reply.get("Values")
        return v if isinstance(v, dict) else {}
    return {}


def first_value(reply: dict):
    v = values(reply)
    return next(iter(v.values())) if v else None


# ── phase 1: detect + identify ────────────────────────────────────────────


def connect(api: Tsw) -> dict:
    """Blocks until /info answers. Prints each distinct failure once."""
    last = None
    while True:
        try:
            info = api.info()
            meta = info.get("Meta", info) if isinstance(info, dict) else {}
            print(f"connected: {meta.get('GameName', '?')} "
                  f"build {meta.get('GameBuildNumber', '?')} "
                  f"API {meta.get('APIVersion', '?')}")
            return info
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code}" + (" — key rejected (403); re-reading key file"
                                      if e.code == 403 else "")
        except (urllib.error.URLError, OSError, ValueError) as e:
            msg = f"no answer on {api.base} — game not up, or launched without -HTTPAPI ({e})"
        if msg != last:
            print(f"waiting: {msg}")
            last = msg
        time.sleep(3)


def identify(api: Tsw) -> None:
    loco = first_value(api.get("CurrentFormation/0.ObjectClass"))
    print(f"formation[0]:  {loco or '(no session loaded yet)'}")
    player = values(api.get("DriverAid.PlayerInfo"))
    if player:
        print(f"service:       {player.get('currentServiceName') or '(free roam?)'}")
        print(f"geo:           {player.get('geoLocation')}")
    tod = first_value(api.get("TimeOfDay.LocalTimeISO8601"))
    if tod is not None:
        print(f"sim time:      {tod}")


# ── phase 2: control discovery ────────────────────────────────────────────


def control_kind(endpoints: list) -> str:
    names = {e.get("Name") for e in endpoints if isinstance(e, dict)}
    if "Function.GetNotchCount" in names:
        return "lever"
    if "Property.bDefaultToPressed" in names:
        return "button"
    return "other"


def scan_controls(api: Tsw, dump: bool) -> dict[str, str]:
    """Walk /list/CurrentDrivableActor one level deep. Returns
    {role: control-node-name} for the levers the live loop will watch."""
    tree = api.list("CurrentDrivableActor")
    nodes = tree.get("Nodes") or []
    picked: dict[str, str] = {}

    print(f"\ncontrols on CurrentDrivableActor ({len(nodes)} child nodes):")
    for node in nodes:
        name = node.get("Name") if isinstance(node, dict) else None
        if not name:
            continue
        try:
            detail = api.list(f"CurrentDrivableActor/{name}")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        endpoints = detail.get("Endpoints") or []
        kind = control_kind(endpoints)
        writable = sorted(e["Name"] for e in endpoints
                          if isinstance(e, dict) and e.get("Writable"))
        if dump:
            print(f"  {name:40s} {kind:7s} "
                  f"writable: {', '.join(writable) if writable else '—'}")
        elif kind != "other":
            print(f"  {name:40s} {kind:7s} "
                  f"{'writable' if 'InputValue' in writable else 'read-only'}")

        for role, hints in LEVER_HINTS.items():
            if role not in picked and any(h.lower() in name.lower() for h in hints):
                picked[role] = name

    print("\nlive-loop lever picks (by name hint — verify against the cab):")
    for role in LEVER_HINTS:
        print(f"  {role:14s} -> {picked.get(role, '— not matched on this loco')}")
    return picked


# ── phase 3: live loop with freshness ─────────────────────────────────────

SUB_ID = 9109  # arbitrary but stable, so a stale one from a crashed probe
               # gets cleaned up by the next run's delete-then-create


def build_subscription(api: Tsw, levers: dict[str, str]) -> list[str]:
    api.sub_delete(SUB_ID)
    paths = [
        "CurrentDrivableActor.Function.HUD_GetSpeed",
        "CurrentDrivableActor.Function.HUD_GetBrakeGauge_1",
        "CurrentDrivableActor.Function.HUD_GetBrakeGauge_2",
        "CurrentDrivableActor.Function.HUD_GetAmmeter",
        "CurrentDrivableActor.Function.HUD_GetIsSlipping",
        "DriverAid.Data",
    ]
    paths += [f"CurrentDrivableActor/{node}.InputValue"
              for node in levers.values()]
    kept = []
    for p in paths:
        try:
            api.sub_add(SUB_ID, p)
            kept.append(p)
        except (urllib.error.URLError, OSError, ValueError):
            print(f"  subscription refused: {p}")
    return kept


def fmt_num(v, scale=1.0, suffix=""):
    if isinstance(v, (int, float)):
        return f"{v * scale:7.1f}{suffix}"
    return "      —"


def live_loop(api: Tsw, levers: dict[str, str], hz: float) -> None:
    """Print telemetry until Ctrl+C or the game goes away (returns on loss).

    Every line carries the two clocks the integration will need: `rtt` is
    this sample's round trip, `age` is time since the last GOOD sample —
    the number that turns into FRESH / AGING / STALE on the panel.
    """
    kept = build_subscription(api, levers)
    print(f"\nsubscribed {len(kept)} paths as id {SUB_ID}; "
          f"polling at {hz:g} Hz — Ctrl+C to stop\n")

    last_good = None
    misses = 0
    while True:
        t0 = time.time()
        try:
            reply = api.sub_read(SUB_ID)
            rtt_ms = (time.time() - t0) * 1000
        except (urllib.error.URLError, OSError, ValueError) as e:
            misses += 1
            age = time.time() - last_good if last_good else float("inf")
            print(f"{time.strftime('%H:%M:%S')}  STALE — no reply "
                  f"({e.__class__.__name__}), last good "
                  f"{age:.0f}s ago" if last_good else
                  f"{time.strftime('%H:%M:%S')}  STALE — no reply yet")
            if misses >= 5:
                print("connection lost — returning to detect phase")
                return
            time.sleep(max(1.0, 1.0 / hz))
            continue

        misses = 0
        by_path = {}
        for entry in reply.get("Entries") or []:
            if entry.get("NodeValid"):
                by_path[entry.get("Path")] = entry.get("Values") or {}

        if not by_path:
            # The API answers but the nodes do not: main menu, loading
            # screen, or walking mode. Distinct state, reported as such.
            print(f"{time.strftime('%H:%M:%S')}  API UP, NO VEHICLE "
                  f"(menus / loading / on foot)  rtt {rtt_ms:.0f}ms")
            time.sleep(1.0)
            continue

        last_good = time.time()
        speed_ms = next(iter(by_path.get(
            "CurrentDrivableActor.Function.HUD_GetSpeed", {}).values()), None)
        gauges = by_path.get("CurrentDrivableActor.Function.HUD_GetBrakeGauge_1", {})
        amps = next(iter(by_path.get(
            "CurrentDrivableActor.Function.HUD_GetAmmeter", {}).values()), None)
        slip = next(iter(by_path.get(
            "CurrentDrivableActor.Function.HUD_GetIsSlipping", {}).values()), None)
        aid = by_path.get("DriverAid.Data", {})

        parts = [
            time.strftime("%H:%M:%S"),
            f"spd{fmt_num(speed_ms, 2.23694, ' mph')}",
            f"limit{fmt_num(aid.get('speedLimit'), 2.23694, ' mph')}"
            if aid else "limit       —",
        ]
        for role, node in levers.items():
            v = next(iter(by_path.get(
                f"CurrentDrivableActor/{node}.InputValue", {}).values()), None)
            parts.append(f"{role.split('_')[0]}{fmt_num(v)}")
        if gauges:
            needles = "/".join(f"{v/1000:.0f}" for v in gauges.values()
                               if isinstance(v, (int, float)))
            parts.append(f"brake {needles} kPa")
        if isinstance(amps, (int, float)):
            parts.append(f"{amps:5.0f} A")
        if slip:
            parts.append("SLIP")
        parts.append(f"rtt {rtt_ms:3.0f}ms")
        print("  ".join(parts))

        time.sleep(max(0.0, 1.0 / hz - (time.time() - t0)))


# ── main ──────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--key", help="override CommAPIKey.txt discovery")
    ap.add_argument("--hz", type=float, default=2.0, help="live poll rate")
    ap.add_argument("--controls", action="store_true",
                    help="dump every control node with its writable endpoints, then exit")
    args = ap.parse_args()

    while True:  # detect → identify → live; falls back here on connection loss
        key, source = find_key(args.key)
        print(f"key: {source}")
        api = Tsw(args.host, args.port, key)
        connect(api)
        try:
            identify(api)
            levers = scan_controls(api, dump=args.controls)
            if args.controls:
                return
            live_loop(api, levers, args.hz)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print("key rejected mid-session (re-minted?) — re-reading")
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            print(f"lost the API ({e.__class__.__name__}) — waiting for it to return")
        except KeyboardInterrupt:
            api.sub_delete(SUB_ID)
            print("\nbye")
            return
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
