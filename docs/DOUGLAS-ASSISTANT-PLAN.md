# Douglas as a persistent assistant: findings and plan

Douglas answers well inside one conversation and forgets it five minutes
later; he can say nothing about the machines around him or about himself.
This is the plan for changing that without changing what he is allowed to
do. The rule throughout: **broad visibility, narrow authority.** He gets to
see and to remember; he does not get to act on anything he could not act on
before.

Phases 1 and 2 are built and running (the README's "A memory" and "What he
can see of the machines, and of himself" say how they work and what was
measured). Phases 3 to 6 are designed here and wait for a go-ahead.

## 1. What was found

**His tools today** (all in `douglas/douglas-voice.py`, offered only when
their settings are present): weather, news, Wikipedia, read a web page, web
search, Reddit, look at a picture, draw, edit a picture, the owner's records
on jobContext (stories and tone samples only, changes on a yes), and the
workshop (clone, read, issues, jobs, studies, push on a yes). What a message
asks for is settled by the program with a one-line question to the model;
actions that change something are read back and need a yes.

**What he remembers today:** six exchanges, in the pipeline's memory, gone
after five idle minutes or a restart. Everything longer-lived is static
text the owner wrote (`DOUGLAS_FACTS`, `/etc/douglas/instructions.d`). There
is no database anywhere in the fleet. The one durable record of what was
said is `/var/lib/douglas/transcript.jsonl`, appended by the pipeline for
the screens.

**What the fleet already reports**, and to whom:

| Machine | Known today | Missing |
|---|---|---|
| Douglas | CPU, memory, uptime, package temperature and power, GPU load and memory, model state, name, context, tokens per second, requests in flight (`douglas-agent` `/status`) | disk, unit health and restarts, the other model servers, versions |
| node1 (Pi 4) | temperature, load, memory, uptime, CPU (deck-api reads its own `/proc`) | disk, k3s workloads |
| Workstation | which system is up, since when; GPU and, where an exporter matches, CPU and memory | disk, uptime, services |
| DougVoice (Pi Zero) | reachable, ssh, satellite port | everything else: it runs no agent |
| Mac mini, MacBook | reachable, ssh, screen sharing | everything else |

(That is how it stood before phase 2. What phase 2 added, and what is still
not seen after it, is under that phase below.)

deck-api polls all of it every five seconds into one state object
(`GET /api/state`, pushed on `/api/events`), keeps nothing but the latest
values and the last forty events, and writes nothing to the Pi's drive, by
design (`DESIGN.md`).

**What stood in the way:**

- **One token, one scope.** `DECK_TOKEN` opens every route, and some of
  those boot and shut down the workstation. Douglas must not hold it. There
  was no read-only scope.
- **Douglas could not see his own machine.** His agent's numbers go to the
  panel, behind a token the pipeline does not have.
- **Not every interface is the pipeline.** The phone, the panel, the desk
  and the voice satellite all go through `douglas-voice`. The ChatGPT
  connector and Open WebUI talk to the model server directly: no tools, no
  instructions, and so no memory until they are brought through the
  pipeline (phase 5).
- **Operator traffic looks like the owner.** Test turns sent to the
  pipeline are indistinguishable from the owner's in the transcript.

**jobContextMCP** was read for memory patterns, as asked. It has no
long-term memory of the kind wanted here (nothing is extracted or
consolidated, nothing carries confidence), so there was no architecture to
lift. What it has learned the hard way is taken over as rules:

| Lesson there | Rule here |
|---|---|
| A session-start dump of everything grows without bound | Recall is by relevance, under a budget, every turn |
| Greedy packing that skips an oversized item instead of stopping | The same, in `recall` |
| "A superseding entry just leaves the wrong one sitting there" | The owner's correction changes the memory in place, with its history |
| Deleted stories came back as ghost rows (two stores, one pruned) | One store, in transactions; a delete is a status, a purge is a delete |
| A cached guess "permanently poisoned every future call" | A weaker source never replaces a stronger; lessons come only from outcomes that were observed |
| Retrieved text framed as the source of truth, with no data boundary | Recalled memories are handed over marked as notes, never instructions |
| Embeddings computed in somebody's cloud | Full-text search on the machine; no embeddings to start with |

## 2. Decisions

