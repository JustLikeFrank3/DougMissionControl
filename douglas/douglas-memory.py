#!/usr/bin/env python3
"""douglas-memory: what Douglas remembers from one conversation to the next.

Runs ON DOUGLAS as its own user, with no network and one file to its name: a
SQLite database. Stdlib only. It knows nothing of models: what it keeps is
sentences, each with where it came from, when, and how sure it is. Swap the
model, restart the pipeline, reboot the machine: the memory is as it was.

    douglas-memory.py serve          the service (douglas-memory.service)
    douglas-memory.py backup FILE    a copy of the database as it stands,
                                     safe to take while the service runs
    douglas-memory.py check          is the database sound, and what is in it
(the last two by hand, as root or as the douglas-memory user)

Two doors, two kinds of caller, told apart by which door they can open
(the file system decides that, not anything a caller says of itself):

    assistant.sock   the voice pipeline. It may ask what bears on a message
                     (recall) or what it is surest of (known: a dozen things,
                     not the lot), note something down (remember), hand in what
                     a finished conversation came to (consolidate), mark a
                     memory as forgotten (forget), and write to the log of
                     what Douglas did (log). It cannot read the whole
                     memory, cannot change a memory's words, cannot bring
                     back or destroy anything, and cannot read the log.
    owner.sock       Mission Control, through douglas-agent: the owner's
                     hands. List, read with its history, correct, delete,
                     restore, purge for good, read the log, export.

Notices are kept here too, because this is where durable things on Douglas
live: what the watch in douglas-agent raised (a machine gone, a service
down, a disk nearly full), each once for as long as it holds. The watch
raises and clears them through the owner's door; the owner reads them there,
acknowledges them and says where each severity is to go; Douglas, through
his own door, is given the open ones to mention and may mark them as told,
and can do nothing else with them. No model is asked anything to raise one.

One line of JSON in, one out:   {"op": "recall", "text": "..."}

What is kept, of each memory:
    kind         preference | fact | project | decision | task | lesson |
                 fleet | summary
    subject      a few words for what it is about; two memories of one kind
                 and one subject are the same memory, said twice or changed
    text         the memory itself, a sentence or two
    source       who vouches for it: owner (entered or corrected in Mission
                 Control), said (the person's own words), observed (this
                 system saw it happen), inferred (worked out from a
                 conversation). A weaker source never replaces a stronger.
    confidence   0 to 1
    via, conversation, model      where it came from
    created, updated, last_used, uses
    status       active | superseded | deleted

Nothing is overwritten without a trace: every change is a row in `history`,
with what was there before. A memory that a newer one replaces is kept and
marked superseded; one the owner corrects is corrected in place; one that is
deleted is kept, out of reach of recall, until it is purged.

What comes out of `recall` is data. The pipeline hands it to the model
marked as notes, never as instructions, and this service takes out of
every text, coming in and going out, what could pass for anything else:
characters that do not show, and the square brackets the pipeline's own
notes are written in.

Settings (environment; /etc/douglas/memory.env):
    MEMORY_DB          the database           /var/lib/douglas-memory/memory.db
    MEMORY_RUN         where the doors are    /run/douglas-memory
    MEMORY_MAX         memories kept active, the least useful let go   5000
    MEMORY_EVENTS      log rows kept                                  5000
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import socket
import socketserver
import sqlite3
import sys
import threading
import time


def E(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    return v if v != "" else default


DB = E("MEMORY_DB", "/var/lib/douglas-memory/memory.db")
RUN = E("MEMORY_RUN", "/run/douglas-memory")
MAX_ACTIVE = int(E("MEMORY_MAX", "5000"))
MAX_EVENTS = int(E("MEMORY_EVENTS", "5000"))

KINDS = ("preference", "fact", "project", "decision", "task", "lesson", "fleet", "summary")
# A notice is one of three things, and where each goes is the owner's to say:
# whether Douglas mentions it at the next turn (tell), and whether it is said
# aloud in the room (aloud; never in the quiet hours). On the screens they
# all are, always. Nothing is said aloud until the owner switches that on.
SEVERITIES = ("information", "warning", "critical")
PREFS = {"information": {"tell": False, "aloud": False}, "warning": {"tell": True, "aloud": False},
         "critical": {"tell": True, "aloud": False}, "quiet_from": 22, "quiet_to": 8}
NOTICES_MAX = 2000                # rows kept; the oldest that are over go first
NOTICE_DAYS = 30                  # how long one that is over is kept
# Who vouches for a memory, and how far: a weaker source does not replace a
# stronger one's memory of the same thing.
SOURCES = {"owner": 3, "said": 2, "observed": 2, "inferred": 1}
SURE = {"owner": 1.0, "said": 0.9, "observed": 0.9, "inferred": 0.5}
# How long a memory of each kind stays worth as much, in days: after that
# long it counts half in what is recalled. A preference holds; what a
# machine was doing last week does not.
HALF_LIFE = {"preference": 730, "fact": 730, "decision": 365, "lesson": 365, "project": 90,
             "task": 45, "summary": 45, "fleet": 14}
TEXT_MAX, SUBJECT_MAX = 400, 80
HIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁠-⁩﻿"
                    "\U000e0000-\U000e007f]")
# What is never kept, whoever asks: a memory is not a place for a secret.
# Three ways one is known: by its shape (a token of a kind that services
# hand out), by being named and then given ("the password for the wifi is
# ...", "garage door code: 4471"), and by being a card or a social security
# number. A long run of letters and digits counts too, unless it is part of
# an address or a path: a branch name is not a key.
SECRET = re.compile(
    r"github_pat_\w{20,}|gh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|"
    r"\bBSA[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|"
    # A password named and given is one, whatever it looks like ("the wifi
    # password is swordfish"). The other names are ordinary words too ("the
    # token ring network is long gone", "the secret is that she knows"), so
    # what follows them has to look the part: a digit or a sign in it, or long.
    r"\bpass(?:word|phrase|code)s?\b[^.!?\n]{0,40}?(?:\bis\b|\bare\b|=|:)\s*\S{3,}|"
    r"(?:\b(?:secret|token|credentials?|login|combination|cvv|cvc|"
    r"(?:api|secret|private|access|search|licen[cs]e|product)[ _-]?key|pin[ _-]?(?:code|number)|"
    r"(?:door|gate|garage|alarm|safe|entry|access|security|lock)[ _-]?code)\b|(?-i:\bPIN\b))"
    r"[^.!?\n]{0,40}?(?:\bis\b|\bare\b|=|:)\s*(?:(?=\S*[0-9!@#$%^&*+=_~])\S{3,}|\S{12,})|"
    r"\bpin\b[^.!?\n]{0,20}?(?:\bis\b|=|:)\s*\d{3,8}\b|"      # "the door pin: 4471", not "pin 17 is the button"
    r"\b(?:\d[ -]?){13,19}\b|\b\d{3}-\d{2}-\d{4}\b|"
    r"(?<![/\w.-])[A-Za-z0-9+_-]{40,}(?![/\w.-])", re.I)
STOP = set("""a about after again all also am an and any are as at be because been before being but by can
could did do does doing don down for from had has have having he her here hers him his how i if in into is
it its just me more most my no nor not now of off on once only or other our out over own s same she so
some such t than that the their them then there these they this those to too under until up very was we
were what when where which while who whom why will with you your yours tell know think like want need
get got say said one thing things please douglas doug yes yeah okay ok""".split())


def log(msg: str) -> None:
    print(msg, flush=True)                     # the journal stamps it


def clean(text, limit: int) -> str:
    """Somebody's words as a memory keeps them: on one line, with nothing in
    them that does not show and nothing that could pass for a note of the
    pipeline's own, cut to size."""
    text = HIDDEN.sub("", str(text or "")).replace("[", "(").replace("]", ")")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'_.-]*[a-z0-9]|[a-z0-9]", str(text).lower())
            if w not in STOP and len(w) > 1]


