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
  (KV shards as `key-hash`, content shards as `content-digest`). What is
  NOT done: rewiring the Python engine's own `NEDB(path)` open to a
  portal-aware load that bypasses full materialization of the AOF and
  snapshot. Until that lands, the Python engine still loads in full when
  opened the legacy way — portals give it on-call reads, they do not yet
  change its default open. The Rust conformance tests
  `cross_core_fixture_written_by_python` / `cross_core_fixture_written_by_rust`
  (ignored by default; run with `NEDB_PORTAL_FIXTURE_DIR` /
  `NEDB_PORTAL_FIXTURE_OUT`) prove both cores read each other's shards.
- **Eviction / caching policy.** An open portal is two handles and a count;
  what (if anything) to cache above it is the caller's decision.
- **Migration of pre-existing unsorted `.idx` files.** Registration refuses
  them with a re-seal instruction; an automatic re-seal tool is follow-up.