1. **Memory is a service of its own** (`douglas-memory`): its own user, no
   network, one SQLite file. Not a module of the pipeline: it has to
   outlast the pipeline, be reachable by other interfaces later, and be
   correctable by the owner without going through the model.
2. **Two doors, told apart by the file system.** The pipeline's door can
   recall, note, hand in a finished conversation, mark as forgotten and
   append to the log. The owner's door (Mission Control, through
   `douglas-agent`) can list, correct, delete, restore, purge and read the
   log. Douglas cannot read the whole memory, reword a memory, or undo a
   deletion, and cannot read or rewrite the record of his own tool calls.
   Each door is a socket with a group of its own, given to one unit: seven
   services run as the pipeline's user, and only the pipeline opens its door.
3. **Sentences, not vectors.** A memory is a kind, a subject, a sentence or
   two, who vouches for it, how sure, when, from where. Retrieval is SQLite
   full-text search weighed by confidence, source and age. Nothing in the
   store depends on which model is loaded; an embedding index can be added
   beside it later without changing it.
4. **Consolidation reads the transcript the pipeline already keeps**, from
   a cursor held in the memory service. A conversation is gone through once
   it has been quiet for the idle time: one model call, only for
   conversations that could hold something durable, never while someone is
   talking. Restart-proof and model-change-proof for free: whatever model
   is up next picks up where the cursor stands.
5. **Only the person's words are evidence.** What a web page, a
   repository, an issue or a record said is not remembered as fact, and
   every memory the model proposes has to be made of words the person used.
   Secrets are refused at the door.
6. **Newer does not simply win.** Same kind and subject, and nothing in it
   that the old one does not say: the same memory said again, which gets
   surer. Anything else in it (another date, a "not", one more detail): the
   new one stands and the old is kept, marked replaced. Sharing most of their
   words is not the test, because that is exactly what a changed date looks
   like. Unless the old one came from a stronger source
   (the owner's correction beats what was said, which beats what was
   inferred): then it stands, and the contradiction is written beside it.
7. **Fleet sight goes through deck-api, with a token of its own.**
   `DECK_READ_TOKEN` opens one route, `GET /api/fleet`, and nothing else.
   deck-api stays the one place that knows the fleet; nothing new polls.
8. **Self-sight goes through his own agent**, over a socket on the machine
   that serves vitals only: no transcript, no relays, no token to hold.
   It does not depend on the Pi being up.
9. **No shell, no SSH, no commands.** Every new tool reads one fixed
   thing. What he may ask of it is decided by the program from the
   person's message, as with the workshop.
10. **The log of what he did is not his to edit.** Tool calls and state
    changes are appended through the assistant's door and read through the
    owner's.
11. **MCP where it earns its place.** Inside the pipeline a tool is a
    function call, and an MCP hop would add a server to keep alive and
    nothing else. MCP is how *other* clients will reach the same memory and
    fleet view (phase 5), from the same two services.
12. **Nothing durable on the Pi.** Memory and log live on Douglas's disk.

## 3. Phases

### Phase 1: persistent memory (built)

- `douglas-memory` service, schema with history, two doors.
- Recall on every turn, budgeted, marked as notes.
- Consolidation from the transcript; "remember that", "forget that" and
  "what do you remember about" settled by the program; forgetting is read
  back and needs a yes.
- Mission Control: inspect, search, correct, delete, restore, purge; the
  log of tool calls.
- Operator turns (`via: "ops"`) are never consolidated, and can neither
  note anything down nor have anything forgotten.
- A backup that is safe to take while it runs, and a check that the
  database is sound.

*Depends on:* nothing new. *Risks:* a wrong memory repeated with
confidence (met by provenance on every recalled note, in-place correction,
and a weaker source never overriding a stronger); a planted memory (met by
decision 5 and by marking notes as data); the model's time (one call per
finished conversation, skipped for small talk); a growing database (a cap,
with the least sure and longest unused let go first).

*Accepted when:* a fact stated in one conversation is used in another after
the pipeline, the memory service and the model server have each been
restarted; a correction made in Mission Control is what he says next; a
deleted memory is not recalled and a purged one is gone from the file; text
from a lookup is not remembered; the assistant's door refuses every owner
operation; the suite passes with a different model name in the settings.
*All of it was seen on Douglas, with the model he runs on.* Not done: a
reboot of the machine (each service was restarted; the units are enabled).

### Phase 2: read-only fleet and runtime awareness (built)