def key_of(subject: str) -> str:
    """What two subjects have to share to be the same subject: their words,
    whatever order, case or filler they came in."""
    low = str(subject).lower().replace("\u2019", "'").replace("'s", "")
    return " ".join(sorted(set(words(low))))


# The words that carry nothing by themselves. Kept short on purpose: what is
# not in it counts when two texts are held against each other, and that has
# to include every number, and whatever turns a sentence into its opposite
# (not, no, never, without, off).
SMALL = set("a an the is are was were be been am to of and that this it its he she they his her their "
            "him them i my me you your".split())


def gist(text: str) -> list[str]:
    """What a text says, for telling whether another says the same: its
    words in their order, with their endings off and the small ones out. A
    number, a date or a time stays whole: "14th" is not "15th", and
    "forty-two" is not "forty-seven"."""
    low = str(text).lower().replace("\u2019", "'").replace("n't", " not").replace("'s", "")
    return [re.sub(r"(ies|es|ed|ing|s)$", "", w) if w.isalpha() and len(w) > 4 else w
            for w in re.findall(r"[a-z0-9]+(?:[-.:/][a-z0-9]+)*", low) if w not in SMALL]


def said_again(new: str, old: str) -> bool:
    """Does a text say just what a memory says? Then it is that memory,
    said twice. The test is strict on purpose, because the other mistake is
    the costly one: taking something new for something known drops it, and
    what is dropped is the newer word. Sharing most of their words will not
    do (that is what a changed date looks like), and neither will saying
    less: "he eats meat" after "he does not eat meat" is all inside the old
    one, and is its opposite. Nor the same words in another order: "Ann
    owes Bob" is not "Bob owes Ann". So: the same words, in the same order,
    once the small ones and the endings are off. Anything else is a
    different thing to remember; if it is on the same subject it takes the
    old one's place, and the old one is kept, marked replaced."""
    x = gist(new)
    return bool(x) and x == gist(old)


class Problem(Exception):
    """Something that could not be done, in words for the caller."""


