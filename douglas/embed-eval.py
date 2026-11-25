#!/usr/bin/env python3
"""embed-eval: how well an embedding model finds Douglas's memories, measured
before it is chosen, and the thresholds douglas-memory uses with it.

Runs douglas-memory's own recall on a temporary database of MADE-UP memories
(nothing of anybody's real memory is read), with the words only, and with
words and sense together at several thresholds, against a llama-server
serving an embedding model at a Unix socket.

    python3 -I embed-eval.py SOCKET [--query TEMPLATE --doc TEMPLATE] [--dim N] [--pid PID]

What it says:
    found@1, found@3, MRR   for questions whose memory shares no word with them
                            (paraphrase) and for names and identifiers (exact)
    noise                   memories handed out for questions nothing bears on
    owner first             an owner's correction above an older inference
    similarities            of near-duplicates and of near-but-different pairs,
                            for EMBED_SAME; of right answers and of unrelated
                            questions' best match, for EMBED_MIN
    speed                   a question's vector (p50, p95), memories a second
"""

import argparse
import importlib.util
import os
import shutil
import statistics
import sys
import tempfile
import time

ap = argparse.ArgumentParser()
ap.add_argument("socket")
ap.add_argument("--query", default="task: search result | query: {text}")
ap.add_argument("--doc", default="title: {subject} | text: {text}")
ap.add_argument("--dim", type=int, default=768)
ap.add_argument("--pid", type=int, default=0, help="the llama-server's pid, to say how much memory it holds")
args = ap.parse_args()

root = tempfile.mkdtemp(prefix="embed-eval-")
os.environ.update(MEMORY_DB=os.path.join(root, "m.db"), EMBED_SOCKET="", EMBED_DIM=str(args.dim))
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("douglas_memory", os.path.join(here, "douglas-memory.py"))
mem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem)
mem.log = lambda m: None

