#!/usr/bin/env python3
"""Where the Linux boot's agents are dialled: the LAN address while that
answers, the point-to-point link when only that does. Every probe and every
request is stood in for; no Pi and no workstation needed to run this."""
import os, pathlib, sys, urllib.parse
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ["DEFAULT_SURFACE"] = "evals"
import deck_api as d

LAN, LINK = "192.0.2.50", "198.51.100.1"      # addresses kept for documentation
d.WS_LAN, d.LINUX_PROBE_IP = LAN, LINK
d._linux_at[0] = d._win_at[0] = LAN
d.LINUX_MEDIA_TOKEN = d.LINUX_AGENT_TOKEN = d.SIM_TOKEN = d.AGENT_TOKEN = "a-token"

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok: fails.append(label)

UP = set()        # the addresses that answer, in this file's world
ASKED = []        # every address something was requested from, in order
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
# To a socket an empty host is this machine, and a Pi answers on 22. The
# stand-in says so too, or a link nobody named would pass for one that answers.
d.tcp_ok = lambda host, port, timeout=2.0: host in UP or host == ""
d.pingable = lambda host: host in UP

def boot(os_now):
    d.DECK.mutate(lambda s: s["workstation"].__setitem__("os", os_now))

def dialled(fn):
    """The addresses `fn` asked, and what it returned."""
    del ASKED[:]
    got = fn()
    return list(ASKED), got

boot("linux")

print("\n-- the LAN address answers: nothing changes --")
UP.clear(); UP.update({LAN, LINK})
check("linux is seen", d.linux_seen(), True)
check("its agents are on the LAN address", d.linux_host(), LAN)
check("the screens are asked there", dialled(lambda: d._monitor_url("GET"))[0], [LAN])

print("\n-- only the point-to-point link answers --")
UP.clear(); UP.add(LINK)
check("the screens get no answer before the next probe", dialled(lambda: d._monitor_url("GET")), ([LAN], None))
check("linux is still seen", d.linux_seen(), True)
check("its agents are on the link", d.linux_host(), LINK)
asked, got = dialled(lambda: d._monitor_url("GET"))
check("the screens are asked on the link", asked, [LINK])
check("and answer", got, {"monitors": [], "active": True})
check("a switch of input goes the same way", dialled(lambda: d._monitor_url("POST", {"input": "dp1", "index": 0}))[0], [LINK])
check("the track", host_of(d._media_url("/media", "linux")), LINK)
check("its buttons", host_of(d._media_url("/media/command", "linux")), LINK)
check("its picture", host_of(d._media_url("/media/art", "linux")), LINK)
asked, got = dialled(d.audio_spectrum)
check("the bars", (asked, got.get("active")), ([LINK], True))
asked, got = dialled(d.fire_shutdown)
check("the off button", (asked, got), ([LINK], (True, "powering off")))

print("\n-- the LAN address answers again --")
UP.add(LAN)
check("linux is seen", d.linux_seen(), True)
check("its agents are back on the LAN address", d.linux_host(), LAN)
check("the screens are asked there", dialled(lambda: d._monitor_url("GET"))[0], [LAN])

print("\n-- nothing answers --")
UP.clear()
check("linux is not seen", d.linux_seen(), False)
check("the address is left as it was", d.linux_host(), LAN)

print("\n-- no point-to-point link in the settings --")
d.LINUX_PROBE_IP = ""
UP.clear(); UP.add(LINK)
check("linux is not seen by a link nobody named", d.linux_seen(), False)
check("the address stays the LAN one", d.linux_host(), LAN)
d.LINUX_PROBE_IP = LINK

print("\n-- Windows is not dialled on the Linux link --")
UP.clear(); UP.add(LINK)
d.linux_seen()                                   # the link is where Linux last answered
check("(the link is remembered)", d.linux_host(), LINK)
boot("windows")
check("the screens ask the LAN address", dialled(lambda: d._monitor_url("GET"))[0], [LAN])
check("the track too", host_of(d._media_url("/media", "windows")), LAN)
check("and the bars", dialled(d.audio_spectrum)[0], [LAN])
check("and the off button", dialled(d.fire_shutdown)[0], [LAN])

print("\n-- with the machine off, nothing is dialled --")
boot("off")
check("the screens", dialled(lambda: d._monitor_url("GET")), ([], None))
check("the track", d._media_url("/media", "off"), None)

print()
sys.exit(1 if fails else 0)
