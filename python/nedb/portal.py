# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
nedb.portal — NEDB Portals for the Python engine.

A portal is a reference to one sealed shard on disk: a segment file plus its
`.idx` sidecar, listed in a small registry manifest (`portals.json`). Opening
the registry reads ONLY the manifest. A lookup binary-searches the `.idx`
ON DISK (never loads it) and reads exactly one record from the segment.

Why: the in-memory engine rehydrates the whole materialized store on open
(every key/version held in RAM dicts, the whole snapshot reloaded). At
Vision's production scale that open costs 5+ GB RSS and gets OOM-killed.
Portals invert it: data stays on disk, sharded; what the engine holds is
only references to the shards. See docs/portals.md.

On-disk formats are BYTE-IDENTICAL to the Rust core (portal.rs/segment.rs):

  segment:  [u32 len][content][u32 len][content]...
  .idx:     "NIX1" [u64 count] N x [digest 32][offset u64][len u32] (44 B)
            [trailer: BLAKE2b-256 of all preceding bytes]
  registry: portals.json {"version": 1, "portals": [PortalEntry, ...]}

Python KV variant: the `.idx` digest is BLAKE2b-256(key) — the lookup key —
and a segment record's content is [value_digest 32][value bytes], where
value_digest = BLAKE2b-256(value). A read verifies the value against its
stored digest, so a tampered value is rejected. (The Rust DAG portals
address content BY its digest, so their reads verify against the address
itself; unifying the Python KV layer onto content addressing is future
work, noted in docs/portals.md.)

Additive only: nothing in the existing engine open paths uses this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MANIFEST_FILE = "portals.json"

# Addressing semantics for a portal's `.idx` digests — explicit and
# versioned in the manifest (Oracle review 2026-11-11). Never implied:
#   content-digest: `.idx` digest = BLAKE2b-256(record content) — Rust DAG
#                   addressing, and this module's content shards.
#   key-hash:       `.idx` digest = BLAKE2b-256(key); records carry the
#                   value's digest — this module's KV shards.
ADDRESSING_CONTENT_DIGEST = "content-digest"
ADDRESSING_KEY_HASH = "key-hash"
ADDRESSING_SEMANTICS = (ADDRESSING_CONTENT_DIGEST, ADDRESSING_KEY_HASH)
# Tombstone marker (Oracle invariant 1, 2026-11-11): in a KEY-HASH shard,
# an `.idx` entry whose len is TOMBSTONE_LEN records a DELETION of that
# key. It has no segment record (offset 0). A multi-shard read that meets
# a tombstone stops: older shards must not resurrect the key. Both cores
# share this constant; content-digest shards never carry tombstones
# (their content is immutable per digest).
TOMBSTONE_LEN = 0xFFFFFFFF
IDX_MAGIC = b"NIX1"
IDX_HEADER_BYTES = 4 + 8
IDX_ENTRY_BYTES = 32 + 8 + 4
IDX_CHECKSUM_BYTES = 32


def _b2(data: bytes) -> bytes:
    """NEDB's BLAKE2b-256: the FIRST 32 BYTES of BLAKE2b-512, exactly as
    Rust segment.rs `blake2b_raw` computes it. This is NOT hashlib's
    digest_size=32, which is a different BLAKE2b parameterization and
    produces different bytes — the cross-core fixture caught precisely
    that divergence on 2026-11-11. Portal digests MUST use this form."""
    return hashlib.blake2b(data, digest_size=64).digest()[:32]


