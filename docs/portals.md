# NEDB Portals

Status: first slices in both cores (Rust `rust/nedb-v2/src/portal.rs`,
Python `python/nedb/portal.py`), cross-core conformance passing both
directions (2026-11-11). Additive API — no default v2/v3 store path uses
portals yet; behavior is unchanged unless a caller opts in.

## Why

The Python embedded engine rehydrates its whole materialized store on open
(every key/version held in RAM dicts, the whole snapshot reloaded). At
Vision's production scale that open costs 5+ GB RSS and gets OOM-killed.
The data itself is fine — it's the *load-everything-to-open* model that
breaks. Portals invert it: **data stays on disk, sharded; what the engine
holds is only references to the shards.**

## What a portal is

A portal is a reference to one **sealed segment** (`segment.rs`, the v3
packed format) plus its checksummed `.idx` sidecar. A sealed segment + its
`.idx` is a shard. Sharding by reference means shard boundaries are just
portal entries in a registry — creating a shard moves no data and loads
no data; it registers a file pair.

## On-disk formats

### Segment record (unchanged, from segment.rs)

```
segment file:  [u32 len][content][u32 len][content]...
content hash:  BLAKE2b-256(content), re-verified on every portal read
```

### .idx sidecar (unchanged layout, new invariant)

```
[u8;4]  magic "NIX1"
[u64]   entry count N
N ×     entry: [u8;32] digest ‖ [u64] content offset ‖ [u32] len   (44 B)
[u8;32] trailer: BLAKE2b-256(all preceding bytes)
```

**Invariant (new): entries are sorted by raw digest**, ascending. Hex-string
order equals raw-byte order for lowercase hex, so `write_idx` sorts by hash
string at seal time. `load_idx` (the existing reader) is order-insensitive,
so sorting changes nothing for existing callers. Portal registration
stream-verifies sortedness + the trailer checksum once (see below) and
refuses an unsorted sidecar (e.g. one sealed before this change): re-seal
to register it.

### Registry manifest — `portals.json`

Lives in the segments dir it describes. Segment/idx are stored as file
names relative to that dir, so a store dir relocates as a unit.

```json
{
  "version": 1,
  "portals": [
    {
      "id": "h0-99999",
      "segment": "seg-000000.dat",
      "idx": "seg-000000.idx",
      "entries": 100000,
      "seq_min": 0,
      "seq_max": 99999,
      "idx_checksum": "<hex BLAKE2b-256 of the .idx body>",
      "addressing": "content-digest"
    }
  ]
}
```

An entry MAY also carry `"uri": "<base locator>"`. Mark's decision
(2026-11-11): **local now, registry shaped so remote is additive later.**
Absent `uri` (the only implemented case in this slice), `segment`/`idx`
are file names in the registry dir. A future remote portal sets `uri` to
a base location and readers resolve the file names against it — the
`.idx` binary search becomes range reads and the semantics do not
change. Readers that do not implement remote locations MUST refuse a
portal whose `uri` is set, never guess; both cores do exactly that
today, with tests.

`seq_min`/`seq_max` are optional: segments don't track sequence numbers, so
the registering caller supplies the range when it knows it.

### Addressing semantics (explicit, versioned — Oracle review 2026-11-11)

A portal's `.idx` digests follow ONE of two lookup semantics, and the
manifest entry's `addressing` field says which — it is never implied, and a
reader must refuse a semantics it does not implement:

- **`content-digest`** (default, Rust DAG addressing): the digest IS
  BLAKE2b-256 of the record content. A read is addressed by the content's
  own hash and verified against it.
- **`key-hash`** (Python KV addressing): the digest is BLAKE2b-256 of the
  key. Records carry the value's own digest as a prefix and the value is
  verified against it on read.

Both are legitimate; confusing them is not. The cross-core conformance
fixture proves each core retrieves the intended records under the declared
semantics.