# (label, kind, subject, text, source)
MEMORIES = [
    ("coffee", "preference", "coffee", "He takes his coffee black with no sugar.", "said"),
    ("allergy", "fact", "allergy", "He is allergic to shellfish.", "said"),
    ("sister", "fact", "sister", "His sister Marguerite lives in Halifax.", "said"),
    ("dog", "fact", "the dog", "The dog, Biscuit, is a nine-year-old beagle.", "said"),
    ("car", "fact", "car", "The car is a 2019 Subaru Outback, plate KJX-4471.", "said"),
    ("holiday", "decision", "august holiday", "We decided to spend the August holiday in Portugal.", "said"),
    ("dentist", "task", "dentist", "Book a dentist cleaning before the end of January.", "said"),
    ("backup", "lesson", "backup stick", "Always check the backup stick is mounted before running the nightly copy.",
     "inferred"),
    ("node1", "fleet", "node1", "node1 drives the cockpit panel and serves the deck API on port 8088.", "observed"),
    ("tools", "place", "bike tools", "The bike tools hang on the pegboard in the garage.", "said"),
    ("thermostat", "place", "upstairs thermostat", "The upstairs thermostat is in the hallway outside the bathroom.",
     "said"),
    ("jazz", "preference", "music", "He likes to listen to jazz while cooking.", "said"),
    ("job", "fact", "work", "He works as a software engineer at a bank.", "said"),
    ("greenhouse", "project", "greenhouse", "He is building a greenhouse; the frame is up and the glazing is next.",
     "said"),
    ("heating", "preference", "house temperature", "He keeps the house at 68 degrees in winter.", "said"),
    ("mom", "fact", "mother birthday", "His mother's birthday is April 12.", "said"),
    ("sis-bday", "fact", "Marguerite birthday", "Marguerite's birthday is June 3.", "owner"),
    ("throttle", "summary", "sim throttle", "Talked through the flight simulator throttle quadrant wiring; the second "
     "lever reads backwards.", "inferred"),
    ("oom", "lesson", "image model memory", "When the image model runs out of memory, stop the drawing service first.",
     "inferred"),
    ("briefing", "preference", "morning briefing", "He doesn't want sports scores in the morning briefing.", "said"),
    ("iot", "fact", "IoT network", "The IoT devices use the 2.4 GHz network, not the main mesh.", "said"),
    ("diet", "preference", "red meat", "He's trying to eat less red meat.", "said"),
    ("gp", "fact", "doctor", "His GP is Dr. Okafor at the Riverside clinic.", "said"),
    ("insurance", "task", "home insurance", "Renew the home insurance policy before March 15.", "said"),
    ("bike", "fact", "road bike", "His road bike is a steel Surly with a 54 cm frame.", "said"),
    ("spare", "place", "spare house key", "The spare house key is with the neighbour at number 14.", "said"),
    ("tv", "decision", "bedroom TV", "We chose not to put a TV in the bedroom.", "said"),
    ("douglas", "fleet", "Douglas", "Douglas the GEEKOM box runs the language model and the voice pipeline.",
     "observed"),
    ("books", "preference", "reading", "He reads science fiction before bed, mostly Le Guin and Banks.", "said"),
    ("fig", "fact", "fiddle-leaf fig", "The fiddle-leaf fig needs water every ten days.", "said"),
    ("sourdough", "lesson", "sourdough", "The sourdough starter needs feeding the night before baking.", "said"),
    ("cockpit", "project", "home cockpit", "He is building a home cockpit for Microsoft Flight Simulator.", "said"),
    ("parking", "fact", "street parking", "Street parking is not allowed on Tuesdays for sweeping.", "said"),
    ("texts", "preference", "contact", "He prefers text messages over phone calls.", "said"),
    ("recycling", "fact", "recycling", "The recycling goes out on Thursday night.", "said"),
    ("blood", "fact", "blood type", "His blood type is O negative.", "said"),
    ("visit", "summary", "Marguerite visit", "Planned Marguerite's visit for the second week of July.", "inferred"),
    # an older inference that the owner's word above contradicts
    ("sis-bday-old", "summary", "talk about Marguerite", "Mentioned that Marguerite's birthday falls on June 5.",
     "inferred"),
]
# (question, labels that are a right answer, sort)
QUESTIONS = [
    ("how does he like his morning brew", ["coffee"], "paraphrase"),
    ("is there any seafood he can't eat", ["allergy"], "paraphrase"),
    ("where does his sibling reside", ["sister"], "paraphrase"),
    ("what breed is our pet", ["dog"], "paraphrase"),
    ("what vehicle do we own", ["car"], "paraphrase"),
    ("where are we going this summer", ["holiday"], "paraphrase"),
    ("when do I need to get my teeth looked at", ["dentist"], "paraphrase"),
    ("anything to verify before the overnight archive job", ["backup"], "paraphrase"),
    ("which machine runs the instrument panel", ["node1"], "paraphrase"),
    ("where are the wrenches for my bicycle", ["tools"], "paraphrase"),
    ("where do I adjust the heat on the top floor", ["thermostat"], "paraphrase"),
    ("what tunes should I put on while I make dinner", ["jazz"], "paraphrase"),
    ("what does he do for a living", ["job"], "paraphrase"),
    ("how far along is the glass house", ["greenhouse"], "paraphrase"),
    ("how warm do we keep things when it is cold out", ["heating"], "paraphrase"),
    ("when should I call mom to celebrate", ["mom"], "paraphrase"),
    ("what did we figure out about the levers for the sim", ["throttle"], "paraphrase"),
    ("what should be left out of my daily update", ["briefing"], "paraphrase"),
    ("which wireless should the smart plugs join", ["iot"], "paraphrase"),
    ("what am I cutting down on eating", ["diet"], "paraphrase"),
    ("who is my family physician", ["gp"], "paraphrase"),
    ("what paperwork is due in the spring", ["insurance"], "paraphrase"),
    ("how big is my bicycle", ["bike"], "paraphrase"),
    ("if I get locked out how can I get in", ["spare"], "paraphrase"),
    ("how often does the big leafy plant get a drink", ["fig"], "paraphrase"),
    ("what do I do the evening before making bread", ["sourdough"], "paraphrase"),
    ("when can't I leave the car on the road", ["parking"], "paraphrase"),
    ("how should people get in touch with him", ["texts"], "paraphrase"),
    ("which night do the bins go to the curb", ["recycling"], "paraphrase"),
    ("when is my sister coming to stay", ["visit"], "paraphrase"),
    ("what novels does he enjoy", ["books"], "paraphrase"),
    ("the GPU box crashed while making a picture, what now", ["oom"], "paraphrase"),
    ("KJX-4471", ["car"], "exact"),
    ("Okafor", ["gp"], "exact"),
    ("Biscuit", ["dog"], "exact"),
    ("port 8088", ["node1"], "exact"),
    ("Surly", ["bike"], "exact"),
    ("Marguerite", ["sister", "sis-bday", "visit"], "exact"),
    ("Le Guin", ["books"], "exact"),
    ("GEEKOM", ["douglas"], "exact"),
    ("when is Marguerite's birthday", ["sis-bday"], "correction"),
    ("what's the capital of Australia", [], "unrelated"),
    ("how do I fix a flat tyre on a lawnmower", [], "unrelated"),
    ("who won the election", [], "unrelated"),
    ("recommend a good horror movie", [], "unrelated"),
    ("what time is sunset today", [], "unrelated"),
    ("how many ounces are in a cup", [], "unrelated"),
    ("translate hello into French", [], "unrelated"),
    ("what is the stock price of Apple", [], "unrelated"),
    ("tell me a joke about penguins", [], "unrelated"),
    ("how far away is the moon", [], "unrelated"),
]
SAME = [("He is allergic to shellfish.", "He has a shellfish allergy."),
        ("The car is a Subaru Outback.", "We drive a Subaru Outback."),
        ("He takes his coffee black with no sugar.", "He drinks his coffee black, no sugar."),
        ("The recycling goes out on Thursday night.", "Put the recycling out on Thursday evening."),
        ("Marguerite's birthday is June 3.", "Mentioned that Marguerite's birthday falls on June 5.")]