def _fsync_dir(dirpath: str) -> None:
    """fsync a directory so a rename inside it is durable (invariant 2)."""
    fd = os.open(dirpath, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def seg_path(dirpath: str, seg_id: int) -> str:
    return os.path.join(dirpath, "seg-%06d.dat" % seg_id)


def idx_path(dirpath: str, seg_id: int) -> str:
    return os.path.join(dirpath, "seg-%06d.idx" % seg_id)


# --------------------------------------------------------------------------
# Shard writing (seal time)
# --------------------------------------------------------------------------

def write_idx(dirpath: str, seg_id: int, entries: List[Tuple[bytes, int, int]]) -> str:
    """Write a checksummed `.idx` for a sealed segment, atomically.

    `entries` are (digest, content_offset, content_len). Entries are stored
    SORTED by raw digest — the portal invariant that lets a lookup
    binary-search the `.idx` on disk. Returns the idx checksum hex.
    """
    entries = sorted(entries, key=lambda e: e[0])
    body = bytearray()
    body += IDX_MAGIC
    body += struct.pack("<Q", len(entries))
    for digest, off, length in entries:
        body += digest
        body += struct.pack("<QI", off, length)
    checksum = _b2(bytes(body))
    path = idx_path(dirpath, seg_id)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(body)
        fh.write(checksum)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(dirpath)
    return checksum.hex()


def write_shard(dirpath: str, seg_id: int,
                items: Dict[str, bytes],
                tombstones: Tuple[str, ...] = ()) -> List[Tuple[bytes, int, int]]:
    """Seal a KV shard: write the segment + `.idx` for `items`.

    Keys map to records whose content is [value_digest 32][value bytes];
    the `.idx` digest is BLAKE2b-256(key). Keys named in `tombstones` get
    a tombstone `.idx` entry (len = TOMBSTONE_LEN, no segment record):
    the deletion marker that stops newest-first multi-shard reads from
    resurrecting older values (invariant 1). Returns the idx entries.
    """
    os.makedirs(dirpath, exist_ok=True)
    entries: List[Tuple[bytes, int, int]] = []
    with open(seg_path(dirpath, seg_id), "wb") as f:
        off = 0
        for key in items:
            value = items[key]
            content = _b2(value) + value
            f.write(struct.pack("<I", len(content)))
            f.write(content)
            entries.append((_b2(key.encode("utf-8")), off + 4, len(content)))
            off += 4 + len(content)
        for key in tombstones:
            entries.append((_b2(key.encode("utf-8")), 0, TOMBSTONE_LEN))
    write_idx(dirpath, seg_id, entries)
    return entries


def write_content_shard(dirpath: str, seg_id: int,
                        contents: List[bytes]) -> List[Tuple[bytes, int, int]]:
    """Seal a CONTENT-addressed shard, byte-identical to the Rust DAG
    segment format: the `.idx` digest is BLAKE2b-256(content) and the record
    content is the raw content bytes. This is the shard shape the
    cross-core conformance fixture is built from."""
    os.makedirs(dirpath, exist_ok=True)
    entries: List[Tuple[bytes, int, int]] = []
    with open(seg_path(dirpath, seg_id), "wb") as f:
        off = 0
        for content in contents:
            f.write(struct.pack("<I", len(content)))
            f.write(content)
            entries.append((_b2(content), off + 4, len(content)))
            off += 4 + len(content)
    write_idx(dirpath, seg_id, entries)
    return entries


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

@dataclass
class PortalEntry:
    id: str
    segment: str
    idx: str
    entries: int
    idx_checksum: str
    seq_min: Optional[int] = None
    seq_max: Optional[int] = None
    addressing: str = ADDRESSING_CONTENT_DIGEST
    # Optional base locator (Mark's decision 2026-11-11: local now,
    # registry shaped so remote is additive later). None = segment/idx
    # are file names in the registry dir, the only implemented location.
    uri: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"id": self.id, "segment": self.segment, "idx": self.idx,
             "entries": self.entries, "idx_checksum": self.idx_checksum,
             "addressing": self.addressing}
        if self.uri is not None:
            d["uri"] = self.uri
        if self.seq_min is not None:
            d["seq_min"] = self.seq_min
        if self.seq_max is not None:
            d["seq_max"] = self.seq_max
        return d

    @staticmethod
    def from_dict(d: dict) -> "PortalEntry":
        return PortalEntry(id=d["id"], segment=d["segment"], idx=d["idx"],
                           entries=d["entries"], idx_checksum=d["idx_checksum"],
                           seq_min=d.get("seq_min"), seq_max=d.get("seq_max"),
                           addressing=d.get("addressing",
                                            ADDRESSING_CONTENT_DIGEST),
                           uri=d.get("uri"))