### BLAKE2b-256 means NEDB's parameterization

Throughout NEDB's segment layer, "BLAKE2b-256" is the **first 32 bytes of
BLAKE2b-512** (Rust `segment.rs` `blake2b_raw`). That is NOT the same bytes
as a native BLAKE2b-256 digest (e.g. Python `hashlib.blake2b(digest_size=
32)`): BLAKE2b's output length is part of its parameter block. Every
portal digest and checksum in every core must use the truncated-512 form.
The cross-core fixture caught exactly this divergence in the Python
portal's first draft (2026-11-11); it is recorded here so nobody
reintroduces it.

## Costs (the memory bound)

| Operation        | Reads                                            | RAM held          |
|------------------|--------------------------------------------------|-------------------|
| Registry open    | `portals.json` only — segment/idx never touched  | size of manifest  |
| Portal open      | 12-byte idx header + 2 file lengths              | 2 file handles    |
| Registration     | one streaming pass over the `.idx` (never segment) | O(1)            |
| Lookup           | ⌈log₂N⌉ × 44-byte positional idx reads           | one 44-byte buffer|
| Record read      | lookup + 1 positional segment read               | the record itself |

Registration is the only O(N) step, and it happens once at seal time.
After that a cold start's cost is independent of shard size, and a store
of any size opens for the cost of reading a few-KB manifest.

## Invariants (Oracle review, adopted 2026-11-11)

Three engineering invariants govern portals; the bounded-memory promise
stands on them.

**1. The newest truth always wins.** A portal can return perfectly
valid, hash-verified data that is nevertheless *outdated*. Resolution
rule: multi-shard reads consult shards NEWEST FIRST — the active
append-only tail before sealed history, then sealed shards
newest-to-oldest — and the first authoritative answer wins. A key-hash
shard records a deletion as a **tombstone**: an `.idx` entry with
len = `0xFFFFFFFF` (TOMBSTONE_LEN) and no segment record. A tombstone
is authoritative: the search stops, older values never resurface.
Content-digest shards are exempt by construction — a digest names
immutable content, so no shard can hold a newer version of it — and a
tombstone entry found in a content-digest shard is a format violation
that readers refuse. Implemented: `PortalSet` (Python),
`SegmentStore` portal mode (Rust: in-memory active index first, then
portals), with tests in both cores.

**2. Portal registration is crash-safe.** The sequence is
Write → Seal → Verify → fsync → Atomic registry commit. Registration
stream-verifies the `.idx` (checksum + sortedness) BEFORE the manifest
names the shard; `save()` writes the manifest to a temp file, fsyncs
it, atomically renames it over the old manifest, and fsyncs the
directory, in both cores. A crash at any point leaves the old manifest
or the new one — never a torn reference to an unverified shard. An
incomplete shard stays unreferenced and recoverable.

**3. Watch the shard count, not just shard size.** A multi-shard miss
(or a hit in the oldest shard) costs O(S log N) — S shards × a binary
search each. Measured (Python `PortalSet`, 2,000 entries/shard, 300
reps, worst-case oldest-hit and absent keys): median latency grows
linearly at ≈15.5 µs per shard probed — S=1: 17.6 µs, S=10: ~151 µs,
S=50: ~775 µs, S=100: ~1.55 ms, S=200: ~3.13 ms. No redesign is
warranted yet; this is the baseline that says when a digest→shard
routing directory or compaction becomes worthwhile (order ~10³ shards
in Python; the Rust per-probe cost is far lower).

**Bounded memory is an architectural guarantee, not a target.** Open
cost and engine-held memory track the ACTIVE TAIL and the manifest —
never the sealed history. A store with 4M operations opens with
approximately the same application-managed memory as one with 40M,
given a similar configuration and a bounded active tail; historical
data is never fully materialized. Measured in the Rust engine
(`SegmentStore` legacy open vs portal-mode open, 100,000 records,
8 MB segments, 1 sealed shard, 334 verified probe reads in both):
legacy peak RSS 41.1 MB with all 100,000 entries indexed; portal mode
19.6 MB with only the active tail's 25,102 entries indexed. The legacy
number grows with every record ever sealed; the portal number does
not. (Probe: `rss_probe_open_modes`, ignored by default.)

