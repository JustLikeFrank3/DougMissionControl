#!/usr/bin/env python3
"""douglas-memory finding memories by their sense, against a stand-in for the
embedding service: a small HTTP server at a Unix socket that answers as
llama-server does, with vectors made from a list of which words mean the same
thing. It tests the plumbing (what gets a vector, when, and what can never be
found by one), not how good a real model is: that is embed-eval.py, on
Douglas.

    python3 douglas/test_douglas_memory_sense.py
"""

import http.server
import importlib.util
import json
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import zlib

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))


root = tempfile.mkdtemp(prefix="douglas-sense-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
SOCK = os.path.join(root, "embed.sock")
os.environ.update(MEMORY_DB=os.path.join(root, "memory.db"), MEMORY_RUN=os.path.join(root, "run"),
                  EMBED_SOCKET=SOCK, EMBED_DIM="64", EMBED_MIN="0.5", EMBED_SAME="0.95",
                  MEMORY_OBSERVATIONS="1", MEMORY_OBS_HOURS="1")
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("douglas_memory", os.path.join(here, "douglas-memory.py"))
mem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem)
LOG = []
mem.log = LOG.append

# Which words mean what: one dimension a meaning. A word in none of them
# means nothing here (it lands in the last dimension, weakly).
MEANINGS = [
    "coffee espresso latte drink drinks caffeine brew",
    "morning mornings breakfast wake dawn",
    "dog puppy hound pet terrier",
    "car vehicle drive truck sedan",
    "birthday born anniversary",
    "sister sibling brother",
    "garage shed workshop",
    "allergic allergy peanuts nuts",
    "vacation holiday trip travel",
    "bike bicycle cycling ride",
    "doctor dentist appointment clinic",
    "porch package delivery parcel",
]
STATE = {"model": "fake-embed-v1.gguf", "delay": 0.0, "down": False, "asked": 0}