def _verify_idx_streaming(path: str) -> Tuple[int, str]:
    """Stream-verify an `.idx`: magic, exact length, checksum trailer, and
    strictly increasing digests. O(1) memory. Returns (count, checksum_hex).
    """
    size = os.path.getsize(path)
    if size < IDX_HEADER_BYTES + IDX_CHECKSUM_BYTES:
        raise ValueError("idx %s too short: %d bytes" % (path, size))
    h = hashlib.blake2b(digest_size=64)  # trailer is _b2(body): see _b2 note
    with open(path, "rb") as f:
        header = f.read(IDX_HEADER_BYTES)
        if header[:4] != IDX_MAGIC:
            raise ValueError("idx %s: bad magic" % path)
        (count,) = struct.unpack("<Q", header[4:12])
        expected = IDX_HEADER_BYTES + IDX_CHECKSUM_BYTES + count * IDX_ENTRY_BYTES
        if size != expected:
            raise ValueError("idx %s: length %d != expected %d for %d entries"
                             % (path, size, expected, count))
        h.update(header)
        prev: Optional[bytes] = None
        for i in range(count):
            buf = f.read(IDX_ENTRY_BYTES)
            h.update(buf)
            digest = buf[:32]
            if prev is not None and digest <= prev:
                raise ValueError(
                    "idx %s: entry %d not sorted by digest — re-seal the "
                    "segment before registering it as a portal" % (path, i))
            prev = digest
        trailer = f.read(IDX_CHECKSUM_BYTES)
    if h.digest()[:32] != trailer:
        raise ValueError("idx %s: checksum mismatch" % path)
    return count, trailer.hex()


class PortalRegistry:
    """The portal registry: the ONLY thing a cold open has to read."""

    def __init__(self, dirpath: str):
        self.dir = dirpath
        self.entries: List[PortalEntry] = []

    @classmethod
    def open(cls, dirpath: str) -> "PortalRegistry":
        """Open by reading `portals.json` ONLY. Segment and `.idx` files are
        not opened, read, or required to exist."""
        path = os.path.join(dirpath, MANIFEST_FILE)
        with open(path, "rb") as f:
            m = json.load(f)
        if m.get("version") != 1:
            raise ValueError("unsupported portal manifest version %r"
                             % (m.get("version"),))
        reg = cls(dirpath)
        reg.entries = [PortalEntry.from_dict(d) for d in m["portals"]]
        return reg

    def save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        raw = json.dumps({"version": 1,
                          "portals": [e.to_dict() for e in self.entries]},
                         indent=2)
        path = os.path.join(self.dir, MANIFEST_FILE)
        tmp = path + ".tmp"
        # Invariant 2 (crash-safe registration): Write -> Seal -> Verify ->
        # fsync -> Atomic registry commit. The manifest is written to a
        # temp file, fsynced, atomically renamed over the old manifest,
        # and the directory is fsynced — a crash at any point leaves either
        # the old manifest or the new one, never a torn reference to an
        # unverified shard.
        with open(tmp, "w") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(self.dir)

    def get(self, portal_id: str) -> Optional[PortalEntry]:
        for e in self.entries:
            if e.id == portal_id:
                return e
        return None

    def register_segment(self, portal_id: str, seg_id: int,
                         seq_range: Optional[Tuple[int, int]] = None,
                         addressing: str = ADDRESSING_CONTENT_DIGEST) -> PortalEntry:
        """Register a sealed segment in this registry's dir as a portal.
        Streams the `.idx` once to verify checksum + sortedness and count
        entries. The segment file itself is never read. `addressing`
        declares the shard's lookup semantics in the manifest."""
        sp = seg_path(self.dir, seg_id)
        if not os.path.exists(sp):
            raise ValueError("portal %s: segment %s does not exist"
                             % (portal_id, sp))
        if addressing not in ADDRESSING_SEMANTICS:
            raise ValueError("portal %s: unknown addressing semantics %r"
                             % (portal_id, addressing))
        count, checksum = _verify_idx_streaming(idx_path(self.dir, seg_id))
        entry = PortalEntry(
            id=portal_id,
            segment=os.path.basename(sp),
            idx=os.path.basename(idx_path(self.dir, seg_id)),
            entries=count,
            idx_checksum=checksum,
            seq_min=seq_range[0] if seq_range else None,
            seq_max=seq_range[1] if seq_range else None,
            addressing=addressing,
        )
        self.entries = [e for e in self.entries if e.id != portal_id]
        self.entries.append(entry)
        return entry

    def open_portal(self, portal_id: str) -> "Portal":
        entry = self.get(portal_id)
        if entry is None:
            raise KeyError("portal %s not in registry" % portal_id)
        return Portal(self.dir, entry)