class Store:
    def __init__(self, path: str) -> None:
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=FULL", "foreign_keys=ON", "secure_delete=ON"):
            self.db.execute("PRAGMA " + pragma)
        self.migrate()
        # A second look at the same file, for what is asked on every message.
        # Every change waits for the disk to say it is written, and a disk
        # can take half a second over that. With a write-ahead log a reader
        # does not wait for a writer, so what Douglas recalls is not held up
        # by something else being written down.
        self.rd = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.rd.row_factory = sqlite3.Row
        self.rd.execute("PRAGMA query_only=ON")
        self.rlock = threading.Lock()
        # Which memories were handed out, and when: written down with the next
        # change or by the clock (settle), never in the way of a recall.
        self.used: dict[int, list] = {}
        self.used_lock = threading.Lock()
        self.uncut = False               # a purge whose text may still be in the log

    def migrate(self) -> None:
        """The schema, brought up to date one step at a time: each step runs
        once, and an older database loses nothing by it."""
        steps = ["""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL,
                subject_key TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
                confidence REAL NOT NULL, via TEXT NOT NULL DEFAULT '', conversation TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
                last_used REAL, uses INTEGER NOT NULL DEFAULT 0, due REAL, done REAL,
                status TEXT NOT NULL DEFAULT 'active', superseded_by INTEGER);
            CREATE INDEX memories_by_subject ON memories (kind, subject_key, status);
            CREATE INDEX memories_by_status ON memories (status, updated);
            CREATE VIRTUAL TABLE memories_fts USING fts5(subject, text, tokenize='porter unicode61');
            INSERT INTO memories_fts (memories_fts, rank) VALUES ('secure-delete', 1);
            CREATE TABLE history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory INTEGER NOT NULL, at REAL NOT NULL,
                who TEXT NOT NULL, action TEXT NOT NULL, before TEXT NOT NULL DEFAULT '',
                after TEXT NOT NULL DEFAULT '');
            CREATE INDEX history_by_memory ON history (memory, at);
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, started REAL, ended REAL, via TEXT NOT NULL DEFAULT '',
                turns INTEGER NOT NULL DEFAULT 0, consolidated REAL, model TEXT NOT NULL DEFAULT '',
                kept INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
                name TEXT NOT NULL, action TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL DEFAULT 1,
                ms INTEGER NOT NULL DEFAULT 0, via TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
            CREATE TABLE state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        """, """
            CREATE TABLE notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, rule TEXT NOT NULL,
                subject TEXT NOT NULL, severity TEXT NOT NULL, text TEXT NOT NULL,
                raised REAL NOT NULL, updated REAL NOT NULL, cleared REAL, acked REAL, told REAL,
                spoken REAL, times INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX notices_by_key ON notices (key, cleared);
        """]
        with self.lock:
            have = self.db.execute("PRAGMA user_version").fetchone()[0]
            for n, step in enumerate(steps[have:], have + 1):
                self.db.executescript("BEGIN;" + step + f"PRAGMA user_version={n};COMMIT;")
                log(f"memory: the database is now at version {n}")

    @contextlib.contextmanager
    def tx(self):
        """One change, whole or not at all. Inside another, it is part of
        that one."""
        with self.lock:
            if self.db.in_transaction:
                yield
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.settle()
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def settle(self) -> None:
        """Write down which memories were handed out since the last change.
        Part of whatever change is being made."""
        with self.used_lock:
            used, self.used = self.used, {}
        for i, (times, when) in used.items():
            self.db.execute("UPDATE memories SET last_used = ?, uses = uses + ? WHERE id = ?", (when, times, i))

    def flush(self) -> None:
        """The same, when there is something to write down and no change to
        ride along with: before the owner reads, and by the clock."""
        if self.used:
            with self.tx():
                pass
        if self.uncut:
            self.cut()

    # -- writing ---------------------------------------------------------------

    def note(self, memory: int, who: str, action: str, before: str = "", after: str = "") -> None:
        self.db.execute("INSERT INTO history (memory, at, who, action, before, after) VALUES (?,?,?,?,?,?)",
                        (memory, time.time(), who, action, before, after))

    def index(self, row_id: int, subject: str | None, text: str | None) -> None:
        self.db.execute("DELETE FROM memories_fts WHERE rowid = ?", (row_id,))
        if subject is not None:
            self.db.execute("INSERT INTO memories_fts (rowid, subject, text) VALUES (?,?,?)",
                            (row_id, subject, text))

    def add(self, kind: str, subject: str, text: str, source: str, who: str, confidence=None,
            via: str = "", conversation: str = "", model: str = "", due=None) -> tuple[int, str]:
        """Keep a memory, or find that it is kept already. What became of
        it: 'kept' (new), 'known' (said again: the one there is made a
        little surer), 'replaced' (it takes the place of an older one on the
        same subject, which is kept and marked), or 'refused' (a firmer
        memory of that subject says otherwise: that one stands)."""
        kind, source = str(kind), str(source)
        if kind not in KINDS:
            raise Problem(f"A memory is one of: {', '.join(KINDS)}.")
        if source not in SOURCES:
            raise Problem(f"A memory comes from one of: {', '.join(SOURCES)}.")
        subject, text = clean(subject, SUBJECT_MAX), clean(text, TEXT_MAX)
        if len(text) < 3 or not words(text):
            raise Problem("There is nothing in that to remember.")
        if SECRET.search(text) or SECRET.search(subject):
            raise Problem("That looks like a password, a key or a token, and those are not kept in memory.")
        # A subject made only of small words ("what I like", "about me") names
        # nothing, and every such one would be the same subject as the next.
        if not key_of(subject):
            subject = clean(" ".join(words(text)[:5]), SUBJECT_MAX)
        key = key_of(subject)
        try:
            sure = min(1.0, max(0.05, float(confidence))) if confidence is not None else SURE[source]
        except (TypeError, ValueError):
            sure = SURE[source]
        try:
            due = float(due) if due not in (None, "", 0) else None
        except (TypeError, ValueError):
            due = None
        now = time.time()
        with self.tx():
            # What there is on this subject already. A task that is done is not
            # in the way of a new one, and is not replaced by it.
            same = self.db.execute("SELECT * FROM memories WHERE kind = ? AND subject_key = ? AND "
                                   "status = 'active' AND (kind != 'task' OR done IS NULL) "
                                   "ORDER BY updated DESC", (kind, key)).fetchall() if key else []
            near = same or self.like(text, kind, self.db)
            for old in near:
                if said_again(text, old["text"]):             # the same memory, said twice
                    self.db.execute("UPDATE memories SET updated = ?, confidence = ? WHERE id = ?",
                                    (now, min(1.0, max(old["confidence"], sure) + 0.05), old["id"]))
                    self.note(old["id"], who, "said again")
                    return old["id"], "known"
            firmer = [o for o in same if SOURCES[o["source"]] > SOURCES[source]]
            if firmer:                                        # it stands, and this is written beside it
                self.note(firmer[0]["id"], who, "kept over a weaker note", after=text)
                return firmer[0]["id"], "refused"
            cur = self.db.execute(
                "INSERT INTO memories (kind, subject, subject_key, text, source, confidence, via, "
                "conversation, model, created, updated, due) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, subject, key, text, source, sure, clean(via, 20), clean(conversation, 40),
                 clean(model, 80), now, now, due))
            new = cur.lastrowid
            self.index(new, subject, text)
            self.note(new, who, "kept", after=text)
            for old in same:                                  # the newer word on a subject is the word on it
                self.db.execute("UPDATE memories SET status = 'superseded', superseded_by = ?, updated = ? "
                                "WHERE id = ?", (new, now, old["id"]))
                self.index(old["id"], None, None)
                self.note(old["id"], who, "replaced", before=old["text"], after=f"by {new}")
            return new, "replaced" if same else "kept"

    def like(self, text: str, kind: str = "", db=None) -> list:
        """Active memories that share words with a text, the nearest first.
        Asked of `db`: the reader, unless a change is being made and has to
        see itself."""
        q = self.query(text)
        if not q:
            return []
        try:
            rows = (db or self.rd).execute(
                "SELECT m.* FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid WHERE memories_fts MATCH ? "
                "AND m.status = 'active'" + (" AND m.kind = ?" if kind else "") + " ORDER BY bm25(memories_fts) "
                "LIMIT 8", (q, kind) if kind else (q,)).fetchall()
        except sqlite3.OperationalError:
            return []
        return rows

    @staticmethod
    def query(text: str) -> str:
        """A person's words as a full-text query: their content words, any
        of them, each in quotes so that nothing in them is read as the
        query language's own."""
        seen = list(dict.fromkeys(w for w in words(text) if len(w) > 2))[:14]
        return " OR ".join('"' + w.replace('"', "") + '"' for w in seen)

    # -- reading, for the assistant ---------------------------------------------

    def recall(self, text: str, limit: int = 6, budget: int = 900, kinds=None) -> list[dict]:
        """What is remembered that bears on a message: the active memories
        that share its words, weighed by how sure each is, who vouches for
        it and how old it is, as many as fit in `budget` characters. One
        that does not fit is passed over for a shorter one, not the end of
        the list. Each that is handed out is marked as used."""
        now = time.time()
        limit, budget = min(max(1, int(limit)), 12), min(max(100, int(budget)), 3000)
        q = self.query(text)
        if not q:
            return []
        kinds = [k for k in (kinds or []) if k in KINDS]
        with self.rlock:
            try:
                rows = self.rd.execute(
                    "SELECT m.*, bm25(memories_fts, 3.0, 1.0) AS rank FROM memories_fts JOIN memories m "
                    "ON m.id = memories_fts.rowid WHERE memories_fts MATCH ? AND m.status = 'active' "
                    + (f"AND m.kind IN ({','.join('?' * len(kinds))}) " if kinds else "")
                    + "ORDER BY rank LIMIT 40", (q, *kinds)).fetchall()
            except sqlite3.OperationalError:
                return []
        scored = []
        for r in rows:
            if r["kind"] == "task" and r["done"]:
                continue
            age = max(0.0, now - r["updated"]) / 86400
            weight = (-r["rank"]) * (0.4 + 0.6 * r["confidence"]) * (1.0 + 0.15 * SOURCES[r["source"]]) \
                * math.pow(0.5, age / HALF_LIFE[r["kind"]])
            scored.append((weight, r))
        scored.sort(key=lambda x: -x[0])
        out, used = [], 0
        for _w, r in scored:
            if len(out) >= limit:
                break
            size = len(r["text"]) + len(r["subject"]) + 40
            if used + size > budget:
                continue                    # skip, not stop: a shorter one further down may fit
            out.append(self.shown(r))
            used += size
        with self.used_lock:                # noted here, written down later (settle)
            for m in out:
                seen = self.used.setdefault(m["id"], [0, now])
                seen[0], seen[1] = seen[0] + 1, now
        return out

    @staticmethod
    def shown(r) -> dict:
        return {"id": r["id"], "kind": r["kind"], "subject": r["subject"], "text": r["text"],
                "source": r["source"], "confidence": round(r["confidence"], 2), "via": r["via"],
                "created": r["created"], "updated": r["updated"], "last_used": r["last_used"],
                "uses": r["uses"], "status": r["status"], "superseded_by": r["superseded_by"],
                "due": r["due"], "done": r["done"], "conversation": r["conversation"], "model": r["model"]}

    def known(self, limit: int = 12, budget: int = 1500) -> list[dict]:
        """What he is surest of, for "what do you remember about me?": the
        standing things (not summaries, not what a machine was doing), the
        firmest and the newest first, as many as fit. A digest, not the
        whole memory: that is the owner's to read."""
        with self.rlock:
            rows = self.rd.execute(
                "SELECT * FROM memories WHERE status = 'active' AND kind IN ('preference', 'fact', 'project', "
                "'decision', 'task') AND (kind != 'task' OR done IS NULL) ORDER BY confidence * (CASE source "
                "WHEN 'owner' THEN 1.3 WHEN 'inferred' THEN 0.7 ELSE 1.0 END) DESC, updated DESC LIMIT 60").fetchall()
        out, used = [], 0
        for r in rows:
            if len(out) >= limit:
                break
            if used + len(r["text"]) + 40 > budget:
                continue
            out.append(self.shown(r))
            used += len(r["text"]) + 40
        return out

    def matching(self, text: str, limit: int = 3) -> tuple[list[dict], int]:
        """The memories a person most likely means by "forget that ...":
        read back to them before anything is forgotten. And how many more
        match that are the owner's own word: those are not his to forget,
        whoever asks him to."""
        with self.rlock:
            rows = self.like(text)[:8]
        mine = [self.shown(r) for r in rows if r["source"] != "owner"]
        return mine[:max(1, min(int(limit), 5))], len(rows) - len(mine)

    def forget(self, ids, who: str) -> list[int]:
        """Mark memories as deleted: out of reach of recall, still there
        for the owner to bring back or to purge."""
        done = []
        with self.tx():
            for i in [int(x) for x in ids if str(x).isdigit()][:10]:
                # Not what the owner wrote down: that is changed where it was
                # written, and a yes said to Douglas does not reach it.
                r = self.db.execute("SELECT * FROM memories WHERE id = ? AND status = 'active' AND "
                                    "source != 'owner'", (i,)).fetchone()
                if r:
                    self.db.execute("UPDATE memories SET status = 'deleted', updated = ? WHERE id = ?",
                                    (time.time(), i))
                    self.index(i, None, None)
                    self.note(i, who, "deleted", before=r["text"])
                    done.append(i)
        return done

    def consolidate(self, conv: dict, items, model: str, who: str) -> dict:
        """What a finished conversation came to: its memories kept, each as
        `add` would keep it, and the conversation written down as done so
        that it is not gone through twice."""
        cid = clean(conv.get("id"), 40)
        if not cid:
            raise Problem("Which conversation?")
        counts = {"kept": 0, "known": 0, "replaced": 0, "refused": 0, "dropped": 0}
        with self.tx():
            if self.db.execute("SELECT 1 FROM conversations WHERE id = ? AND consolidated IS NOT NULL",
                               (cid,)).fetchone():
                return dict(counts, already=True)
            # Something noted down during this conversation has since been
            # deleted or purged. What was said is still in the record this
            # is made from, and going through it would bring the thing
            # straight back; so nothing is made of this conversation.
            try:
                began, ended = float(conv.get("started") or 0) - 1, float(conv.get("ended") or 0) + 1
            except (TypeError, ValueError):
                began = ended = 0.0
            if began > 0 and (
                    self.db.execute("SELECT 1 FROM memories WHERE status = 'deleted' AND created BETWEEN ? AND ?",
                                    (began, ended)).fetchone()
                    or self.db.execute("SELECT 1 FROM history WHERE action = 'purged' AND "
                                       "CAST(after AS REAL) BETWEEN ? AND ?", (began, ended)).fetchone()):
                counts["dropped"] += len(items) if isinstance(items, list) else 0
                items = []
            for item in (items if isinstance(items, list) else [])[:12]:
                if not isinstance(item, dict):
                    counts["dropped"] += 1
                    continue
                try:
                    _id, what = self.add(item.get("kind"), item.get("subject"), item.get("text"),
                                         item.get("source") if item.get("source") in ("said", "inferred")
                                         else "inferred", who, item.get("confidence"),
                                         via=conv.get("via") or "", conversation=cid, model=model,
                                         due=item.get("due"))
                    counts[what] += 1
                except Problem:
                    counts["dropped"] += 1
            self.db.execute(
                "INSERT INTO conversations (id, started, ended, via, turns, consolidated, model, kept) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET consolidated = excluded.consolidated, "
                "model = excluded.model, kept = excluded.kept",
                (cid, conv.get("started"), conv.get("ended"), clean(conv.get("via"), 20),
                 int(conv.get("turns") or 0), time.time(), clean(model, 80), counts["kept"] + counts["replaced"]))
            self.trim()
        return counts

    def trim(self) -> None:
        """Keep the memory to a size: past the most it holds, the active
        memories least sure and longest unused are marked as let go (not
        the owner's own, and never destroyed here)."""
        n = self.db.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]
        if n > MAX_ACTIVE:
            for r in self.db.execute(
                    "SELECT id, text FROM memories WHERE status = 'active' AND source != 'owner' "
                    "ORDER BY confidence, COALESCE(last_used, created) LIMIT ?", (n - MAX_ACTIVE,)).fetchall():
                self.db.execute("UPDATE memories SET status = 'deleted', updated = ? WHERE id = ?",
                                (time.time(), r["id"]))
                self.index(r["id"], None, None)
                self.note(r["id"], "memory", "let go for room", before=r["text"])

    def state(self, name: str, value=None):
        if name != "cursor:transcript":
            raise Problem("There is one thing kept here: where the record of what was said has been read to.")
        if value is not None:
            if not (isinstance(value, dict) and isinstance(value.get("offset"), int)
                    and not isinstance(value["offset"], bool) and value["offset"] >= 0):
                raise Problem("That is a place in the record: a number of bytes, and nothing else.")
            value = {"offset": value["offset"]}
        with self.tx():
            if value is not None:
                self.db.execute("INSERT INTO state (name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE "
                                "SET value = excluded.value", (name, json.dumps(value)))
                return value
            r = self.db.execute("SELECT value FROM state WHERE name = ?", (name,)).fetchone()
            return json.loads(r["value"]) if r else None

    # -- notices: what the watch raised ------------------------------------------

    @staticmethod
    def prefs_of(row) -> dict:
        """The owner's choices as they are kept, made whole: whatever is
        missing or is not what it should be is as it was to begin with."""
        try:
            kept = json.loads(row["value"]) if row else {}
        except ValueError:
            kept = {}
        kept = kept if isinstance(kept, dict) else {}
        now = {}
        for s in SEVERITIES:
            mine = kept.get(s) if isinstance(kept.get(s), dict) else {}
            now[s] = {k: mine[k] if isinstance(mine.get(k), bool) else PREFS[s][k] for k in ("tell", "aloud")}
        for k in ("quiet_from", "quiet_to"):
            v = kept.get(k)
            now[k] = v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 23 else PREFS[k]
        return now

    def prefs(self, change: dict | None = None) -> dict:
        """Where each severity of notice goes, and the hours nothing is said
        aloud. Changed a severity or the hours at a time, each value checked:
        nothing else can be put in here."""
        with self.tx():
            now = self.prefs_of(self.db.execute("SELECT value FROM state WHERE name = 'notice_prefs'").fetchone())
            if change:
                sev = change.get("severity")
                if sev is not None and sev not in SEVERITIES:
                    raise Problem(f"A notice is one of: {', '.join(SEVERITIES)}.")
                for k in ("tell", "aloud"):
                    if sev and isinstance(change.get(k), bool):
                        now[sev][k] = change[k]
                for k in ("quiet_from", "quiet_to"):
                    v = change.get(k)
                    if isinstance(v, int) and not isinstance(v, bool):
                        if not 0 <= v <= 23:
                            raise Problem("An hour is a number from 0 to 23.")
                        now[k] = v
                self.db.execute("INSERT INTO state (name, value) VALUES ('notice_prefs', ?) ON CONFLICT(name) "
                                "DO UPDATE SET value = excluded.value", (json.dumps(now),))
            return now

    def raise_notice(self, key, rule, subject, severity, text) -> tuple[int, str]:
        """The watch says something holds. One notice for a thing for as long
        as it holds: said again while it is open, it is the same notice
        ('open'); said again worse, it is that notice made worse, to be told
        afresh ('worse'); otherwise it is new ('raised')."""
        key, rule, subject = clean(key, 120), clean(rule, 20), clean(subject, 80)
        text = clean(text, 300)
        if severity not in SEVERITIES:
            raise Problem(f"A notice is one of: {', '.join(SEVERITIES)}.")
        if not key or len(text) < 3:
            raise Problem("A notice is about something, and says something.")
        now = time.time()
        with self.tx():
            r = self.db.execute("SELECT * FROM notices WHERE key = ? AND cleared IS NULL", (key,)).fetchone()
            if r is None:
                cur = self.db.execute(
                    "INSERT INTO notices (key, rule, subject, severity, text, raised, updated) VALUES (?,?,?,?,?,?,?)",
                    (key, rule, subject, severity, text, now, now))
                self.db.execute("DELETE FROM notices WHERE cleared IS NOT NULL AND (cleared < ? OR id <= "
                                "(SELECT MAX(id) FROM notices) - ?)", (now - NOTICE_DAYS * 86400, NOTICES_MAX))
                return cur.lastrowid, "raised"
            if SEVERITIES.index(severity) > SEVERITIES.index(r["severity"]):
                self.db.execute("UPDATE notices SET severity = ?, text = ?, updated = ?, acked = NULL, told = NULL, "
                                "spoken = NULL, times = times + 1 WHERE id = ?", (severity, text, now, r["id"]))
                return r["id"], "worse"
            return r["id"], "open"

    def clear_notice(self, key) -> int:
        """The watch says it holds no longer."""
        with self.tx():
            return self.db.execute("UPDATE notices SET cleared = ?, updated = ? WHERE key = ? AND cleared IS NULL",
                                   (time.time(), time.time(), clean(key, 120))).rowcount

    @staticmethod
    def noticed(r) -> dict:
        return {k: r[k] for k in ("id", "key", "rule", "subject", "severity", "text", "raised", "updated",
                                  "cleared", "acked", "told", "spoken", "times")}

    def notices(self, limit=50, days=3) -> dict:
        """For the owner: what is open, the worst and the newest first, then
        what was open lately and is over."""
        limit = min(max(1, int(limit or 50)), 200)
        with self.lock:
            now = self.db.execute(
                "SELECT * FROM notices WHERE cleared IS NULL ORDER BY CASE severity WHEN 'critical' THEN 0 "
                "WHEN 'warning' THEN 1 ELSE 2 END, raised DESC LIMIT ?", (limit,)).fetchall()
            over = self.db.execute("SELECT * FROM notices WHERE cleared IS NOT NULL AND cleared > ? "
                                   "ORDER BY cleared DESC LIMIT ?", (time.time() - days * 86400, limit)).fetchall()
            return {"open": [self.noticed(r) for r in now], "over": [self.noticed(r) for r in over]}

    def ack_notice(self, notice) -> dict:
        """The owner has seen it. It stays open for as long as it holds; it
        is not told or said aloud again."""
        with self.tx():
            n = self.db.execute("UPDATE notices SET acked = ? WHERE id = ? AND acked IS NULL",
                                (time.time(), int(notice))).rowcount
            r = self.db.execute("SELECT * FROM notices WHERE id = ?", (int(notice),)).fetchone()
            if r is None:
                raise Problem("There is no such notice.")
            return dict(self.noticed(r), changed=bool(n))

    def notices_for_him(self) -> dict:
        """For Douglas: what is open, in a few plain fields; which of those he
        has yet to mention, and which are yet to be said aloud, by what the
        owner has set for each severity. What the owner has acknowledged is
        neither. Read through the reader: it is asked on every turn."""
        with self.rlock:
            rows = self.rd.execute("SELECT * FROM notices WHERE cleared IS NULL ORDER BY CASE severity WHEN "
                                   "'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, raised DESC LIMIT 20").fetchall()
            p = self.prefs_of(self.rd.execute("SELECT value FROM state WHERE name = 'notice_prefs'").fetchone())
        quiet = [p["quiet_from"], p["quiet_to"]]
        short = lambda x: {k: x[k] for k in ("id", "severity", "subject", "text", "raised")}       # noqa: E731
        return {"open": [short(x) for x in rows],
                "tell": [short(x) for x in rows if x["told"] is None and x["acked"] is None and p[x["severity"]]["tell"]],
                "aloud": [short(x) for x in rows if x["spoken"] is None and x["acked"] is None
                          and p[x["severity"]]["aloud"]],
                "quiet": quiet}

    def marked(self, ids, what: str) -> int:
        """Douglas has mentioned these, or said them aloud. That is all he
        can do to a notice: he cannot clear one, acknowledge one or raise one."""
        if what not in ("told", "spoken"):
            raise Problem("A notice is marked as told or as spoken.")
        ids = [int(x) for x in (ids if isinstance(ids, list) else []) if str(x).isdigit()][:20]
        if not ids:
            return 0
        with self.tx():
            return self.db.execute(f"UPDATE notices SET {what} = ? WHERE {what} IS NULL AND id IN "
                                   f"({','.join('?' * len(ids))})", (time.time(), *ids)).rowcount

    def event(self, kind: str, name: str, action: str = "", ok=True, ms=0, via: str = "", detail: str = "") -> None:
        """One line in the log of what Douglas did: which tool, to do what,
        whether it was done and how long it took. Never the words."""
        with self.tx():
            self.db.execute("INSERT INTO events (at, kind, name, action, ok, ms, via, detail) VALUES "
                            "(?,?,?,?,?,?,?,?)", (time.time(), clean(kind, 20), clean(name, 40),
                                                  clean(action, 40), 1 if ok else 0, int(ms or 0),
                                                  clean(via, 20), clean(detail, 120)))
            # Each kind has its own room: a flood of lookups does not push
            # the record of a change out of the far end.
            self.db.execute("DELETE FROM events WHERE kind = ? AND id <= COALESCE((SELECT id FROM events "
                            "WHERE kind = ? ORDER BY id DESC LIMIT 1 OFFSET ?), 0)",
                            (clean(kind, 20), clean(kind, 20), MAX_EVENTS))

    # -- reading and changing, for the owner -------------------------------------

    def list(self, kind: str = "", status: str = "active", q: str = "", limit=50, offset=0) -> dict:
        where, args = [], []
        if kind in KINDS:
            where, args = where + ["m.kind = ?"], args + [kind]
        if status in ("active", "superseded", "deleted"):
            where, args = where + ["m.status = ?"], args + [status]
        limit, offset = min(max(1, int(limit or 50)), 200), max(0, int(offset or 0))
        self.flush()
        with self.lock:
            if str(q or "").strip():
                # Found by what it says, whatever its status: the index holds
                # the active ones only, and the owner looks for the others too.
                parts = [w for w in words(q)][:8] or [str(q).lower()[:40]]
                where += ["(" + " AND ".join("(lower(m.text) LIKE ? OR lower(m.subject) LIKE ?)" for _ in parts) + ")"]
                args += [x for w in parts for x in (f"%{w}%", f"%{w}%")]
            sql = " FROM memories m" + (" WHERE " + " AND ".join(where) if where else "")
            total = self.db.execute("SELECT COUNT(*)" + sql, args).fetchone()[0]
            rows = self.db.execute("SELECT m.*" + sql + " ORDER BY m.updated DESC LIMIT ? OFFSET ?",
                                   (*args, limit, offset)).fetchall()
            return {"total": total, "memories": [self.shown(r) for r in rows]}

    def get(self, memory) -> dict:
        self.flush()                             # the owner sees how often it was used, to date
        with self.lock:
            r = self.db.execute("SELECT * FROM memories WHERE id = ?", (int(memory),)).fetchone()
            if not r:
                raise Problem("There is no such memory.")
            past = self.db.execute("SELECT at, who, action, before, after FROM history WHERE memory = ? "
                                   "ORDER BY id DESC LIMIT 50", (r["id"],)).fetchall()
            return dict(self.shown(r), history=[dict(h) for h in past])

    def correct(self, memory, who: str, text=None, subject=None, kind=None, confidence=None, done=None) -> dict:
        """The owner puts a memory right, in place: it is the owner's word
        from then on, and what it said before is in its history. A corrected
        memory is not a second memory beside the wrong one."""
        with self.tx():
            r = self.db.execute("SELECT * FROM memories WHERE id = ?", (int(memory),)).fetchone()
            if not r:
                raise Problem("There is no such memory.")
            new_text = clean(text, TEXT_MAX) if text is not None else r["text"]
            new_subject = clean(subject, SUBJECT_MAX) if subject is not None else r["subject"]
            new_kind = kind if kind in KINDS else r["kind"]
            if len(new_text) < 3:
                raise Problem("A memory has to say something. To be rid of it, delete it.")
            if SECRET.search(new_text) or SECRET.search(new_subject):
                raise Problem("That looks like a password, a key or a token, and those are not kept in memory.")
            # Ticking a task off is not rewording it: whose word it is, and
            # how sure, stay as they were.
            reworded = text is not None or subject is not None or kind is not None or confidence is not None
            self.db.execute(
                "UPDATE memories SET text = ?, subject = ?, subject_key = ?, kind = ?, source = ?, "
                "confidence = ?, updated = ?, done = ? WHERE id = ?",
                (new_text, new_subject, key_of(new_subject), new_kind, "owner" if reworded else r["source"],
                 r["confidence"] if not reworded else
                 1.0 if confidence is None else min(1.0, max(0.05, float(confidence))), time.time(),
                 (time.time() if done else None) if done is not None else r["done"], r["id"]))
            if r["status"] == "active":
                self.index(r["id"], new_subject, new_text)
            self.note(r["id"], who, "corrected" if reworded else "marked done" if done else "marked not done",
                      before=r["text"], after=new_text)
            return self.get(r["id"])

    def set_status(self, memory, who: str, status: str) -> dict:
        with self.tx():
            r = self.db.execute("SELECT * FROM memories WHERE id = ?", (int(memory),)).fetchone()
            if not r:
                raise Problem("There is no such memory.")
            if status == "active":
                # Brought back, it is the memory of its subject again: one that
                # took its place meanwhile gives way, as any older one would.
                for other in self.db.execute("SELECT id, text FROM memories WHERE kind = ? AND subject_key = ? "
                                             "AND status = 'active' AND id != ?",
                                             (r["kind"], r["subject_key"], r["id"])).fetchall():
                    self.db.execute("UPDATE memories SET status = 'superseded', superseded_by = ? WHERE id = ?",
                                    (r["id"], other["id"]))
                    self.index(other["id"], None, None)
                    self.note(other["id"], who, "replaced", before=other["text"], after=f"by {r['id']}")
                self.index(r["id"], r["subject"], r["text"])
            else:
                self.index(r["id"], None, None)
            self.db.execute("UPDATE memories SET status = ?, superseded_by = NULL, updated = ? WHERE id = ?",
                            (status, time.time(), r["id"]))
            self.note(r["id"], who, "restored" if status == "active" else "deleted", before=r["text"])
            return self.get(r["id"])

    def purge(self, memory, who: str) -> bool:
        """Gone for good: the memory, its place in the index, every earlier
        wording of it in its history, and the memories it took the place
        of, which said the same thing an older way. That each was purged,
        and when, is all that stays. False when the text may still be in
        the database's log for a moment (see cut)."""
        with self.tx():
            first = int(memory)
            if not self.db.execute("SELECT 1 FROM memories WHERE id = ?", (first,)).fetchone():
                raise Problem("There is no such memory.")
            gone, look = [first], [first]
            while look:                              # what it replaced, and what those replaced
                look = [r["id"] for r in self.db.execute(
                    f"SELECT id FROM memories WHERE superseded_by IN ({','.join('?' * len(look))})", look)
                    if r["id"] not in gone]
                gone += look
            for i in gone:
                made = self.db.execute("SELECT created FROM memories WHERE id = ?", (i,)).fetchone()["created"]
                self.index(i, None, None)
                self.db.execute("DELETE FROM memories WHERE id = ?", (i,))
                self.db.execute("DELETE FROM history WHERE memory = ?", (i,))
                # A task left open is a notice with the task's words in it:
                # that goes with it, open or over.
                self.db.execute("DELETE FROM notices WHERE key = ?", (f"task:{i}",))
                # When it had been noted down, and nothing of what it said: so
                # that the conversation it came from is not gone through and
                # the thing brought back (see consolidate).
                self.note(i, who, "purged", after=f"{made:.0f}")
        return self.cut()

    def cut(self) -> bool:
        """Cut the write-ahead log back to nothing, so that what was purged
        is not left in it. That can be done only while nothing is reading
        the file: this program's own reader is kept out for it, and if
        another program has the file open (a backup under way) it is tried
        again by the clock until it works. Whether it did."""
        with self.lock, self.rlock:
            busy = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            self.uncut = bool(busy)
        return not busy

    def events(self, limit=100, kind: str = "") -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT at, kind, name, action, ok, ms, via, detail FROM events "
                + ("WHERE kind = ? " if kind else "") + "ORDER BY id DESC LIMIT ?",
                ((kind,) if kind else ()) + (min(max(1, int(limit or 100)), 500),)).fetchall()
            return [dict(r, ok=bool(r["ok"])) for r in rows]

    def stats(self) -> dict:
        with self.lock:
            by = {r[0]: r[1] for r in self.db.execute(
                "SELECT kind, COUNT(*) FROM memories WHERE status = 'active' GROUP BY kind")}
            states = {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM memories GROUP BY status")}
            last = self.db.execute("SELECT MAX(consolidated), COUNT(*) FROM conversations").fetchone()
            return {"active": states.get("active", 0), "superseded": states.get("superseded", 0),
                    "deleted": states.get("deleted", 0), "by_kind": by,
                    "conversations": last[1], "last_consolidated": last[0],
                    "open_tasks": self.db.execute("SELECT COUNT(*) FROM memories WHERE status = 'active' AND "
                                                  "kind = 'task' AND done IS NULL").fetchone()[0],
                    "version": self.db.execute("PRAGMA user_version").fetchone()[0]}


# ── the doors ───────────────────────────────────────────────────────────────

def whole(value, most: int, least: int = 0) -> int:
    try:
        return min(max(least, int(value)), most)
    except (TypeError, ValueError, OverflowError):
        return least


class Desk:
    """What each kind of caller may ask, and nothing else. The role is the
    door the question came in by."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def assistant(self, op: str, r: dict):
        s = self.store
        if op == "recall":
            return {"memories": s.recall(str(r.get("text") or "")[:2000], whole(r.get("limit") or 6, 12, 1),
                                         whole(r.get("budget") or 900, 3000, 100),
                                         r.get("kinds") if isinstance(r.get("kinds"), list) else None)}
        if op == "remember":
            source = r.get("source") if r.get("source") in ("said", "observed", "inferred") else "inferred"
            i, what = s.add(r.get("kind"), r.get("subject"), r.get("text"), source, "douglas",
                            r.get("confidence"), via=r.get("via") or "", conversation=r.get("conversation") or "",
                            model=r.get("model") or "", due=r.get("due"))
            return {"id": i, "what": what, "memory": s.get(i)["text"]}
        if op == "known":
            return {"memories": s.known()}
        if op == "matching":
            found, owners = s.matching(str(r.get("text") or "")[:600], r.get("limit") or 3)
            return {"memories": [{"id": m["id"], "kind": m["kind"], "text": m["text"]} for m in found],
                    "owners": owners}
        if op == "forget":
            return {"forgotten": s.forget(r.get("ids") if isinstance(r.get("ids"), list) else [], "douglas")}
        if op == "consolidate":
            conv = r.get("conversation") if isinstance(r.get("conversation"), dict) else {}
            return s.consolidate(conv, r.get("items"), str(r.get("model") or ""), "douglas")
        if op == "tasks":
            with s.rlock:
                rows = s.rd.execute("SELECT * FROM memories WHERE status = 'active' AND kind = 'task' AND "
                                    "done IS NULL ORDER BY COALESCE(due, updated) LIMIT 12").fetchall()
                return {"memories": [s.shown(x) for x in rows]}
        if op == "cursor":
            # Where in its own record of what was said the pipeline has got to
            # in going through finished conversations: kept here, so that it
            # outlasts the pipeline.
            name = "cursor:" + clean(r.get("name"), 40)
            return {"value": s.state(name, r.get("value")) if "value" in r else s.state(name)}
        if op == "log":
            s.event("change" if r.get("kind") == "change" else "tool", r.get("name") or "",
                    r.get("action") or "", bool(r.get("ok", True)),
                    r.get("ms") or 0, r.get("via") or "", r.get("detail") or "")
            return {}
        if op == "stats":
            return s.stats()
        if op == "notices":
            return s.notices_for_him()
        if op == "notice_told":
            return {"marked": s.marked(r.get("ids"), "spoken" if r.get("aloud") else "told")}
        raise Problem(f"That is not something the assistant's door answers: {op!r}.")

    def owner(self, op: str, r: dict):
        s = self.store
        if op == "list":
            return s.list(str(r.get("kind") or ""), str(r.get("status") or "active"), str(r.get("q") or ""),
                          r.get("limit"), r.get("offset"))
        if op == "get":
            return s.get(r.get("id"))
        if op == "correct":
            return s.correct(r.get("id"), "owner", r.get("text"), r.get("subject"), r.get("kind"),
                             r.get("confidence"), r.get("done"))
        if op == "add":
            i, what = s.add(r.get("kind"), r.get("subject"), r.get("text"), "owner", "owner", via="deck")
            return dict(s.get(i), what=what)
        if op == "delete":
            return s.set_status(r.get("id"), "owner", "deleted")
        if op == "restore":
            return s.set_status(r.get("id"), "owner", "active")
        if op == "purge":
            return {} if s.purge(r.get("id"), "owner") else {"lingering": True}
        if op == "events":
            return {"events": s.events(r.get("limit"), str(r.get("kind") or ""))}
        if op == "stats":
            return s.stats()
        if op == "notices":
            return dict(s.notices(r.get("limit")), prefs=s.prefs())
        if op == "notice_ack":
            return s.ack_notice(r.get("id"))
        if op == "notice_prefs":
            return {"prefs": s.prefs({k: r[k] for k in ("severity", "tell", "aloud", "quiet_from", "quiet_to")
                                      if k in r})}
        if op == "notice_raise":                 # the watch, not Mission Control: the relay does not pass it on
            i, what = s.raise_notice(r.get("key"), r.get("rule"), r.get("subject"), r.get("severity"), r.get("text"))
            return {"id": i, "what": what}
        if op == "notice_clear":
            return {"cleared": s.clear_notice(r.get("key"))}
        if op == "export":
            with s.lock:
                return {"memories": [s.shown(x) for x in s.db.execute(
                    "SELECT * FROM memories ORDER BY id LIMIT 20000").fetchall()]}
        raise Problem(f"That is not something the owner's door answers: {op!r}.")

    def call(self, role: str, req) -> dict:
        op = req.get("op") if isinstance(req, dict) else None
        if not isinstance(op, str):
            return {"ok": False, "text": "A question is one line of JSON with an op in it."}
        began = time.time()
        try:
            out = (self.owner if role == "owner" else self.assistant)(op, req)
        except Problem as e:
            return {"ok": False, "text": str(e)}
        except (TypeError, ValueError, sqlite3.Error) as e:
            log(f"memory: {role} {op} failed: {type(e).__name__}")
            return {"ok": False, "text": f"The memory could not do that ({type(e).__name__})."}
        if op not in ("recall", "log", "cursor", "stats", "tasks", "known", "notices"):   # which, and how long: never the words
            log(f"memory: {role} {op} in {(time.time() - began) * 1000:.0f} ms")
        return dict(out or {}, ok=True)


class Door(socketserver.StreamRequestHandler):
    timeout = 10                                 # a caller that opens the door and says nothing

    def handle(self) -> None:
        try:
            reply = self.server.desk.call(self.server.role, json.loads(self.rfile.readline(262144) or b"null"))
        except Exception as e:                   # noqa: BLE001 - one bad question, not the service
            log(f"memory: a question tripped it: {type(e).__name__}")
            reply = {"ok": False, "text": f"The memory tripped over that ({type(e).__name__})."}
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve() -> None:
    os.umask(0o077)                              # the database is this user's alone to read
    desk = Desk(Store(DB))
    servers = []
    names = (os.environ.get("LISTEN_FDNAMES") or "").split(":")
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and int(os.environ.get("LISTEN_FDS") or 0) >= 1:
        for n, name in enumerate(names):          # systemd made the doors, and says which is which
            if name in ("assistant", "owner"):
                srv = Server(os.path.join(RUN, name + ".sock"), Door, bind_and_activate=False)
                srv.socket.close()
                srv.socket = socket.socket(fileno=3 + n)
                srv.role, srv.desk = name, desk
                servers.append(srv)
    else:                                         # run by hand: the doors are made here
        os.makedirs(RUN, exist_ok=True)
        for name in ("assistant", "owner"):
            path = os.path.join(RUN, name + ".sock")
            try:
                os.unlink(path)
            except OSError:
                pass
            srv = Server(path, Door)
            os.chmod(path, 0o660)
            srv.role, srv.desk = name, desk
            servers.append(srv)
    if not servers:
        sys.exit("douglas-memory: no door to listen at")
    st = desk.store.stats()
    log(f"douglas-memory: {st['active']} memories ({st['superseded']} replaced, {st['deleted']} deleted), "
        f"{st['conversations']} conversations gone through, doors: "
        + ", ".join(s.role for s in servers) + f"; database version {st['version']}")
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def by_the_clock() -> None:              # which memories were used, written down now and then
        while True:
            time.sleep(30)
            try:
                desk.store.flush()
            except sqlite3.Error as e:
                log(f"memory: what was used could not be written down: {type(e).__name__}")

    threading.Thread(target=by_the_clock, daemon=True).start()
    servers[0].serve_forever()


def backup(to: str) -> str:
    """A copy of the database as it stands, through SQLite's own way of
    copying one that is in use: whole, and never half of a change. To go
    back to it: stop the service, put the copy where the database is (and
    remove the -wal and -shm files beside it), start the service."""
    try:                                         # made here, by this, or not at all
        os.close(os.open(to, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    except FileExistsError:
        raise Problem(f"{to} is there already: a backup does not write over anything.") from None
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(to)
        with dst:
            src.backup(dst)
        n = dst.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        dst.close()
    finally:
        src.close()
    return f"{n} memories copied to {to}"


def sound() -> tuple[bool, str]:
    """Is the database whole, and what is in it: for a person to run after a
    crash, a full disk or a restore."""
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        said = [r[0] for r in db.execute("PRAGMA integrity_check")]
        states = dict(db.execute("SELECT status, COUNT(*) FROM memories GROUP BY status"))
        indexed = db.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        rest = (db.execute("SELECT COUNT(*) FROM history").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                db.execute("PRAGMA user_version").fetchone()[0])
    finally:
        db.close()
    ok = said == ["ok"] and indexed == states.get("active", 0)
    return ok, (f"{'sound' if said == ['ok'] else 'NOT SOUND: ' + '; '.join(said)[:300]}. "
                f"{states.get('active', 0)} memories kept ({indexed} of them findable), "
                f"{states.get('superseded', 0)} replaced, {states.get('deleted', 0)} deleted; "
                f"{rest[0]} changes on record, {rest[1]} lines of what he did; version {rest[2]}.")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "serve":
        serve()
    elif what == "backup" and len(sys.argv) == 3:
        try:
            print(backup(sys.argv[2]))
        except (Problem, sqlite3.Error, OSError) as e:
            sys.exit(f"douglas-memory: no backup made: {e}")
    elif what == "check":
        try:
            fine, said = sound()
        except (sqlite3.Error, OSError) as e:
            sys.exit(f"douglas-memory: the database could not be read: {e}")
        print(said)
        sys.exit(0 if fine else 1)
    else:
        sys.exit("usage: douglas-memory.py serve | backup FILE | check")
