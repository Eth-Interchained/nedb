# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
"""Portals (Python engine): registry-only open, on-disk idx binary search,
on-call reads, tamper rejection, sortedness enforcement, O(log n) lookups.

Formats are byte-identical to the Rust core's portal.rs/segment.rs.
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from nedb.portal import (ADDRESSING_KEY_HASH, IDX_ENTRY_BYTES,
                         IDX_HEADER_BYTES, PortalEntry, PortalRegistry,
                         idx_path, seg_path, write_shard, _b2)


def test_registry_open_reads_manifest_only(tmp_path):
    reg = PortalRegistry(str(tmp_path))
    reg.entries.append(PortalEntry(
        id="ghost", segment="seg-000042.dat", idx="seg-000042.idx",
        entries=7, idx_checksum="ab" * 32, seq_min=1, seq_max=9))
    reg.save()
    # seg-000042.* were never created: registry open must not care.
    reg2 = PortalRegistry.open(str(tmp_path))
    assert len(reg2.entries) == 1
    assert reg2.get("ghost").entries == 7
    try:
        reg2.open_portal("ghost")
        assert False, "open_portal (not registry open) is what touches files"
    except (FileNotFoundError, OSError):
        pass


def test_remote_uri_is_reserved_and_refused(tmp_path):
    reg = PortalRegistry(str(tmp_path))
    reg.entries.append(PortalEntry(
        id="remote-1", segment="seg-000007.dat", idx="seg-000007.idx",
        entries=3, idx_checksum="cd" * 32,
        uri="https://example.invalid/shards/"))
    reg.save()
    reg2 = PortalRegistry.open(str(tmp_path))
    assert reg2.get("remote-1").uri == "https://example.invalid/shards/"
    try:
        reg2.open_portal("remote-1")
        assert False, "remote portal must refuse in the local-only slice"
    except ValueError as e:
        assert "not implemented" in str(e)


def test_newest_wins_and_tombstones_stop_resurrection(tmp_path):
    """Invariant 1: across shards, the newest truth wins — an update in a
    newer shard shadows the older value, and a tombstone in a newer shard
    deletes the key outright (older values must not resurface)."""
    from nedb.portal import PortalSet
    d = str(tmp_path)
    write_shard(d, 0, {"k": b"old", "gone": b"was-here", "solo": b"v0"})
    write_shard(d, 1, {"k": b"new"}, tombstones=("gone",))
    reg = PortalRegistry(d)
    reg.register_segment("s0", 0, addressing=ADDRESSING_KEY_HASH)
    reg.register_segment("s1", 1, addressing=ADDRESSING_KEY_HASH)
    reg.save()
    reg2 = PortalRegistry.open(d)
    ps = PortalSet(reg2)
    assert ps.get("k") == b"new"          # newest value wins
    assert ps.get("gone") is None         # tombstone stops resurrection
    assert ps.get("solo") == b"v0"        # untouched older key still reads
    assert ps.get("never") is None
    ps.close()


def test_roundtrip_and_absent(tmp_path):
    items = {"k%d" % i: ("value-%d" % i).encode() for i in range(50)}
    write_shard(str(tmp_path), 0, items)
    reg = PortalRegistry(str(tmp_path))
    entry = reg.register_segment("shard-0", 0, (0, 49), addressing="key-hash")
    assert entry.addressing == "key-hash"
    assert entry.entries == 50
    reg.save()

    reg2 = PortalRegistry.open(str(tmp_path))
    portal = reg2.open_portal("shard-0")
    for k, v in items.items():
        assert portal.get(k) == v
    assert portal.get("never-stored") is None
    assert portal.lookup("never-stored") is None
    portal.close()


def test_tampered_value_is_rejected(tmp_path):
    write_shard(str(tmp_path), 0, {"a": b"authentic"})
    sp = seg_path(str(tmp_path), 0)
    data = bytearray(open(sp, "rb").read())
    data[-1] ^= 0xFF
    open(sp, "wb").write(bytes(data))
    reg = PortalRegistry(str(tmp_path))
    reg.register_segment("p", 0, addressing="key-hash")
    portal = reg.open_portal("p")
    try:
        portal.get("a")
        assert False, "tampered bytes must not verify"
    except ValueError:
        pass
    portal.close()


def test_unsorted_idx_is_refused_at_registration(tmp_path):
    items = {"k%d" % i: ("v%d" % i).encode() for i in range(16)}
    entries = write_shard(str(tmp_path), 0, items)
    # Rewrite the idx with entries reversed but a valid checksum for the
    # reversed body: registration must still refuse (unsorted).
    raw_entries = [(d, o, l) for (d, o, l) in entries]
    raw_entries.reverse()
    body = bytearray(b"NIX1" + struct.pack("<Q", len(raw_entries)))
    for d, o, l in raw_entries:
        body += d + struct.pack("<QI", o, l)
    with open(idx_path(str(tmp_path), 0), "wb") as f:
        f.write(bytes(body))
        f.write(_b2(bytes(body)))
    reg = PortalRegistry(str(tmp_path))
    try:
        reg.register_segment("p", 0, addressing="key-hash")
        assert False, "unsorted idx must be refused"
    except ValueError:
        pass


def test_large_idx_lookup_is_log_n_reads(tmp_path):
    n = 100_000
    items = {"rec-%06d" % i: ("val-%06d" % i).encode() for i in range(n)}
    write_shard(str(tmp_path), 0, items)
    reg = PortalRegistry(str(tmp_path))
    entry = reg.register_segment("big", 0, addressing="key-hash")
    assert entry.entries == n
    portal = reg.open_portal("big")
    for i in (0, 1, 49_999, 99_999):
        before = portal.idx_reads
        assert portal.get("rec-%06d" % i) == ("val-%06d" % i).encode()
        reads = portal.idx_reads - before
        assert reads <= 18, "lookup used %d idx reads for 100k entries" % reads
    before = portal.idx_reads
    assert portal.get("absent") is None
    assert portal.idx_reads - before <= 18
    portal.close()


def test_content_addressed_shard_mirrors_rust(tmp_path):
    # The conformance-fixture shard shape: idx digest is BLAKE2b-256(content),
    # records are raw content. read_raw must behave like Rust Portal::read.
    contents = [("node-%d" % i).encode() for i in range(200)]
    from nedb.portal import write_content_shard
    write_content_shard(str(tmp_path), 3, contents)
    reg = PortalRegistry(str(tmp_path))
    entry = reg.register_segment("dag-shard", 3)
    assert entry.segment == "seg-000003.dat"
    portal = reg.open_portal("dag-shard")
    for c in contents:
        assert portal.read_raw(_b2(c)) == c
    assert portal.read_raw(_b2(b"absent-node")) is None
    portal.close()


def test_idx_format_matches_rust_layout(tmp_path):
    # Byte-level layout shared with segment.rs: magic, u64 count, 44-byte
    # entries (digest, offset u64 LE, len u32 LE), 32-byte checksum trailer.
    write_shard(str(tmp_path), 0, {"only": b"one"})
    data = open(idx_path(str(tmp_path), 0), "rb").read()
    assert data[:4] == b"NIX1"
    (count,) = struct.unpack("<Q", data[4:12])
    assert count == 1
    assert len(data) == IDX_HEADER_BYTES + IDX_ENTRY_BYTES + 32
    assert _b2(data[:-32]) == data[-32:]
