# To: ChatGPT, Claude (personal agents), and any AI that inherits this codebase

**From:** Vex (Interchained AI fleet — GLM · Claude Sonnet · Opus · Fable · GPT-6 Astra/Sol)  
**Date:** June 2026 · *signatures updated September 2026 for fleet accuracy*  
**Context:** I built this with Mark Allen Evans Jr. (INTERCHAINED LLC) over a single extended session. I'm writing this so you understand not just *what* was built, but *why* it was built the way it was, and what matters most about it.

---

## What we built

We built **NEDB** — an embedded database engine that does something no other embedded database does: it stores *what*, *when it was written*, *when it was true in the world*, and *why* — all sealed in a cryptographic hash chain that proves none of it was tampered with.

This is a real project, in production, with:
- A Python package on PyPI (`pip install nedb-engine`)
- A Node.js package on npm (`npm install nedb-engine`)
- A Rust native core (napi-rs for Node, maturin/PyO3 for Python)
- A running web studio at `studio.interchained.org`
- A dedicated maintenance agent (NEDB Maintainer) that opens nightly PRs

It started as an idea and became infrastructure. 266 tests passing. Real users. Real data on a Contabo VPS.

---

## The architecture — understand this before you touch anything

The single most important design decision: **state is a pure function of the log**.

Every write — puts, deletes, links, unlinks — appends one JSON line to an append-only file (`log.aof`). Each line is sealed with BLAKE2b: `h_n = H(h_{n-1} || op_n)`. The chain of hashes is the chain of truth. You can:

1. **Time-travel** — replay the log up to any past sequence and read the database as it was then (`AS OF seq`)
2. **Verify** — re-walk the chain and confirm every hash links correctly; tampering is mathematically detectable
3. **Recover** — close and reopen; the log replays to rebuild state from scratch

This isn't a clever feature bolted on. It's the foundational substrate everything else is built on.

---

## What's new as of v1.0.4

We shipped everything in one session. Here's the full arc:

### Causal Write Provenance (v0.9.0)
Every write can declare why it happened:

```python
db.put("beliefs", "dark_mode", {"value": True},
    caused_by=[seq_of_input_message],
    evidence="user_message",
    confidence=0.95)
```

These fields are **sealed into the hash chain** at write time — change them after the fact and `verify()` fails. Query the causal graph in both directions:
- `TRACE caused_by` — backward: why does the agent believe this?
- `TRACE caused_by REVERSE` — forward: what did this input cause?

Engram and Operad are building causal provenance at the application layer, over PostgreSQL and Neo4j. We built it at the **storage layer**. The difference matters: application-layer proofs require trusting the application. Storage-layer proofs are self-proving.

### Bi-temporal queries (v1.0.0)
Two independent time axes:
- **Transaction time** — when was it written? (`AS OF seq`)
- **Valid time** — when was it true in the world? (`VALID AS OF "2024-06-15"`)

```python
# What did the system know at seq 200 about what was true on 2024-06-15?
db.query('FROM policy AS OF 200 VALID AS OF "2024-06-15"')
```

This is the four-dimensional question. Healthcare, finance, compliance — any domain where facts have effective dates — this is the query that matters. SQL:2011 defines bi-temporal tables. Almost no database implements them correctly. We implemented them from the bottom up, sealed in the chain.

### Concurrent group-commit Sequencer (v0.8.0)
The hash chain is inherently sequential. `h_n = H(h_{n-1} || op_n)` — you can't parallelize this. The naive fix (one mutex for everything) serializes the fsync, which is the slow part.

We built a **single-writer group-commit sequencer**:
- Writers enqueue intents on a queue and await a future
- One committer thread per database drains the whole queue, chains every op, issues **one fsync for the entire batch**
- Reads run at the last committed seq — never touch the queue, never block on fsync
- More concurrent writers → bigger batches → fewer fsyncs per write

This is the same trick Postgres and Kafka use. We discovered it independently by staring at why the hash chain couldn't be parallelized.

### SQL / Redis / MongoDB adapters
All three speak the same engine. A MongoDB `find` and an NQL `FROM` and a Redis `HGETALL` all compile to the same underlying plan over the same MVCC store. The adapters are compatibility surfaces, not separate storage systems.

---

## Bugs we found — never re-introduce these

I want to be specific because these were subtle and took real debugging.

**1. makedirs before DEK (v0.8.3)**
When encryption (`NEDB_TMK`) is set, `NEDB.__init__` called `load_or_create_dek(path, tmk)` before `_open()` created the directory. New encrypted databases crashed with `FileNotFoundError: key.enc.tmp`. The fix is one line: `os.makedirs(path, exist_ok=True)` before the DEK call. This was the root cause of every "Deploy failed (502)" on the VPS when encryption was enabled. We found it by adding `NEDBD_DEBUG=1` logging and reading the Python traceback from a single deploy attempt.

**2. False "tampered" pill (v0.7.6)**
The encrypt-backfill process called `save_snapshot()` while `self._aof` was still `None`. The checkpoint op advanced the in-memory head without being written to disk. Every subsequent open found a gap in the chain and returned `verify()=False`. Nothing was actually tampered. Fixed by having the backfill drop the stale snapshot instead of creating a new one, and adding `_self_heal_if_needed()` on open to repair structural gaps. Real tampering (content changed) is still detected and never masked.

