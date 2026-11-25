#!/usr/bin/env python3
"""Where the Windows boot is seen and dialled: the LAN address while that
answers, its own address on the direct cable (WIN_DIRECT_IP) when only that
does. The workstation's LAN port has no cable, so the second is the normal
case. Every probe and every request is stood in for."""
import os, pathlib, sys, urllib.parse
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ["DEFAULT_SURFACE"] = "evals"
import deck_api as d

LAN, LINUX_LINK, WIN_LINK = "192.0.2.50", "198.51.100.1", "198.51.100.3"
d.WS_LAN, d.LINUX_PROBE_IP, d.WIN_DIRECT_IP = LAN, LINUX_LINK, WIN_LINK
d.WIN_METRICS_URL = ""
d._linux_at[0] = d._win_at[0] = LAN
d.LINUX_MEDIA_TOKEN = d.LINUX_AGENT_TOKEN = d.SIM_TOKEN = d.AGENT_TOKEN = "a-token"

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

UP = set()
ASKED = []
def host_of(url): return urllib.parse.urlsplit(url).hostname

class Reply:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1): return b'{"monitors": [], "active": true}'

def urlopen(req, timeout=None):
    host = host_of(req if isinstance(req, str) else req.full_url)
    ASKED.append(host)
    if host not in UP:
        raise OSError("no route to host")
    return Reply()

d.urllib.request.urlopen = urlopen
d.tcp_ok = lambda host, port, timeout=2.0: host in UP or host == ""
d.pingable = lambda host: host in UP

def boot(os_now):
    d.DECK.mutate(lambda s: s["workstation"].__setitem__("os", os_now))

def dialled(fn):
    del ASKED[:]
    got = fn()
    return list(ASKED), got

def os_seen():
    d.poll_once(None)
    return d.DECK.state["workstation"]["os"], d.DECK.state["workstation"]["ip"]

# The rest of a probe cycle reads other machines; none of that is under test.
for name in ("fetch_telemetry", "fetch_sim_link", "fetch_mini", "fetch_mobile",
             "fetch_douglas", "fetch_voice", "who_is_gone", "read_pi_metrics",
             "refresh_evals"):
    setattr(d, name, (lambda *a, **k: ({}, None)) if name == "fetch_telemetry"
            else (lambda *a, **k: {}))

print("\n-- only the direct cable answers (the LAN port has no cable) --")
UP.clear(); UP.add(WIN_LINK)
check("windows is seen, its agent too", d.windows_seen(), (True, True))
check("its agents are on the cable", d.win_host(), WIN_LINK)
check("a probe cycle calls it windows, at that address", os_seen(), ("windows", WIN_LINK))
boot("windows")
check("the screens are asked on the cable", dialled(lambda: d._monitor_url("GET"))[0], [WIN_LINK])
check("the track", host_of(d._media_url("/media", "windows")), WIN_LINK)
check("the bars", dialled(d.audio_spectrum)[0], [WIN_LINK])
check("the exporter", host_of(d.win_metrics_url()), WIN_LINK)
check("launch", dialled(d.fire_launch), ([WIN_LINK], (True, "launching")))
check("the off button", dialled(d.fire_shutdown), ([WIN_LINK], (True, "powering off")))

print("\n-- the LAN address answers: it is preferred --")
UP.add(LAN)
check("windows is seen", d.windows_seen(), (True, True))
check("its agents are on the LAN address", d.win_host(), LAN)
check("the screens are asked there", dialled(lambda: d._monitor_url("GET"))[0], [LAN])

print("\n-- the Linux link answering is not Windows --")
UP.clear(); UP.add(LINUX_LINK)
check("windows is not seen", d.windows_seen(), (False, False))
check("a probe cycle calls it linux", os_seen()[0], "linux")

print("\n-- Windows' network is up before its agents (booting) --")
UP.clear()
d.tcp_ok = lambda host, port, timeout=2.0: False
d.pingable = lambda host: host == WIN_LINK
check("a ping on the cable reads booting, not off", os_seen()[0], "booting")
d.pingable = lambda host: False
check("nothing at all reads off", os_seen()[0], "off")
d.tcp_ok = lambda host, port, timeout=2.0: host in UP or host == ""
d.pingable = lambda host: host in UP

print("\n-- no direct address in the settings --")
d.WIN_DIRECT_IP = ""
d._win_at[0] = LAN
UP.clear(); UP.add(WIN_LINK)
check("windows is not seen by an address nobody named", d.windows_seen(), (False, False))
check("the address stays the LAN one", d.win_host(), LAN)

print()
sys.exit(1 if fails else 0)
