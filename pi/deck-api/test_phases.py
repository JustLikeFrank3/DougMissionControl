"""Drive handle_log_line with the exact strings flightsim-boot.sh logs."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import deck_api as d

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok: fails.append(label)

def phase(): return d.DECK.state["boot"]["phase"]
def inflight(): return d.DECK.state["boot"]["in_flight"]
def result(): return d.DECK.state["boot"]["result"]

print("\n-- cold boot to Windows (WOL, then chained) --")
d.DECK.begin("windows", "sim")
check("POST sets TRIGGER", phase(), 1)
d.handle_log_line("[windows] trigger received — probing workstation state")
check("trigger -> PROBE", phase(), 2)
d.handle_log_line("[windows] no response — sending WOL to AA:BB:CC:DD:EE:FF")
check("WOL -> KICK", phase(), 3)
d.handle_log_line("[windows] host answers ping but no exporter yet (mid-boot?) — watching")
check("ping -> PING", phase(), 4)
d.handle_log_line("[windows] other OS came up after WOL — requesting reboot into windows")
check("chained reboot stays >= KICK", phase(), 4)
d.handle_log_line("[windows] windows is up — deck online")
check("deck online -> OS UP", phase(), 5)
# A windows run with a launch intent now holds the door for the greeting's
# callbacks instead of closing at OS UP.
check("run stays open awaiting callbacks", inflight(), True)
check("awaiting stamp set", d.DECK.state["boot"]["os_up_at"] is not None, True)
d.AGENT_TOKEN = "test-token"      # the callbacks authenticate with this
check("bad token rejected", d.greeting_phase({"token": "nope", "phase": "logon"})[0], 403)
d.greeting_phase({"token": "test-token", "phase": "logon"})
check("greeting logon -> LOGON", phase(), 6)
d.greeting_phase({"token": "test-token", "phase": "launched"})
check("launched closes the run", inflight(), False)
check("result ok", result(), "ok")
check("last_boot recorded", d.DECK.state["last_boot"]["reached_phase"], 7)

print("\n-- already up --")
d.DECK.begin("windows", "plain")
d.handle_log_line("[windows] windows already up — nothing to do")
check("short-circuits", inflight(), False)
check("result ok", result(), "ok")

print("\n-- timeout --")
d.DECK.begin("linux", "code")
d.handle_log_line("[linux] trigger received — probing workstation state")
d.handle_log_line("[linux] gave up after 300s — machine not in linux (check BIOS WOL / logon task)")
check("marked failed", result(), "failed")
check("not in flight", inflight(), False)

print("\n-- voice trigger adopted (fauxmo not repointed) --")
d.handle_log_line("[windows] trigger received — probing workstation state")
check("adopted", inflight(), True)
check("target parsed", d.DECK.state["boot"]["target"], "windows")

print("\n-- duplicate + warn are events, not phase changes --")
before = phase()
d.handle_log_line("[windows] already running — ignoring duplicate trigger")
d.handle_log_line("[windows] WARN: reboot request failed")
check("phase unchanged", phase(), before)
levels = [e["level"] for e in d.DECK.state["events"][:2]]
check("both warn", levels, ["warn", "warn"])

print("\n-- phases never go backwards --")
d.handle_log_line("[windows] trigger received — probing workstation state")
check("stays at PROBE not below", phase() >= 2, True)

print()
sys.exit(1 if fails else 0)