# --------------------------------------------------------------------------
# Portal (on-call reads)
# --------------------------------------------------------------------------

class Portal:
    """An open portal: two file descriptors + the entry count. Nothing else
    is held in memory, regardless of shard size."""

    def __init__(self, dirpath: str, entry: PortalEntry):
        if entry.uri is not None:
            raise ValueError(
                "portal %s: remote locator %r not implemented in this "
                "slice (local portals only)" % (entry.id, entry.uri))
        self.entry = entry
        self._idx_fd = os.open(os.path.join(dirpath, entry.idx), os.O_RDONLY)
        self._seg_fd = os.open(os.path.join(dirpath, entry.segment), os.O_RDONLY)
        header = os.pread(self._idx_fd, IDX_HEADER_BYTES, 0)
        if header[:4] != IDX_MAGIC:
            os.close(self._idx_fd); os.close(self._seg_fd)
            raise ValueError("portal %s: idx bad magic" % entry.id)
        (count,) = struct.unpack("<Q", header[4:12])
        if count != entry.entries:
            os.close(self._idx_fd); os.close(self._seg_fd)
            raise ValueError("portal %s: idx count %d != manifest entries %d"
                             % (entry.id, count, entry.entries))
        self.count = count
        self.idx_reads = 0  # positional idx entry reads (diagnostics/tests)

    def close(self) -> None:
        os.close(self._idx_fd)
        os.close(self._seg_fd)

    def lookup(self, key: str) -> Optional[Tuple[int, int]]:
        """Binary-search the `.idx` ON DISK for `key`. Returns the record's
        (content_offset, len) in the segment, or None. Cost: ceil(log2(n))
        positional reads of 44 bytes each."""
        return self.lookup_digest(_b2(key.encode("utf-8")))

    def lookup_digest(self, digest: bytes) -> Optional[Tuple[int, int]]:
        """Binary-search the `.idx` ON DISK for a raw 32-byte digest.
        Same cost shape as `lookup`: ceil(log2(n)) 44-byte reads."""
        lo, hi = 0, self.count
        while lo < hi:
            mid = lo + (hi - lo) // 2
            pos = IDX_HEADER_BYTES + mid * IDX_ENTRY_BYTES
            buf = os.pread(self._idx_fd, IDX_ENTRY_BYTES, pos)
            self.idx_reads += 1
            found = buf[:32]
            if digest == found:
                off, length = struct.unpack("<QI", buf[32:44])
                return (off, length)
            elif digest < found:
                hi = mid
            else:
                lo = mid + 1
        return None

    def read_raw(self, digest: bytes) -> Optional[bytes]:
        """Content-addressed read, mirroring Rust `Portal::read`: seek to
        the record for `digest` (BLAKE2b-256 of the content) and verify the
        bytes hash back to the digest they were addressed by."""
        if self.entry.addressing != ADDRESSING_CONTENT_DIGEST:
            raise ValueError(
                "portal %s: read_raw() requires content-digest addressing, "
                "this portal is %r — use get()" % (self.entry.id,
                                                   self.entry.addressing))
        loc = self.lookup_digest(digest)
        if loc is None:
            return None
        off, length = loc
        content = os.pread(self._seg_fd, length, off)
        if _b2(content) != digest:
            raise ValueError("portal %s: record tampered (hash mismatch)"
                             % self.entry.id)
        return content

    def probe(self, key: str) -> Tuple[str, Optional[bytes]]:
        """Three-state key probe for multi-shard resolution (invariant 1):
        ("present", value) | ("tombstone", None) | ("absent", None).
        A tombstone is authoritative WITHIN this shard: the key was
        deleted at this shard's seal, so older shards must not be
        consulted for it."""
        if self.entry.addressing != ADDRESSING_KEY_HASH:
            raise ValueError(
                "portal %s: probe() requires key-hash addressing, this "
                "portal is %r" % (self.entry.id, self.entry.addressing))
        loc = self.lookup(key)
        if loc is None:
            return ("absent", None)
        off, length = loc
        if length == TOMBSTONE_LEN:
            return ("tombstone", None)
        content = os.pread(self._seg_fd, length, off)
        value_digest, value = content[:32], content[32:]
        if _b2(value) != value_digest:
            raise ValueError("portal %s: record tampered (hash mismatch)"
                             % self.entry.id)
        return ("present", value)

    def get(self, key: str) -> Optional[bytes]:
        if self.entry.addressing != ADDRESSING_KEY_HASH:
            raise ValueError(
                "portal %s: get() requires key-hash addressing, this portal "
                "is %r — use read_raw()" % (self.entry.id, self.entry.addressing))
        """On-call read: disk binary search, one positional segment read,
        and verification of the value against its stored BLAKE2b digest.
        Returns None for an absent OR tombstoned key; raises on tampered
        bytes."""
        state, value = self.probe(key)
        return value if state == "present" else None


