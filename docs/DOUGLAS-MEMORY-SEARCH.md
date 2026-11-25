# Douglas's memory: finding things by their sense

Built 2026-11-25. Douglas's memory (`douglas/douglas-memory.py`) used to find
memories by their words only (SQLite FTS5). It now also finds them by their
meaning, using vectors from a small local embedding model. The memory service
is still the only source of truth: a vector is just one more way to find an
active memory, and nothing can be returned through a vector that recall would
not return anyway.

## What runs where (on Douglas)

| piece | what it is |
|---|---|
| `douglas-embed.service` | `llama-server --embedding` with **EmbeddingGemma 300M Q8_0** (`/srv/models/embed/embeddinggemma-300M-Q8_0.gguf`, 334 MB, from `ggml-org/embeddinggemma-300M-GGUF`). Runs on the CPU only (`--device none`, 4 threads, Nice 5), with no network. It listens on `/run/douglas-embed/embed.sock`, which only the group `douglas-embed` can open, and only `douglas-memory` has that group. |
| `douglas-memory` (schema v3) | Adds an `embeddings` table (one vector per active memory, tagged with the model that made it) and `memories.expires`. It stays stdlib-only, with no network. |
| `douglas/embed-setup.sh [off]` | The installer. `off` switches back to words only. |
| `douglas/embed-eval.py` | The measurement used to choose the model and thresholds. It uses made-up memories only. |
| `douglas/test_douglas_memory_sense.py` | The plumbing tests, run against a fake embedding server. |

Settings live in `/etc/douglas/memory.env`: `EMBED_SOCKET`, `EMBED_DIM=768`,
`EMBED_MIN=0.30` and `EMBED_SAME=0.97`. `EMBED_QUERY` and `EMBED_DOC` default
to EmbeddingGemma's own prompt formats. The model file is set in
`/etc/douglas/embed.env`.

## How recall works now

1. **Words**: the FTS5/bm25 search, unchanged. Exact names and identifiers
   (`KJX-4471`, `Okafor`) are still found by their letters.
2. **Sense**: the question gets a vector, which is compared by cosine
   similarity with every active memory's vector, by brute force. A memory
   found only this way must score at least `EMBED_MIN`.
3. **Merging**: reciprocal rank fusion (k = 60) combines the two lists. The
   result is then weighted by the same factors as before (confidence, source,
   age by kind).
4. **Near-duplicates**: when two memories are at least `EMBED_SAME` similar,
   only the firmer one is returned. An observation never wins this. After
   that comes the stronger source, then the newer memory. Nothing in the
   store is changed; the weaker memory is just not returned that time.
5. Only `active` memories that haven't expired can come out, and tasks
   marked done are left out. Both the word list and the sense list are
   filtered in SQL against `memories.status`.

If the embedding service doesn't answer within 0.5 s, recall falls back to
exactly what it did before, and the service is not tried again for 30 s.
Measured live: 0–2 ms on words only, 15–19 ms with sense.

## Lifecycle

- **New or changed memory**: kept at once with no vector. A background thread
  in douglas-memory, woken by the change, makes the vector. The model is
  called with no lock held. The vector is kept only if the memory still says
  what it said when the model was asked, so a correction that happens in the
  meantime is never shadowed by a vector of the old wording.
- **Correct, replace, forget, delete, let go, purge, expire**: every one of
  these goes through `index()`, which removes the memory's vector in the same
  transaction. A purged memory's vector is destroyed with it
  (`secure_delete`, and the WAL is truncated).
- **Model change**: a vector's tag is the served model's name, the vector
  length and a hash of the prompt formats. If any of those changes, the old
  vectors are ignored and remade in the background. The owner door's
  `reindex` op drops them all on purpose. Mission Control's relay does not
  pass this op through.
- **Backfill**: happens at startup, and every 60 s for anything missed.
- `douglas-memory.py check` counts the vectors and reports **NOT SOUND** if
  any vector belongs to a memory that is not active.

