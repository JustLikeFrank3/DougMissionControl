#!/usr/bin/env python3
"""The laptop's card: a name that is slow to look up does not make a machine
that is answering read as away. No Pi and no laptop needed."""
import os, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ["DEFAULT_SURFACE"] = "evals"
import deck_api as d

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

d.ADDR_WAIT_S = 0.2
looked, pinged, slow, answer = [], [], [0.0], ["192.168.68.63"]
def lookup(host):
    looked.append(host)
    time.sleep(slow[0])
    if not answer[0]:
        raise OSError("no such name")
    return answer[0]
d.socket.gethostbyname = lookup
up_at = {"192.168.68.63"}
d.pingable = lambda host: (pinged.append(host), host in up_at)[1]
d.tcp_open = lambda host, port, timeout=1.5: host in up_at and port == 22

print("-- a name that answers --")
m = d.fetch_mobile()
check("the laptop is up, by the address its name gave", (m["up"], m["ssh"], m["screen"], pinged), (True, True, False, ["192.168.68.63"]))
check("and is still called by its name", m["host"], d.MOBILE_HOST)
d.fetch_mobile(); d.fetch_mobile()
check("the name is looked up once, not on every look", len(looked), 1)

print("-- a name that is slow to answer --")
d._addr_kept.clear(); looked.clear(); pinged.clear()
slow[0] = 0.6
t0 = time.time()
m = d.fetch_mobile()
check("the look does not wait for it", (m["up"], time.time() - t0 < 0.5, pinged), (False, True, []))
d.fetch_mobile()
check("nor ask twice while one is out", len(looked), 1)
time.sleep(0.7)
check("and once it has come, the laptop is up", d.fetch_mobile()["up"], True)
slow[0] = 5.0
t0 = time.time()
ups = [d.fetch_mobile()["up"] for _ in range(3)]
check("and stays up without another lookup, however slow one would be", (ups, time.time() - t0 < 0.5, len(looked)),
      ([True, True, True], True, 1))

print("-- the laptop moves --")
slow[0] = 0.0
up_at.clear(); up_at.add("192.168.68.99"); answer[0] = "192.168.68.99"
check("the old address stops answering: away, this once", d.fetch_mobile()["up"], False)
check("and the name is asked again, and found", (d.fetch_mobile()["up"], pinged[-1]), (True, "192.168.68.99"))

print("-- the laptop is gone --")
up_at.clear(); answer[0] = ""
check("away", d.fetch_mobile()["up"], False)
check("and still away, with the old address tried", (d.fetch_mobile()["up"], pinged[-1]), (False, "192.168.68.99"))
answer[0] = "192.168.68.99"; up_at.add("192.168.68.99")
d.fetch_mobile()
check("back when it answers again", d.fetch_mobile()["up"], True)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