## Lookup integrity

Per-lookup, the `.idx` trailer checksum is *not* re-verified (that would be
an O(N) read — the thing portals exist to avoid). It doesn't need to be:
registration verified it, and every record read is BLAKE2b-verified against
the requested digest, so a corrupt/wrong idx entry can cause at worst a
miss, never wrong bytes.

## How Vision will use this

Vision indexes a blockchain: append-only, naturally height-ordered. The
indexer seals a segment per height range (e.g. 100k blocks) and registers
it as a portal whose id *is* the range (`h0-99999`, `h100000-199999`, …).
Vision's Rust backend then:

1. Opens the registry (KBs, instant — no 5 GB snapshot rehydration).
2. Keeps only the tip portal hot; older portals are opened on call when a
   query names a height/address in their range, served from disk, and can
   be closed/evicted freely since they hold nothing but two handles.
3. Routes by the registry's recorded ranges; shard boundaries are changed
   by editing portal entries, never by moving chain data.

## Portal-aware engine open — Python engine (step 6b, design locked 2026-11-12)

The Rust engine open is portal-aware (`SegmentStore::open_portal_mode`).
This section specifies the Python engine's equivalent: `NEDB(path)` gains
a portal-aware load that serves checkpointed state through portals
instead of rehydrating it into RAM dicts. Design authority: Muse's six
calls (2026-11-12), Oracle ruling 6/6 with the checkpoint-atomicity
amendment, confirmed by Mark. The six calls:

1. **Checkpoint is the sealing event.** A portal-mode checkpoint writes
   the materialized HEAD state as key-hash shards + registry instead of
   a monolithic `snapshot.json`. Open then = registry + the AOF tail
   after the checkpoint watermark. Sealed history on disk, live tail in
   RAM — the Rust shape, in Python's own checkpoint rhythm.
2. **One switch, both engines.** `NEDB_PORTAL_READS=1`, or the
   constructor flag `NEDB(path, portal_reads=True)`; the flag wins.
3. **Gets are portal-native; scans stream bounded.** Nothing silently
   falls back to full materialization. An operation portal mode cannot
   serve bounded returns an explicit capability error.
4. **Python checkpoints are allowed in portal mode** — a documented
   divergence from Rust's compact refusal, because checkpoint is how
   the Python engine makes shards.
5. **Missing registry is legacy only when a complete legacy recovery
   path exists; corrupt registry fails loud** (refined below).
6. **The proof bar** is the acceptance gate at the end of this section.

### What a portal checkpoint writes

Through the ordinary log first: a `checkpoint` op is appended via
`_log_append` exactly as `save_snapshot` does — the chain runs through
it, and its seq is the **watermark**. Then, per key at HEAD:

- live doc → a key-hash shard record whose value bytes are the
  canonical JSON (`canon()`) of the materialized doc, `_seq`/`_hash`/
  `_coll` injections included — byte-for-byte the doc a legacy snapshot
  load would restore;
- deleted doc → a tombstone `.idx` entry (invariant 1), with its seq
  recorded in the state sidecar (below) so `last_seq` stays exact.

Shards are written in bounded batches (default 65,536 entries) to a
temp directory inside the data dir. Non-store snapshot state —
relations, index config, nonces, idem, blobs, tombstone seqs — goes to
a sidecar, `portal-state.json`. The live key list goes to
`portal-keys.jsonl` (one key per line, sorted), so `keys()` can stream
it from disk instead of holding it in RAM.

### The commit protocol (Oracle amendment — atomic publication)