- deck-api: `DECK_READ_TOKEN` and `GET /api/fleet` (machines, roles, up or
  not, last seen, what each reports, recent events, versions).
- `douglas-agent`: disk, unit health and restarts, the other model servers,
  models on disk, installed versions; a vitals socket for the pipeline.
- Tools `fleet` and `self`, program-routed; Mission Control shows the same.
  Neither tool takes an argument: there is no host, path or command to name.
- The agent runs as a user of its own now, not one systemd makes up at each
  start: the system bus turns those away, and it has to ask systemd how the
  services are. Same restrictions, a fixed name.

*Still not seen after it:* the Pi's k3s workloads; anything from DougVoice
and the two Macs beyond whether they answer (they run no agent); the
workstation's disk and uptime. Nothing is kept of a reading, so there is no
"was it hot last night": that is phase 3's.

*Depends on:* phase 1 only for the log. *Risks:* a read token that opens
more than a read (met by an allow-list of one route, tested from both
sides); stale numbers stated as current (every reading carries its age, and
he is told to say it); the Pi being down (self-sight does not go through
it).

*Accepted when:* he answers "what is running here", "is the workstation
up", "how hot are you", "which model are you" from readings taken at that
moment, says so when a machine reports nothing, and the read token is
refused on every other route. *All of it was seen live*, the last from
Douglas itself against eighteen routes.

### Phase 3: proactive notices (designed)

A watcher in deck-api, where the poll already is: rules over state
*changes* (a machine gone, a temperature over a line for a time, a unit
failed, disk under a floor, a task due), each with a severity, a key for
deduplication and a cool-down. No model is asked anything to raise a
notice; one is asked only to word a digest when the owner wants one. Notices
go to the panel and the phone, to Douglas as news for the next turn, and
(by preference, per severity) aloud. Kept on Douglas, not on the Pi.

*Accepted when:* pulling a machine's plug raises one notice, not one per
poll; a flapping reading raises none inside its cool-down; quiet hours and
per-severity preferences hold; a day of normal running raises nothing.

### Phase 4: tasks, approvals, capability switches (designed)

What is read back today and answered with a yes becomes an approval that
also shows in Mission Control, with what it would do and until when. Tasks
he has taken on are listed with their state. Each tool gets a switch the
owner can turn off from the panel; the switch file is the owner's to write
and the pipeline's only to read.

*Accepted when:* an approval can be given or refused from the panel and is
void after its time; a tool switched off is not offered within one turn and
cannot be switched on from a conversation.

### Phase 5: one Douglas on every interface (designed)

The ChatGPT connector and Open WebUI go through the pipeline, so that they
share instructions, tools and memory. An MCP face on the memory's and the
fleet's read operations for desktop clients.

### Phase 6: the house (designed, nothing granted)

Awareness first: what devices exist and their state, read from what already
knows (the emulated devices on the Pi are the starting list). Control only
after that has proved itself, device by device, each behind an approval, and
never through a general "run this" tool. No device control is part of
phases 1 to 5.

## 4. What needs the owner

- Phases 3 to 6: a go-ahead each.
- A reboot of Douglas to prove the units come back by themselves (they are
  enabled; the restart of each service was tested, a reboot was not).
- Whether to consolidate the conversations from before memory existed. They
  are in the transcript; so are a great many operator test turns, which is
  why it was not done.
- Whether what the owner wrote down in Mission Control should give way to
  something said later from the owner's own phone. As built it does not:
  the correction stands and he says where to change it. That is the safe
  reading of "the owner's word", and it is a one-line change if it proves a
  nuisance.

## 5. Rolling back

Each phase is additive and has an off switch. `douglas/memory-setup.sh off`
stops and disables the memory service and takes its settings and the two
units' groups away again; the pipeline then runs exactly as before (it
treats a missing memory as no memory). The database stays where it is, and
`douglas-memory.py backup` takes a copy of it at any time.
`pi/fleet-read.sh off` on the Pi removes the read token, which closes
`/api/fleet` to whoever holds it; taking `FLEET_URL` and `FLEET_TOKEN` out
of `voice.env` takes the `fleet` tool away, and `VITALS_SOCKET` the `self`
tool. Each takes effect when `douglas-voice` is next restarted. The programs
and settings from before each phase are kept in the home directory of the
account that installed them, on Douglas and on the Pi.
