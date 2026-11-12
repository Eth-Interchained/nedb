# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
nedb.portal_store — portal-aware engine open for the Python engine.

Step 6b of NEDB Portals (docs/portals.md, design locked 2026-11-12):
`NEDB(path)` learns to serve checkpointed state through portals instead
of rehydrating it into RAM dicts.

  * `portal_checkpoint(db)` — checkpoint IS the sealing event. A real
    `checkpoint` op goes through the log (the chain runs through it; its
    seq is the watermark), then HEAD state is sealed into key-hash
    shards under the Oracle commit protocol: temp paths -> flush/sync ->
    verify -> publish state sidecar -> publish registry (the commit
    point) -> only then retire superseded recovery data. The AOF is
    never retired, truncated, or rewritten (AOF supremacy).

  * `try_portal_open(db)` — the open decision table from docs/portals.md.
    `portal-state.json` is the shards-authoritative marker: when shards
    became authoritative but cannot be served (registry lost, or a
    checkpoint torn before its registry publish), recovery is a FULL
    AOF replay — exact by construction, since this engine never
    truncates its log — never the stale snapshot. Corrupt registry or
    state fails loud (PortalError). Nothing falls back silently.

  * `PortalStoreView` — an MVCCStore whose RAM holds only the
    post-watermark tail. Sealed state resolves through a PortalSet,
    newest-wins with tombstones. Operations portal mode cannot serve
    bounded raise PortalCapabilityError (memory honesty); the single
    deliberate exception is `snapshot()`, which materializes because
    the caller explicitly asked it to — nothing internal calls it.