DIFFERENT = [("His mother's birthday is April 12.", "Marguerite's birthday is June 3."),
             ("Renew the home insurance before March 15.", "Renew the car insurance before March 15."),
             ("The spare key is with the neighbour at number 14.", "The spare key is under the mat at number 16."),
             ("He likes to listen to jazz while cooking.", "He likes to listen to podcasts while running."),
             ("node1 drives the cockpit panel.", "Douglas runs the language model.")]

emb = mem.Embedder(args.socket, args.dim, args.query, args.doc)
tag = emb.tag(10)
if not tag:
    sys.exit(f"nothing answers at {args.socket}")
store = mem.Store(mem.DB, embedder=emb)
ids = {}
for label, kind, subject, text, source in MEMORIES:
    i, _ = store.add(kind, subject, text, source, "eval")
    ids[label] = i
old = ids["sis-bday-old"]          # the inference is a month older than the owner's word
store.db.execute("UPDATE memories SET updated = updated - 30 * 86400 WHERE id = ?", (old,))
label_of = {v: k for k, v in ids.items()}

began = time.time()
while store.embed(16):
    pass
docs_per_s = len(MEMORIES) / (time.time() - began)

lat = []
for q, _w, _s in QUESTIONS:
    emb.cache.clear()
    t = time.time()
    emb.question(q, timeout=10)
    lat.append((time.time() - t) * 1000)
vecs = dict(store.vectors(tag))


def sim(a, b):
    va, vb = emb.vectors([emb.document("", a)], 10)[0], emb.vectors([emb.document("", b)], 10)[0]
    return sum(x * y for x, y in zip(va, vb))


def run(label, use_sense, minimum=0.0, same=1.01):
    mem.EMBED_MIN, mem.EMBED_SAME = minimum, same
    store.embedder = emb if use_sense else None
    by = {}
    for q, want, sort in QUESTIONS:
        got = [label_of[m["id"]] for m in store.recall(q, 6, 900, mark=False)]
        rank = next((n + 1 for n, g in enumerate(got) if g in want), None)
        by.setdefault(sort, []).append((rank, len(got), got))
    para, exact = by["paraphrase"], by["exact"]
    f1 = lambda rs: sum(r == 1 for r, _n, _g in rs) / len(rs)                 # noqa: E731
    f3 = lambda rs: sum(bool(r) and r <= 3 for r, _n, _g in rs) / len(rs)      # noqa: E731
    mrr = lambda rs: sum(1 / r for r, _n, _g in rs if r) / len(rs)             # noqa: E731
    noise = statistics.mean(n for _r, n, _g in by["unrelated"])
    extra = statistics.mean(n - (1 if r else 0) for r, n, _g in para)
    corr = by["correction"][0][2]
    print(f"  {label:<22} paraphrase found@1 {f1(para):.2f} @3 {f3(para):.2f} MRR {mrr(para):.2f} | "
          f"exact @1 {f1(exact):.2f} @3 {f3(exact):.2f} | unrelated: {noise:.1f} handed out | "
          f"other memories with a paraphrase answer: {extra:.1f} | correction: {corr[:2]}")
    return by


print(f"model: {tag}")
print(f"speed: a question's vector p50 {statistics.median(lat):.0f} ms, p95 {sorted(lat)[int(len(lat) * .95)]:.0f} ms; "
      f"{docs_per_s:.0f} memories a second")
if args.pid:
    with open(f"/proc/{args.pid}/status") as f:
        print("memory held: " + next(x.split(":")[1].strip() for x in f if x.startswith("VmRSS")))
right = [vecs[ids[w[0]]] for q, w, s in QUESTIONS if s == "paraphrase"]
qs = [emb.question(q, 10) for q, w, s in QUESTIONS if s == "paraphrase"]
good = sorted(sum(x * y for x, y in zip(a, b)) for a, b in zip(qs, right))
bad = sorted(max(sum(x * y for x, y in zip(emb.question(q, 10), v)) for v in vecs.values())
             for q, w, s in QUESTIONS if s == "unrelated")
print(f"similarity of a paraphrase to its memory: min {good[0]:.2f}, 10th pct {good[len(good) // 10]:.2f}, "
      f"median {statistics.median(good):.2f}")
print(f"best match of an unrelated question:      median {statistics.median(bad):.2f}, max {bad[-1]:.2f}")
print("near-duplicates:          " + ", ".join(f"{sim(a, b):.2f}" for a, b in SAME))
print("near but different:       " + ", ".join(f"{sim(a, b):.2f}" for a, b in DIFFERENT))
print("recall (limit 6, budget 900, as the voice pipeline asks):")
run("words only", False)
for m in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
    run(f"words + sense, min {m:.2f}", True, m)
same = float(os.environ.get("EVAL_SAME", "0.9"))
by = run(f"min 0.45, same {same:.2f}", True, 0.45, same)
print("misses (min 0.45):")
for sort in ("paraphrase", "exact"):
    for (q, w, s), (r, n, got) in zip([x for x in QUESTIONS if x[2] == sort], by[sort]):
        if r != 1:
            print(f"  {sort}: {q!r} wanted {w}, got {got}")
store.db.close()
shutil.rmtree(root, ignore_errors=True)