def fake_vector(text: str) -> list[float]:
    v = [0.0] * 64
    for w in re.findall(r"[a-z]+", text.lower()):
        hit = [n for n, m in enumerate(MEANINGS) if w in m.split()]
        for n in hit:
            v[n] += 1.0
        if not hit and len(w) > 3 and w not in ("title", "text", "task", "search", "result", "query"):
            v[12 + zlib.crc32(w.encode()) % 52] += 0.3       # a word of no meaning here: somewhere of its own
    return v if any(v) else [0.0] * 63 + [1.0]


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if STATE["down"]:
            self.send_response(503)
            self.end_headers()
            return
        self.reply({"data": [{"id": STATE["model"]}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STATE["asked"] += 1
        time.sleep(STATE["delay"])
        if STATE["down"]:
            self.send_response(503)
            self.end_headers()
            return
        self.reply({"data": [{"index": n, "embedding": fake_vector(t)} for n, t in enumerate(body["input"])]})


class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                            # a caller that gave up waiting

    def get_request(self):
        req, _ = super().get_request()
        return req, ("local", 0)


srv = UnixHTTP(SOCK, Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()

store = mem.Store(mem.DB)
desk = mem.Desk(store)


def a(**req):
    return desk.call("assistant", req)


def o(**req):
    return desk.call("owner", req)


def texts(got):
    return [m["text"] for m in got.get("memories", got) if isinstance(m, dict)] if isinstance(got, dict) \
        else [m["text"] for m in got]


def vectors_of(i):
    return store.db.execute("SELECT COUNT(*) FROM embeddings WHERE memory = ?", (i,)).fetchone()[0]


print("== a vector for each memory, made after it is kept")
coffee = a(op="remember", kind="preference", subject="coffee", text="She takes her coffee black.", source="said")["id"]
dog = a(op="remember", kind="fact", subject="the dog", text="Biscuit is a terrier, nine years old.", source="said")["id"]
plate = a(op="remember", kind="fact", subject="number plate", text="The van's registration is RX-7734.",
          source="said")["id"]
check("kept at once, with no vector yet: nothing waits for the model", (vectors_of(coffee), vectors_of(dog)), (0, 0))
check("the background makes them", (store.embed(), vectors_of(coffee), vectors_of(plate)), (3, 1, 1))
check("and none twice", store.embed(), 0)
check("the vector is of the model the service says it has, cut to length",
      store.db.execute("SELECT model, dim FROM embeddings WHERE memory = ?", (coffee,)).fetchone()[:],
      (store.embedder.known, 64))
check("which is named for the model, the length and the way it is asked",
      store.embedder.known.split("|")[:2], ["fake-embed-v1.gguf", "64"])

print("== found by its sense, and still by its letters")
check("a question that shares no word with the memory finds it",
      texts(a(op="recall", text="what does she like to drink at breakfast")), ["She takes her coffee black."])
check("so does one about the pet", texts(a(op="recall", text="how old is our puppy")),
      ["Biscuit is a terrier, nine years old."])
check("an identifier is found by its letters, as it always was", texts(a(op="recall", text="whose is RX-7734")),
      ["The van's registration is RX-7734."])
check("a name too", texts(a(op="recall", text="tell me about Biscuit"))[:1], ["Biscuit is a terrier, nine years old."])
check("something unrelated finds nothing", texts(a(op="recall", text="when is the dentist appointment")), [])
check("what both ways find comes first", texts(a(op="recall", text="coffee in the morning"))[0],
      "She takes her coffee black.")

print("== a vector never outlives the wording it was made of")
o(op="correct", id=coffee, text="She has given up caffeine and drinks herbal tea.")
check("the owner's correction takes the old vector with it, in the same change", vectors_of(coffee), 0)
store.embed()
check("and a new one is made of the new words", vectors_of(coffee), 1)
new = a(op="remember", kind="fact", subject="the dog", text="Biscuit turned ten in March.", source="said")
check("a memory replaced loses its vector", (new["what"], vectors_of(dog)), ("replaced", 0))
store.embed()
check("and the old wording is not found by sense", texts(a(op="recall", text="how old is our puppy")),
      ["Biscuit turned ten in March."])
o(op="delete", id=plate)
check("a memory deleted loses its vector", vectors_of(plate), 0)
check("and is found neither way", texts(a(op="recall", text="whose is RX-7734 vehicle")), [])
o(op="restore", id=plate)
store.embed()
check("brought back, it is given one again", vectors_of(plate), 1)
o(op="purge", id=plate)
check("purged, the vector is gone with it", store.db.execute("SELECT COUNT(*) FROM embeddings WHERE memory = ?",
                                                              (plate,)).fetchone()[0], 0)
sis = a(op="remember", kind="fact", subject="sister", text="Her sister lives in Leeds.", source="inferred")["id"]
store.embed()
a(op="forget", ids=[sis])
check("forgotten by Douglas, it loses its vector", vectors_of(sis), 0)

print("== what is being embedded may change meanwhile")
car = a(op="remember", kind="fact", subject="car", text="The car is a blue sedan.", source="said")["id"]
orig = store.embedder.vectors


def slow(texts_, timeout):
    o(op="correct", id=car, text="The car was sold in June.")       # while the model is busy
    return orig(texts_, timeout)


store.embedder.vectors = slow
store.embed()
store.embedder.vectors = orig
check("a vector of words the memory no longer says is not kept", vectors_of(car), 0)
store.embed()
check("the next round makes one of what it says now", vectors_of(car), 1)

print("== the model changes")
STATE["model"] = "fake-embed-v2.gguf"
store.embedder.known = None
check("nothing made by the old model is used", texts(a(op="recall", text="what does she brew at dawn")), [])
made = store.embed()
check("every memory is embedded again by the new one",
      (made, store.db.execute("SELECT COUNT(DISTINCT model) FROM embeddings").fetchone()[0]),
      (store.db.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0], 1))
check("and is found by sense again", texts(a(op="recall", text="what does she brew at dawn"))[:1],
      ["She has given up caffeine and drinks herbal tea."])
check("the owner can have them all made again", (o(op="reindex")["dropped"] > 0, store.embed() > 0), (True, True))

print("== without the embedding service, the words only")
STATE["down"] = True
store.embedder.cache.clear()
began = time.time()
check("a question by sense finds nothing, and says nothing is wrong",
      (a(op="recall", text="what does she brew at dawn")["ok"],
       texts(a(op="recall", text="what does she brew at dawn"))), (True, []))
check("a question in the memory's words is answered as before",
      texts(a(op="recall", text="herbal tea")), ["She has given up caffeine and drinks herbal tea."])
check("and after the first try it is not waited for", time.time() - began < 1.5, True)
check("a memory kept meanwhile is kept", a(op="remember", kind="fact", subject="bike",
                                            text="The bike is in the shed.", source="said")["what"], "kept")
check("and has no vector yet", store.embed(), 0)
STATE["down"] = False
store.embedder.down_until = 0
check("when the service is back, it is given one", store.embed(), 1)
check("the journal says the service went away, once", sum("not answering" in x for x in LOG), 1)

print("== a slow model does not hold a recall up for long")
STATE["delay"] = 2.0
store.embedder.cache.clear()
began = time.time()
got = a(op="recall", text="herbal tea in the mornings")
check("the recall goes on without the question's vector", (time.time() - began < 1.0, texts(got)[:1]),
      (True, ["She has given up caffeine and drinks herbal tea."]))
STATE["delay"] = 0.0
time.sleep(2.2)
store.embedder.down_until = 0
store.embedder.known = None

print("== reads do not wait for writes")
held = threading.Event()


def writer():
    with store.lock:                    # a change that takes its time
        held.set()
        time.sleep(1.5)


threading.Thread(target=writer).start()
held.wait()
began = time.time()
got = a(op="recall", text="what does she like to drink")
check("a recall, by words and by sense, while a slow change holds the writer",
      (time.time() - began < 0.5, texts(got)[:1]), (True, ["She has given up caffeine and drinks herbal tea."]))
time.sleep(1.6)

print("== two memories that say the same: the firmer word")
o(op="add", kind="fact", subject="peanut allergy", text="He is allergic to peanuts.")
a(op="remember", kind="lesson", subject="nuts", text="He has a peanut allergy.", source="inferred")
store.embed()
got = a(op="recall", text="does he have any allergies to nuts")["memories"]
check("handed out once, the owner's", [(m["text"], m["source"]) for m in got], [("He is allergic to peanuts.", "owner")])
check("the other is still kept", o(op="list", q="peanut allergy")["total"], 1 + 1)

print("== categories")
o(op="add", kind="place", subject="garage", text="The garage workshop has the bike tools.")
store.embed()
check("each memory says what category it is in", {m["kind"]: m["category"] for m in a(op="recall", text="bike ride")["memories"]},
      {"fact": "semantic", "place": "spatial"})
check("and recall can be asked of one", [m["kind"] for m in a(op="recall", text="bike ride", categories=["spatial"])["memories"]],
      ["place"])
check("a place is among what he is surest of", "place" in [m["kind"] for m in a(op="known")["memories"]], True)

print("== observations: what a camera saw")
check("the owner's word is never an observation",
      o(op="add", kind="observation", subject="porch", text="A parcel is on the porch.")["text"],
      "An observation is what was seen, and nothing else.")
check("nor is anything Douglas was told",
      a(op="remember", kind="observation", subject="porch", text="A parcel is on the porch.", source="said")["ok"], False)
obs = a(op="remember", kind="observation", subject="porch", text="A parcel is on the porch.", source="observed",
        confidence=0.95)
got = o(op="get", id=obs["id"])
check("kept as seen, at most as sure as a guess, and running out",
      (obs["what"], got["source"], got["confidence"], 3500 < got["expires"] - time.time() <= 3600), ("kept", "observed", 0.6, True))
store.embed()
m = a(op="recall", text="is there a package delivery")["memories"][0]
check("handed out with when it was seen, and never as how things are now",
      (m["text"], m["current"], m["category"], "observed" in m), ("A parcel is on the porch.", False, "observational", True))
fact = o(op="add", kind="fact", subject="porch", text="Parcels go in the porch box.")
obs2 = a(op="remember", kind="observation", subject="porch", text="The porch is empty.", source="observed")
check("a new observation replaces an older one of the same thing", o(op="get", id=obs["id"])["status"], "superseded")
check("and never a fact", o(op="get", id=fact["id"])["status"], "active")
store.embed()
store.db.execute("UPDATE memories SET expires = ? WHERE id = ?", (time.time() - 1, obs2["id"]))
check("run out, it is not handed out even before it is let go",
      "The porch is empty." in texts(a(op="recall", text="porch parcel delivery")), False)
check("and is let go by the clock, losing its vector", (store.expire(), o(op="get", id=obs2["id"])["status"],
                                                        vectors_of(obs2["id"])), (1, "deleted", 0))
check("nothing can be made an observation by the owner",
      o(op="correct", id=fact["id"], kind="observation")["text"], "Only a camera makes an observation: the owner's word is a fact.")
mem.OBSERVATIONS = False
check("and where it is not switched on, none is kept",
      a(op="remember", kind="observation", subject="yard", text="A fox is in the yard.", source="observed")["text"],
      "What the cameras see is not kept in memory on this machine.")

print("== a reader reads, and only what is active")
before = o(op="get", id=coffee)["uses"]
got = desk.call("reader", {"op": "recall", "text": "what does she like to drink"})
check("by sense too, and nothing marked as used", (texts(got)[:1], (store.flush(), o(op="get", id=coffee)["uses"])[1]),
      (["She has given up caffeine and drinks herbal tea."], before))
check("never a deleted memory", "Her sister lives in Leeds." in texts(desk.call("reader", {"op": "recall",
                                                                                          "text": "sibling sister"})), False)

print("== how it stands")
st = o(op="stats")["by_sense"]
check("the owner is told whether memories are found by sense, and how many",
      (st["on"], st["answering"], st["embedded"] == store.db.execute(
          "SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]), (True, True, True))
store.flush()
store.cut()
fine, said = mem.sound()
check("the database is sound, with its vectors counted", (fine, "vectors (by 1 model)" in said), (True, True))
store.db.execute("INSERT INTO embeddings (memory, model, dim, vec, at) VALUES (?, 'x', 1, x'00000000', 0)", (sis,))
check("a vector of a memory that is not active is not passed over as sound", mem.sound()[0], False)

srv.shutdown()
store.db.close()
shutil.rmtree(root, ignore_errors=True)
print("douglas-memory sense tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