Deduplication at write time is still the strict "said again" rule. Similarity
was **not** used to merge memories on the way in: the measurements below show
that two different facts can score higher than two wordings of the same fact.

## Categories and observations

Each kind belongs to one category: semantic (preference, fact, project,
decision, task), episodic (summary), procedural (lesson), spatial (fleet,
place) or observational (observation). The new kind `place` is for household
context. Recall accepts `categories=[...]`.

**Observations are off** (`MEMORY_OBSERVATIONS=0`). When switched on:

- Only the assistant door can store one, with `source=observed`.
- Confidence is capped at 0.6.
- Each one expires after `MEMORY_OBS_HOURS` (72), and the clock lets it go.
- An observation can replace only an earlier observation of the same subject,
  never a fact.
- The owner can't turn anything into an observation. If the owner rewords
  one, it becomes a fact.
- Recall returns an observation with `observed`, `current: false` and
  `stale_hours`. The voice pipeline shows it as "a camera saw this at …, not
  checked since, so it may no longer be so".

## Measurements (Douglas, 2026-11-25, `embed-eval.py`)

The test set: 38 made-up memories; 32 paraphrase questions that share no
content word with their memory; 8 exact names and identifiers; 10 unrelated
questions; and an owner fact that contradicts an older inference. Each recall
used limit 6 and a 900-character budget, as the voice pipeline asks.

| | paraphrase found 1st / in top 3 | exact | memories returned for unrelated questions |
|---|---|---|---|
| words only (before) | 0.12 / 0.16 | 1.00 | 0 |
| Nomic Embed v1.5, 256 dims, min 0.60 | 0.72 / 0.91 | 1.00 | 0 (6.0 at min 0.45) |
| EmbeddingGemma, 256 dims, min 0.40 | 0.72 / 0.94 | 1.00 | 0 |
| **EmbeddingGemma, 768 dims, min 0.30** | **0.78 / 0.97** | 1.00 | 0 |

- With EmbeddingGemma at 768 dims, a paraphrase scores 0.22–0.7 against its
  memory, and an unrelated question's best match scores at most 0.21. Nomic's
  ranges overlap: unrelated questions reach up to 0.58, and paraphrases start
  at 0.55.
- Real restatements score 0.83–0.99. But "renew the home insurance" and
  "renew the car insurance" score 0.92. That is why `EMBED_SAME` is 0.97
  (near word-for-word only) and similarity is never used to merge memories.
- In every configuration, the owner's correction came first, above the
  older inference.
- Speed on 4 CPU threads: about 12 ms per question, about 76 memories per
  second to embed. The service holds about 470 MB. Brute-force cosine over
  5,000 vectors in Python's `math.sumprod` takes about 10 ms at 256 dims and
  about 30 ms at 768.
- **BGE was not considered**: it comes from BAAI, and Chinese-origin models
  are excluded on this fleet.

## Decisions not taken, and why

- **sqlite-vec**: not needed. The store holds at most `MEMORY_MAX` = 5,000
  active memories, so brute force in the standard library takes tens of
  milliseconds. Loading a native extension into the hardened, stdlib-only
  memory service isn't worth that. The vectors are stored as float32 blobs,
  so sqlite-vec could index them later without converting anything.
- **A separate vector database**: not needed, for the same reason.
- **A reranker**: not justified yet. The right memory is in the top 3 for 97%
  of paraphrase questions, and the model reads all of up to 6 recalled
  notes.
- **Semantic matching for spoken "forget that …"**: not added. Forgetting
  stays lexical and precise.

## Undo

- `sudo ./douglas/embed-setup.sh off` switches back to words only.
- The database from before this change is
  `/var/lib/douglas-memory/before-embed-20261125-2103.db`.
- The old program and settings are in `~/douglas-memory.py.before-embed` and
  `~/memory.env.before-embed` on Douglas. Schema v3 only adds a table and a
  column, so the old program still runs on the new database.