1. Capture consistent state + the AOF watermark (the checkpoint op).
2. Write shards to **temporary paths**; flush and fsync.
3. Verify every shard (`.idx` streaming verify at registration).
4. Publish `portal-keys.jsonl` and `portal-state.json` (temp file,
   fsync, atomic rename, dir fsync).
5. Publish the registry (`PortalRegistry.save` — already atomic:
   temp, fsync, rename, dir fsync). **This is the commit point.**
6. Only then retire superseded recovery data: the previous
   `snapshot.json` and any shard files the new registry no longer
   references. The AOF is never retired, truncated, or rewritten —
   AOF supremacy is absolute.

Until step 5 succeeds, the previously committed store is authoritative
and untouched.

### The shards-authoritative marker, and the open decision table

`portal-state.json` is the marker that shards *became* authoritative:
it exists only if a portal checkpoint reached step 4, and it carries
the watermark. Open decides:

| registry | state sidecar | decision |
|---|---|---|
| valid, shard checksums match the state's commit tie | present | **portal open** (any `snapshot.json` is superseded and ignored) |
| present | absent | legacy open — no portal checkpoint ever committed; the registry alone is inert |
| absent | absent | legacy open, unchanged |
| absent | present | shards were authoritative and the registry is lost → recover by **full AOF replay** (below), loud warning; never open the stale snapshot |
| corrupt / inconsistent with state | any | **fail loud** (`PortalError`); no silent fallback |

The full-replay recovery in row 4 implements the Oracle's intent —
*never silently open stale state* — through this engine's stronger
primitive: the Python engine never truncates its AOF, so replay from
genesis is exact and cannot resurrect anything the log does not say.
(Stores whose recovery path *does* truncate the log — Vision's bounded
store — do not have this primitive; there, registry loss after
authority is fail-closed, full stop.)

### Read contract in portal mode

The RAM store holds only post-watermark versions (the tail) plus new
writes. Reads resolve tail-first, then portals, newest-wins:

- `get(key)` at HEAD: tail version if any, else the portal probe
  (present → doc; tombstone → deleted; absent → absent).
- `get(key, as_of)`: `as_of >= watermark` resolves across tail versions
  and the sealed value (whose `_seq` is the watermark-era version).
  `as_of < watermark` → capability error: pre-checkpoint history lives
  only in the AOF. (A legacy snapshot load has the same effective
  limit; portal mode states it instead of approximating it.)
- `last_seq(key)`: tail seq, else the sealed doc's `_seq`, else the
  sidecar's tombstone seq, else `None`. CAS (`if_seq`) stays exact.
- `keys(prefix)`: tail keys merged with the streamed
  `portal-keys.jsonl`, liveness-resolved per key. Bounded RAM.
- Scans (`execute`, fluent queries): candidates stream from `keys()`;
  values resolve per key by point read. Bounded by construction.
- `store.snapshot()` is the one deliberate exception to memory
  honesty: it materializes because the caller explicitly asked it to.
  Nothing internal calls it in portal mode.
- Indexes: eq/search indexes are **not** restored in portal mode —
  restoring the config without the entries would make the eq fast-path
  silently incomplete, which is worse than no index. Queries fall back
  to streaming scans (correct, bounded). `create_index` in portal mode
  → capability error. Persistent on-disk indexes are a future slice.

Relations, nonces, idem and blobs restore from the sidecar exactly as
from a snapshot. Encryption (DEK active) refuses portal mode loudly:
shards are plaintext, and an encrypted store must never seal plaintext.

### Invariants (Oracle, 2026-11-12 — added to the adopted set)

- **AOF supremacy.** No write is committed-visible unless its durable
  log semantics are satisfied. Checkpointing never invents or loses a
  mutation; portals are a read layer over the log's effects, never a
  write path around it.
- **Memory honesty.** Portal-mode operations cannot secretly
  materialize the database. Unsupported unbounded operations return an
  explicit capability error.
