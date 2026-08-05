#!/usr/bin/env python3
"""Exporter parsing + canonicalisation, against payloads shaped like the two
exporters that already exist. No Pi and no exporter needed to run this."""
import sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import deck_api as d

WIN = """# HELP gpu_temperature_celsius GPU core temperature
# TYPE gpu_temperature_celsius gauge
gpu_temperature_celsius{gpu="0",name="RTX 4080"} 61.0
gpu_utilization_percent{gpu="0"} 38
gpu_memory_used_bytes{gpu="0"} 4509715660
gpu_memory_total_bytes{gpu="0"} 17179869184
gpu_fan_percent{gpu="0"} 42
"""
LINUX = """gpu_memory_used_bytes 8589934592
gpu_memory_total_bytes 17179869184
gpu_temp_c 54.5
ollama_models_loaded 1
"""
JUNK = "not a metric line\n# comment\nbroken{ 1\nfoo_bar NaN\n"

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

print("\n-- windows exporter --")
w = d.canonicalise(d.parse_prom_text(WIN))
check("gpu temp", w.get("gpu_temp_c"), 61.0)
check("gpu util", w.get("gpu_util_pct"), 38.0)
check("vram used", w.get("vram_used_bytes"), 4509715660.0)
check("vram pct derived", w.get("vram_pct"), 26.2)

print("\n-- linux exporter, different metric names --")
l = d.canonicalise(d.parse_prom_text(LINUX))
check("gpu temp", l.get("gpu_temp_c"), 54.5)
check("vram pct derived", l.get("vram_pct"), 50.0)
check("absent util stays absent", "gpu_util_pct" in l, False)

print("\n-- malformed input must not raise --")
j = d.canonicalise(d.parse_prom_text(JUNK))
check("returns a dict", isinstance(j, dict), True)
check("NaN dropped", j, {})

print("\n-- no OS up is a real gap, not a zero --")
t, s = d.fetch_telemetry("off")
check("no values", t, {})
check("no source", s, None)

print("\n-- pi cpu%% needs two samples --")
check("first call None", d.pi_cpu_pct(), None)
time.sleep(0.2)
v = d.pi_cpu_pct()
check("second call in range", isinstance(v, float) and 0 <= v <= 100, True)

print()
sys.exit(1 if fails else 0)