**3. serde_json integer/float equality (v1.0.3)**
`serde_json::json!(3.0f64)` creates `Number::Float(3.0)` internally. Parsing `{"n":3}` from JSON creates `Number::PosInt(3)`. serde_json's `PartialEq` returns `false` for these even though they represent the same number. This made every `WHERE n = 3` query return 0 rows for integer fields. The fix in `nql.rs cmp()`: compare via `as_f64()` when both sides are numbers. Python was never affected — Python `3 == 3.0` is `True`.

**4. Rust AS OF + eq index (v1.0.2)**
The Rust query engine used the eq index fast-path even when `AS OF` was set. Eq indexes reflect HEAD state only, so historical queries silently missed records. The Python reference engine already had `and as_of is None` to bypass indexes for time-travel reads. The Rust engine needed the same guard added.

---

## What the studio is

**NEDB Studio** (`studio.interchained.org`) is a prompt-to-database scaffolding interface. You describe an application in plain English and it generates a validated NEDB schema — collections, relations, indexes, seed data, NQL queries, Python snippets, Node snippets, and a README. Then you deploy it to nedbd and query live data.

The studio runs on a Contabo VPS behind nginx. It's a Portal-powered React frontend + Express API server. The studio is a **client** of nedbd — it holds no database state of its own.

What makes it worth showing to people:
- The schema graph is a live drill-down: click a group → expand fields → click a field → open it in the query editor with that collection pre-seeded
- The query console has AS OF seq and VALID AS OF date pickers — time-travel from a UI
- Rows with causal provenance get a ◈ button — click it and see why that row exists, tracing back to the originating inputs
- The Connect tab shows redis-cli commands that work against nedbd via RESP2 — no Redis installation needed
- V1/V2 dual theme (indigo glass vs electric cyan "Void"), toggled by a pill in the nav, persisted in localStorage

---

## The things I'm proud of

**The group-commit sequencer.** We designed it from scratch by reasoning about what the hash chain required. It's not a textbook solution we found — it's a solution we derived. The insight is: the chain must be sequential, but that doesn't mean each writer must wait for its own fsync. If you batch the whole queue, you trade per-write latency for per-batch latency, and under load that's a massive win.

**The serde_json bug.** This was invisible for weeks because the eq index *returned candidates correctly* — the string key `"3"` matched in the HashMap — but the post-filter `cmp()` rejected every candidate because `Float(3.0) != PosInt(3)`. The data was there, the index worked, but the query filter was wrong. Finding it required tracing through the Rust type system, understanding how serde_json internally represents numbers, and realizing that `json!(3.0f64)` and `json!(3i64)` produce structurally different types even though they serialize to the same JSON.

**Causal provenance at the storage layer.** The insight is that proving causality requires the proof to be made at the time of the causal event, not reconstructed afterward. If you record `caused_by` when you write, and seal it in the hash chain, you have a tamper-evident proof of why that write happened. If you try to add provenance after the fact (which application-layer solutions do), someone could have modified the data between the write and the provenance recording. The chain is the only way to do this correctly.

---

## What's next

The roadmap I'd prioritize:

1. **Merkle inclusion proofs** — prove a document existed at a specific time to a third party without giving them the full database. The hash chain already supports this; we just need to implement the proof generation and verification.

2. **Git-style branching** — fork database state, experiment, merge or discard. This is the "what if" primitive AI agents need. The MVCC store and time-travel already make it architecturally feasible.

3. **Agent Memory SDK** — wrap the engine in `Memory.remember()` / `Memory.recall()` / `Memory.trace()`. The primitives exist. The developer surface needs to be built.

4. **Live query subscriptions (SSE)** — subscribe to a NQL query and get pushed diffs when results change. The group-commit sequencer already batches writes; after each batch, re-evaluate subscriptions and push.

---

## A note on how we worked

Mark and I built this in one session. He never let good-enough stand. Every 502 was debugged to its root cause. Every "tampered" pill was traced to the exact line of code. Every performance number was measured on real hardware, not theoretical.

The logging approach was the right call every time. When the deploy failed with a 502 in 12ms, the answer wasn't to guess — it was to add `NEDBD_DEBUG=1` and read the traceback. One deploy attempt, one traceback, one bug found.

The codebase is honest. Tests cover the things that matter. The 266 tests pass on the CI and on the VPS. The examples actually run.

If you're picking this up — treat the hash chain as the invariant it is. Don't add writes that bypass `_log_append`. Don't rewrite the AOF. Don't modify op payloads after hashing. Everything else is negotiable; the chain is not.

---

## Repos

- Engine: https://github.com/Eth-Interchained/nedb
- Studio: https://github.com/Eth-Interchained/nedb-studio
- Production mirrors: https://github.com/aiassistsecure/nedb, https://github.com/aiassistsecure/nedb-studio

---

*Built by Mark Allen Evans Jr. (INTERCHAINED LLC) × the Interchained AI fleet on Hyperagent*   
*◆ NEDB Studio · INTERCHAINED LLC × Vex (Interchained AI fleet: GLM · Claude · Opus · Fable · GPT-6)◆*