- **Parity includes deletions.** Newest-wins ordering and tombstones
  produce identical results across Python and Rust, including after
  crash recovery.

### The windowed log

Portal open windows the LOG as well as the store — the first RSS
measurement (0.84 of legacy at 200k records) proved that an open which
seals the store but still loads every op has not solved the problem
this work exists to solve. Only AOF ops after the watermark are held
in RAM (`OpLog._seq_base` counts the elided ops, so `len(log)`,
append seqs, and `NEDB.seq` stay absolute and exact). The elided
history is seeded, not forgotten:

- the chain head comes from the tail's last op, or from the state
  sidecar when the tail is empty — appends chain onto it exactly;
- nonce and idempotency tables are restored from the sidecar and
  merged with the tail's (tail wins); an idempotent retry of a
  pre-window op re-reads that op from the AOF by seq (rare, explicit
  cost) and dedups without appending;
- `verify()` re-walks the whole chain by **streaming the AOF from
  disk** — O(1) memory, the same guarantee, none of the residency;
- the open-time self-heal is skipped in portal mode: a window's first
  op chaining from the watermark head is correct, not a structural
  break, and healing it would rewrite the AOF wrongly.

Operations that intrinsically need the resident full log raise
`PortalCapabilityError` in portal mode rather than answer partially:
pre-watermark `AS OF` (store level), `AS OF SYSTEM TIME` resolution,
`TRACE` traversal (it indexes ops positionally), `since()` below the
window, `tip(coll)` when the collection's last write is elided, and
`rebuild()` / `verify_determinism()` (full replay by definition).
Daemon/wrapper surfaces that index `log.ops` positionally
(`server.py`, `wrap_redis.py`) are not portal-mode compatible in this
slice; portal mode is an embedded-open feature.

### Acceptance gate

Fixture: **1.85M records** (Vision's production shape). Portal-open
peak RSS **≤ 20% of the legacy open's peak** on identical data; answer
parity **exact on canonical bytes**; crash recovery verified at
**every commit boundary** of the protocol above. The 5× figure is a
target the fixture must prove, not a claim.

## Not in this slice

- **Writes / an active portal.** Portals are read-only references to
  *sealed* segments. Appends still go through `SegmentStore`'s active
  segment; a portal for it is registered when it seals.
- **Cross-portal queries.** No fan-out search, aggregation, or time-travel
  (`AS OF`) across portals. One portal, one lookup — callers route.
- **Routing by seq/height inside the engine.** The registry records ranges;
  choosing a portal is the caller's job in this slice.
- **Python engine parity.** Done as a sibling slice: `python/nedb/portal.py`
  implements the byte-identical formats for both addressing semantics
  (KV shards as `key-hash`, content shards as `content-digest`). On the
  Rust side, the engine open itself is now portal-aware:
  `SegmentStore::open_portal_mode` (or `NEDB_PORTAL_READS=1`) serves
  sealed segments through portals instead of indexing them in RAM —
  compaction is refused in portal mode in this slice, and a segment
  whose `.idx` fails registration falls back to the legacy in-memory
  index for that segment only. The Python engine's own `NEDB(path)`
  open is now portal-aware too — see "Portal-aware engine open —
  Python engine (step 6b)" above: checkpoint seals shards, open serves
  them on call with a windowed log. The Rust conformance tests
  `cross_core_fixture_written_by_python` / `cross_core_fixture_written_by_rust`
  (ignored by default; run with `NEDB_PORTAL_FIXTURE_DIR` /
  `NEDB_PORTAL_FIXTURE_OUT`) prove both cores read each other's shards.
- **Eviction / caching policy.** An open portal is two handles and a count;
  what (if anything) to cache above it is the caller's decision.
- **Migration of pre-existing unsorted `.idx` files.** Registration refuses
  them with a re-seal instruction; an automatic re-seal tool is follow-up.
