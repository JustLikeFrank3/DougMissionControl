#!/usr/bin/env python3
"""Navigation route validation. No Pi or simulator required."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import deck_api as d


route = d._clean_waypoints([
    {"name": "BASE", "lat": 41.9, "lon": -87.9, "altitude_ft": 5000},
    {"name": "FINAL", "lat": 41.8, "lon": -87.8},
])
assert route == [
    {"name": "BASE", "lat": 41.9, "lon": -87.9, "altitude_ft": 5000.0},
    {"name": "FINAL", "lat": 41.8, "lon": -87.8},
]
assert d._clean_waypoints([
    {"name": "BAD", "lat": 41.9, "lon": -87.9, "altitude_ft": 70000},
]) is None

print("deck-api navigation route tests passed")