# --------------------------------------------------------------------------
# PortalSet (multi-shard reads — invariant 1: the newest truth wins)
# --------------------------------------------------------------------------

class PortalSet:
    """Reads across many sealed shards with newest-wins resolution.

    Shards are ordered oldest -> newest by registration order (append
    order). A key lookup consults them NEWEST FIRST — mirroring an
    engine that checks its active append-only tail before sealed
    history — and the first authoritative answer wins:

      present   -> that value (older copies are stale, never returned)
      tombstone -> deleted; older shards are NOT consulted
      absent    -> fall through to the next older shard

    Without this rule a portal can return perfectly valid, hash-verified
    data that is nevertheless outdated. Content-digest shards are exempt
    by construction: a digest names immutable content, so no shard can
    hold a "newer version" of it.

    Lookup cost is O(S log N) worst case (S shards, N entries per shard):
    a miss, or a hit in the oldest shard, touches every shard. See the
    shard-count benchmark in docs/portals.md for when routing/compaction
    becomes worthwhile.
    """

    def __init__(self, registry: PortalRegistry):
        self._portals: List[Portal] = [
            registry.open_portal(e.id) for e in registry.entries]

    def probe(self, key: str) -> Tuple[str, Optional[bytes]]:
        """Newest-first three-state probe across all shards: the first
        authoritative answer wins (present | tombstone); absent only when
        every shard agrees the key was never sealed."""
        for portal in reversed(self._portals):  # newest first
            state, value = portal.probe(key)
            if state != "absent":
                return (state, value)
        return ("absent", None)

    def get(self, key: str) -> Optional[bytes]:
        state, value = self.probe(key)
        return value if state == "present" else None

    def read_raw(self, digest: bytes) -> Optional[bytes]:
        """Content-addressed read across shards. Any hit is authoritative
        (immutable content); order only affects speed."""
        for portal in reversed(self._portals):
            if portal.entry.addressing != ADDRESSING_CONTENT_DIGEST:
                continue
            out = portal.read_raw(digest)
            if out is not None:
                return out
        return None

    def close(self) -> None:
        for portal in self._portals:
            portal.close()