Encryption (DEK active) refuses portal mode loudly: shards are
plaintext, and an encrypted store must never seal plaintext.
"""
from __future__ import annotations

import bisect
import json
import os
import shutil
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import portal as _portal
from .log import canon
from .store import MVCCStore, TOMB

if TYPE_CHECKING:
    from .engine import NEDB

STATE_FILE = "portal-state.json"
KEYS_FILE = "portal-keys.jsonl"
TMP_DIRNAME = ".portal-tmp"
STATE_VERSION = 1
SHARD_ENTRIES = 65536
ENV_VAR = "NEDB_PORTAL_READS"


class PortalError(Exception):
    """Portal state is corrupt or inconsistent — open/checkpoint fails
    loud rather than falling back to state it cannot prove."""


class PortalCapabilityError(PortalError):
    """An operation portal mode cannot serve within its memory bound.
    Raised explicitly (memory honesty): never a silent materialization."""


def portal_reads_requested(explicit: Optional[bool]) -> bool:
    """One switch, both engines: the constructor flag wins; otherwise
    the NEDB_PORTAL_READS env var (same name as the Rust core)."""
    if explicit is not None:
        return bool(explicit)
    return os.environ.get(ENV_VAR, "") in ("1", "true", "True")


# ---------------------------------------------------------------------------
# Small durable-write helpers (temp -> fsync -> rename -> dir fsync)
# ---------------------------------------------------------------------------

def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _portal._fsync_dir(os.path.dirname(path) or ".")


def _read_json_file(path: str, what: str) -> dict:
    try:
        with open(path, "rb") as fh:
            out = json.loads(fh.read())
    except Exception as exc:
        raise PortalError(f"{what} at {path} is corrupt "
                          f"({type(exc).__name__}: {exc}) — failing loud; "
                          f"recover from the AOF, which is never rewritten")
    if not isinstance(out, dict):
        raise PortalError(f"{what} at {path} is not a JSON object — failing loud")
    return out


# ---------------------------------------------------------------------------
# The store view: RAM tail + sealed portals
# ---------------------------------------------------------------------------

class PortalStoreView(MVCCStore):
    """MVCCStore over (post-watermark tail in RAM) + (sealed shards).

    Writes (put/delete) are inherited: they append version chains in RAM
    exactly as the plain store does. Reads resolve tail-first, then the
    PortalSet, newest-wins with tombstones (invariant 1). The sealed
    doc's own `_seq` is its version anchor, so `last_seq` and CAS stay
    exact for sealed keys; a sealed deletion's seq lives in the state
    sidecar's tombstone map.
    """

    def __init__(self, portal_set: "_portal.PortalSet", watermark: int,
                 tombstone_seqs: Dict[str, int], keys_path: str) -> None:
        super().__init__()
        self._portal_set = portal_set
        self.watermark = watermark
        self._tombstone_seqs = tombstone_seqs
        self._keys_path = keys_path

    # -- sealed resolution --------------------------------------------------
    def _sealed(self, key: str) -> Tuple[str, Optional[dict], Optional[int]]:
        """(state, doc, seq) for a key in the sealed shards."""
        state, raw = self._portal_set.probe(key)
        if state == "present":
            doc = json.loads(raw.decode("utf-8"))
            return ("present", doc, doc.get("_seq"))
        if state == "tombstone":
            return ("tombstone", None, self._tombstone_seqs.get(key))
        return ("absent", None, None)

    # -- reads ---------------------------------------------------------------
    def get(self, key: str, as_of: Optional[int] = None) -> Optional[Any]:
        if as_of is not None and as_of < self.watermark:
            raise PortalCapabilityError(
                f"portal mode: AS OF {as_of} predates the checkpoint "
                f"watermark {self.watermark}; pre-checkpoint history lives "
                f"only in the AOF (replay it with portal reads off)")
        chain = self._v.get(key)
        if chain:
            if as_of is None:
                val = chain[-1][1]
                return None if val is TOMB else val
            i = bisect.bisect_right(self._seqs[key], as_of) - 1
            if i >= 0:
                val = chain[i][1]
                return None if val is TOMB else val
            # every tail version is newer than as_of: the sealed value rules
        state, doc, seq = self._sealed(key)
        if state != "present":
            return None
        if as_of is not None and seq is not None and seq > as_of:
            return None
        return doc

    def last_seq(self, key: str) -> Optional[int]:
        seqs = self._seqs.get(key)
        if seqs:
            return seqs[-1]
        _state, _doc, seq = self._sealed(key)
        return seq

    def keys(self, prefix: str = "", as_of: Optional[int] = None) -> List[str]:
        if as_of is not None:
            raise PortalCapabilityError(
                "portal mode: keys(as_of=...) would need pre-checkpoint "
                "history; replay the AOF with portal reads off instead")
        out: set = set()
        for k in list(self._v.keys()):
            if prefix and not k.startswith(prefix):
                continue
            if self.get(k) is not None:
                out.add(k)
        if self._keys_path and os.path.exists(self._keys_path):
            with open(self._keys_path, encoding="utf-8") as fh:
                for line in fh:
                    k = line.rstrip("\n")
                    if not k or k in out or k in self._v:
                        continue
                    if prefix and not k.startswith(prefix):
                        continue
                    if self.get(k) is not None:
                        out.add(k)
        return sorted(out)

    def snapshot(self, prefix: str = "", as_of: Optional[int] = None) -> Dict[str, Any]:
        """Explicit materialization — the caller's stated intent, never
        used internally. (verify_determinism compares through this.)"""
        if as_of is not None:
            raise PortalCapabilityError(
                "portal mode: snapshot(as_of=...) predates or crosses the "
                "checkpoint watermark; replay the AOF with portal reads off")
        return {k: self.get(k) for k in self.keys(prefix)}

    def close_portals(self) -> None:
        self._portal_set.close()


# ---------------------------------------------------------------------------
# Checkpoint: the sealing event (Oracle commit protocol)
# ---------------------------------------------------------------------------

def _next_seg_base(data_dir: str, registry: Optional["_portal.PortalRegistry"]) -> int:
    """First free segment id: one past the max id referenced by the
    current registry or present on disk, so a new checkpoint generation
    never overwrites the generation that is still authoritative."""
    base = -1
    if registry is not None:
        for e in registry.entries:
            name = os.path.basename(e.segment)
            if name.startswith("seg-") and name.endswith(".dat"):
                try:
                    base = max(base, int(name[4:-4]))
                except ValueError:
                    pass
    for name in os.listdir(data_dir):
        if name.startswith("seg-") and name.endswith(".dat"):
            try:
                base = max(base, int(name[4:-4]))
            except ValueError:
                pass
    return base + 1


def _capture_tombstones(store: MVCCStore) -> Dict[str, int]:
    """{key: delete seq} for every key whose HEAD is a deletion."""
    out: Dict[str, int] = {}
    if isinstance(store, PortalStoreView):
        out.update(store._tombstone_seqs)
    for k, chain in store._v.items():
        if chain and chain[-1][1] is TOMB:
            out[k] = chain[-1][0]
    return out


def _serialize_aux(db: "NEDB") -> Dict[str, Any]:
    """The non-store snapshot state (relations / nonces / idem / blobs),
    serialized exactly as snapshot.py serializes it (plaintext form —
    portal mode refuses DEK stores before this is ever called)."""
    rel_data: Dict[str, Any] = {}
    for (frm, rel), edges in db.relations._adj.items():  # type: ignore[attr-defined]
        rel_data[f"{frm}|{rel}"] = [
            {"to": to, "added": added, "removed": removed}
            for to, added, removed in edges
        ]
    import base64
    blobs_data: Dict[str, Any] = {}
    for tier_name, bs in db.blobs.items():
        blobs_data[tier_name] = {
            "chunks": {h: base64.b64encode(data).decode()
                       for h, data in bs.chunks.items()},
            "files": bs.files,
            "logical_bytes": bs.logical_bytes,
            "dedup_hits": bs.dedup_hits,
        }
    return {
        "relations": rel_data,
        "index_config": [list(t) for t in db.indexes.config],
        "nonces": dict(db.log._last_nonce),
        "idem": {k: v for k, v in db.log._idem.items()},
        "blobs": blobs_data,
    }


def portal_checkpoint(db: "NEDB") -> str:
    """Seal HEAD state into shards and publish them as portals.

    Returns the head hash after the checkpoint op. See docs/portals.md
    for the commit protocol; the steps below are numbered to match.
    """
    if db.path is None:
        raise ValueError("Portal checkpoints require a durable NEDB(path) database.")
    if getattr(db, "_dek", None) is not None:
        raise PortalError(
            "portal checkpoint refused: encryption (DEK) is active and "
            "shards are plaintext — an encrypted store must never seal "
            "plaintext. Use the legacy checkpoint, or disable encryption.")
    data_dir = db.path

    # ── 1. Capture consistent state + AOF watermark: the checkpoint op
    #       goes through the ordinary log, exactly like save_snapshot.
    #       The nonce must strictly exceed the client's last one — a
    #       bare millisecond timestamp collides when two checkpoints
    #       land in the same ms (caught by the script-mode suite). ────
    nonce = max(int(time.time() * 1_000),
                db.log._last_nonce.get("__snapshot__", 0) + 1)
    op, _ = db._log_append(
        client="__snapshot__",
        nonce=nonce,
        op="checkpoint",
        payload={"note": "portal checkpoint"},
    )
    watermark = op.seq
    head = db.head

    store = db.store
    live_keys = sorted(store.keys())
    tomb_seqs = _capture_tombstones(store)
    for k in live_keys:
        tomb_seqs.pop(k, None)  # live again — not a tombstone

    # The registry being replaced (its shards stay authoritative until
    # the commit point, and its files are retired only after).
    old_registry: Optional[_portal.PortalRegistry] = None
    old_files: set = set()
    manifest = os.path.join(data_dir, _portal.MANIFEST_FILE)
    if os.path.exists(manifest):
        old_registry = _portal.PortalRegistry.open(data_dir)
        for e in old_registry.entries:
            old_files.add(e.segment)
            old_files.add(e.idx)
    base = _next_seg_base(data_dir, old_registry)

    # ── 2. Write shards to TEMPORARY paths (bounded batches). ──────────
    tmpdir = os.path.join(data_dir, TMP_DIRNAME)
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir)
    shard_info: List[Tuple[int, int, int]] = []  # (seg_id, seq_min, seq_max)
    batch: Dict[str, bytes] = {}
    batch_tombs: List[str] = []
    batch_seqs: List[int] = []

    def flush_batch() -> None:
        if not batch and not batch_tombs:
            return
        seg_id = base + len(shard_info)
        _portal.write_shard(tmpdir, seg_id, batch, tuple(batch_tombs))
        shard_info.append((seg_id, min(batch_seqs), max(batch_seqs)))
        batch.clear()
        batch_tombs.clear()
        batch_seqs.clear()

    for key in live_keys:
        doc = store.get(key)
        if doc is None:
            continue  # raced a TTL sweep between keys() and get()
        batch[key] = canon(doc)
        batch_seqs.append(int(doc.get("_seq", watermark)))
        if len(batch) + len(batch_tombs) >= SHARD_ENTRIES:
            flush_batch()
    for key in sorted(tomb_seqs):
        batch_tombs.append(key)
        batch_seqs.append(int(tomb_seqs[key]))
        if len(batch) + len(batch_tombs) >= SHARD_ENTRIES:
            flush_batch()
    flush_batch()

    # Move the sealed shards into place under FRESH ids (never over the
    # still-authoritative generation), then fsync the directory.
    for seg_id, _smin, _smax in shard_info:
        os.replace(_portal.seg_path(tmpdir, seg_id),
                   _portal.seg_path(data_dir, seg_id))
        os.replace(_portal.idx_path(tmpdir, seg_id),
                   _portal.idx_path(data_dir, seg_id))
    _portal._fsync_dir(data_dir)
    shutil.rmtree(tmpdir, ignore_errors=True)

    # ── 3. Verify every shard: registration streams each .idx and
    #       checks checksum + sortedness before anything is published. ─
    registry = _portal.PortalRegistry(data_dir)
    shard_checksums: List[str] = []
    for seg_id, smin, smax in shard_info:
        entry = registry.register_segment(
            f"sealed-{seg_id:06d}", seg_id, seq_range=(smin, smax),
            addressing=_portal.ADDRESSING_KEY_HASH)
        shard_checksums.append(entry.idx_checksum)

    # ── 4. Publish the key list + the shards-authoritative marker. ─────
    keys_raw = "".join(k + "\n" for k in live_keys).encode("utf-8")
    _atomic_write(os.path.join(data_dir, KEYS_FILE), keys_raw)
    state = {
        "version": STATE_VERSION,
        "checkpoint_seq": watermark,
        "head": head,
        "live_keys": len(live_keys),
        "tombstones": len(tomb_seqs),
        "tombstone_seqs": tomb_seqs,
        # The commit tie: the exact shard checksums this state was
        # published with. Open serves the registry only if its entries
        # carry precisely these checksums — a registry from any other
        # generation (torn publish, foreign files) is not served.
        "shards": shard_checksums,
    }
    state.update(_serialize_aux(db))
    _atomic_write(os.path.join(data_dir, STATE_FILE),
                  json.dumps(state, separators=(",", ":")).encode("utf-8"))

    # ── 5. COMMIT POINT: publish the registry atomically. ─────────────
    registry.save()

    # ── 6. Retire superseded recovery data (never the AOF). ───────────
    from . import snapshot as _snap
    snap_path = _snap._snap_path(data_dir)
    if os.path.exists(snap_path):
        os.remove(snap_path)
    new_files = set()
    for e in registry.entries:
        new_files.add(e.segment)
        new_files.add(e.idx)
    for name in old_files - new_files:
        try:
            os.remove(os.path.join(data_dir, name))
        except FileNotFoundError:
            pass
    db.flush()  # the checkpoint op is durable before we return
    db._portal_watermark = watermark
    return head


# ---------------------------------------------------------------------------
# Open: the decision table
# ---------------------------------------------------------------------------

def _restore_aux(db: "NEDB", state: dict) -> None:
    """Restore relations / nonces / idem / blobs from the state sidecar,
    mirroring load_snapshot. Index config is deliberately NOT restored
    into live indexes (docs/portals.md: an index config without entries
    would make the eq fast-path silently incomplete); it is stashed on
    the db for reference."""
    db._portal_index_config = state.get("index_config", [])
    for key_str, edges in state.get("relations", {}).items():
        frm, rel = key_str.split("|", 1)
        for e in edges:
            db.relations._adj.setdefault((frm, rel), []).append(
                [e["to"], e["added"], e.get("removed")])
            db.relations._radj.setdefault((e["to"], rel), []).append(
                [frm, e["added"], e.get("removed")])
    # Nonces/idem are NOT restored here: seed_log() merges them with the
    # tail-derived tables after log.load() (which resets both).
    db._portal_seed_nonces = {k: int(v)
                              for k, v in state.get("nonces", {}).items()}
    db._portal_seed_idem = {k: int(v)
                            for k, v in state.get("idem", {}).items()}
    import base64
    for tier_name, bs_data in state.get("blobs", {}).items():
        if tier_name not in db.blobs:
            from .cascade import BlobStore
            db.blobs[tier_name] = BlobStore(tier_name)
        bs = db.blobs[tier_name]
        bs.chunks = {h: base64.b64decode(enc)
                     for h, enc in bs_data.get("chunks", {}).items()}
        bs.files = bs_data.get("files", {})
        bs.logical_bytes = bs_data.get("logical_bytes", 0)
        bs.dedup_hits = bs_data.get("dedup_hits", 0)


def try_portal_open(db: "NEDB") -> Optional[int]:
    """Attempt a portal open. Returns the watermark when portal state
    was loaded into `db` (store swapped for a PortalStoreView); returns
    None when no committed portal checkpoint exists and the caller
    should proceed with a legacy load. Sets `db._portal_force_replay`
    when shards were authoritative but cannot be served — the caller
    must then do a FULL AOF replay and must not trust snapshot.json.
    Raises PortalError on corrupt portal artifacts (fail loud)."""
    data_dir = db.path
    assert data_dir is not None
    manifest = os.path.join(data_dir, _portal.MANIFEST_FILE)
    state_path = os.path.join(data_dir, STATE_FILE)
    has_manifest = os.path.exists(manifest)
    has_state = os.path.exists(state_path)

    if not has_manifest and not has_state:
        return None  # never portaled — plain legacy open
    if has_manifest and not has_state:
        return None  # a registry alone is inert: no checkpoint committed

    # Shards became authoritative at some point (the marker exists).
    # From here the legacy snapshot is NEVER trusted again.
    state = _read_json_file(state_path, "portal state")
    if state.get("version") != STATE_VERSION:
        raise PortalError(
            f"portal state version {state.get('version')!r} unsupported — failing loud")
    watermark = int(state["checkpoint_seq"])

    if not has_manifest:
        print("  [nedb] WARNING: portal state exists but portals.json is "
              "gone — shards were authoritative; recovering by FULL AOF "
              "replay (the snapshot is stale and will not be trusted).")
        db._portal_force_replay = True
        return None

    try:
        registry = _portal.PortalRegistry.open(data_dir)
    except PortalError:
        raise
    except Exception as exc:
        raise PortalError(f"portal registry at {manifest} is corrupt "
                          f"({type(exc).__name__}: {exc}) — failing loud")

    want = sorted(state.get("shards", []))
    have = sorted(e.idx_checksum for e in registry.entries)
    if want != have:
        print("  [nedb] WARNING: portal registry does not match the "
              "committed checkpoint's shard set (a checkpoint was torn "
              "before its registry publish, or the registry is foreign) "
              "— recovering by FULL AOF replay.")
        db._portal_force_replay = True
        return None

    if getattr(db, "_dek", None) is not None:
        raise PortalError(
            "portal open refused: encryption (DEK) is active but this "
            "store was sealed into plaintext shards — failing loud rather "
            "than serving plaintext under an encrypted configuration")

    # Constructing the PortalSet opens every shard's fds and checks
    # idx magic + entry count against the manifest — loud on mismatch.
    portal_set = _portal.PortalSet(registry)
    db._portal_state = state
    _restore_aux(db, state)
    db.store = PortalStoreView(
        portal_set, watermark,
        {k: int(v) for k, v in state.get("tombstone_seqs", {}).items()},
        os.path.join(data_dir, KEYS_FILE))
    db._portal_watermark = watermark
    return watermark


# ---------------------------------------------------------------------------
# The windowed log: tail-only load, seeded state, streaming verify
# ---------------------------------------------------------------------------

def read_tail_ops(db: "NEDB", watermark: int) -> List:
    """Stream the AOF and return only the ops AFTER the watermark.

    Pre-watermark lines are parsed (to reach their seq) and discarded —
    retained memory is the tail, not the history. A corrupt line fails
    loud (PortalError): portal mode does not improvise repairs.
    """
    from . import crypto as _crypto
    from .log import Op
    tail: List = []
    if not os.path.exists(db._aof_path):
        return tail
    with open(db._aof_path, encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, 1):
            line = _crypto.aof_decode(raw_line, db._dek)
            if not line:
                continue
            try:
                op = Op.from_dict(json.loads(line))
            except Exception as exc:
                raise PortalError(
                    f"AOF line {lineno} is corrupt ({type(exc).__name__}: "
                    f"{exc}) — portal mode fails loud; open with portal "
                    f"reads off for the legacy self-repair path")
            if op.seq > watermark:
                tail.append(op)
    return tail


def seed_log(db: "NEDB", tail_ops: List, watermark: int) -> None:
    """Load the tail window into db.log and seed everything the window
    elides: the seq base, the head (when the tail is empty), nonce/idem
    tables merged from the state sidecar, and the AOF lookup used to
    answer idempotent retries of pre-window ops."""
    state = db._portal_state or {}
    log = db.log
    log.load(tail_ops)
    log._seq_base = watermark + 1
    if not tail_ops:
        log._head = state.get("head", log._head)
    for client, n in (db._portal_seed_nonces or {}).items():
        if n > log._last_nonce.get(client, 0):
            log._last_nonce[client] = n
    for k, s in (db._portal_seed_idem or {}).items():
        log._idem.setdefault(k, s)
    log._external_op_lookup = lambda seq: read_op_at_seq(db, seq)
    db._nonce = dict(log._last_nonce)


def read_op_at_seq(db: "NEDB", seq: int):
    """Re-read ONE elided op from the AOF by its seq (rare path: an
    idempotent retry of a pre-window write). Scans by parsed seq, never
    by line position, so blank/corrupt-free assumptions aren't needed."""
    from . import crypto as _crypto
    from .log import Op
    if not os.path.exists(db._aof_path):
        return None
    with open(db._aof_path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = _crypto.aof_decode(raw_line, db._dek)
            if not line:
                continue
            try:
                op = Op.from_dict(json.loads(line))
            except Exception:
                return None
            if op.seq == seq:
                return op
            if op.seq > seq:
                return None
    return None


def stream_verify_aof(db: "NEDB") -> bool:
    """verify() for a windowed log: re-walk the WHOLE chain from the
    AOF on disk, O(1) memory, and compare the computed head against the
    live head. Same guarantee as OpLog.verify(), without holding the
    history in RAM to check it."""
    from . import crypto as _crypto
    from .log import GENESIS, Op, OpLog, blake, canon
    prev = GENESIS
    if os.path.exists(db._aof_path):
        with open(db._aof_path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = _crypto.aof_decode(raw_line, db._dek)
                if not line:
                    continue
                try:
                    op = Op.from_dict(json.loads(line))
                except Exception:
                    return False
                if op.prev_hash != prev:
                    return False
                if op.hash != blake(prev.encode() + canon(OpLog._op_body(op))):
                    return False
                prev = op.hash
    return prev == db.head
