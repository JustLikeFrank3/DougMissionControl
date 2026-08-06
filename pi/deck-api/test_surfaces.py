#!/usr/bin/env python3
"""Surface episodes and playlist-mode selection. No Pi, no Grafana needed."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import deck_api as d

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)
def sf(): return d.DECK.state["surface"]

print("\n-- idle default --")
check("starts on evals", sf()["active"], "evals")

print("\n-- a boot episode shows DECK, then restores --")
d.DECK.open_episode("boot", "deck")
check("switched", sf()["active"], "deck")
check("remembered previous", sf()["previous"], "evals")
d.DECK.close_episode("boot")
check("restored", sf()["active"], "evals")
check("episode cleared", sf()["episode"], None)

print("\n-- manual wins and suppresses autos for the rest of the episode --")
d.DECK.open_episode("boot", "deck")
d.DECK.set_surface("sim", manual=True)
check("manual applied", sf()["active"], "sim")
d.DECK.set_surface("deck", manual=False)
check("auto suppressed", sf()["active"], "sim")
d.DECK.close_episode("boot")
check("no restore over a manual choice", sf()["active"], "sim")

print("\n-- the next episode starts clean --")
d.DECK.open_episode("sim", "sim")
check("auto works again", sf()["active"], "sim")
d.DECK.close_episode("sim")
check("restores to the manual choice", sf()["active"], "sim")

print("\n-- playlist mode: 'unknown' must never flap the display --")
check("windows", d.playlist_mode("windows", "linux"), "windows")
check("linux", d.playlist_mode("linux", "windows"), "linux")
check("booting keeps current", d.playlist_mode("booting", "windows"), "windows")
check("off keeps current", d.playlist_mode("off", "windows"), "windows")
check("unknown keeps current", d.playlist_mode("unknown", "linux"), "linux")
check("nothing chosen yet -> linux", d.playlist_mode("off", None), "linux")

print("\n-- an unknown surface name is ignored --")
d.DECK.set_surface("bogus", manual=True)
check("ignored", sf()["active"], "sim")

print()
sys.exit(1 if fails else 0)
