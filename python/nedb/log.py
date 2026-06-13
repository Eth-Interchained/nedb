"""
nedb.log — the append-only, hash-chained, nonce-enforced, idempotent operation log.

This is the single source of truth for NEDB. Every mutation in the database is an
Op appended here. Three guarantees live in this one structure:

  * Replay protection  — each client has a strictly-monotonic nonce; an op whose
                         nonce is <= the client's last seen nonce is rejected.
  * Idempotency        — an op carrying an idempotency key that was already applied
                         returns the original result and is NOT appended again.
  * Tamper evidence    — ops are chained by hash (h_n = H(h_{n-1} || op_n)), so the
                         whole history is a verifiable chain and the head hash is a
                         commitment to the entire log (anchorable on a blockchain).

The same log is the substrate for MVCC snapshot isolation, crash recovery, and
time-travel reads: every Op has a monotonic `seq`, and state "AS OF seq N" is just
the log truncated at N.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

GENESIS = "0" * 64


def canon(obj: Any) -> bytes:
    """Deterministic canonical encoding for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def blake(data: bytes) -> str:
    # Reference uses BLAKE2b (stdlib). The production Rust core uses BLAKE3
    # (faster, natively tree-structured for the Merkle history).
    return hashlib.blake2b(data, digest_size=32).hexdigest()


class ReplayError(Exception):
    """Raised when an op is replayed with a stale/duplicate nonce."""


@dataclass
class Op:
    seq: int
    client: str
    nonce: int
    op: str  # put | delete | link | unlink | put_file
    payload: dict
    ts: float
    idem: Optional[str]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        """Serialize for the append-only log file (AOF)."""
        return {
            "seq": self.seq, "client": self.client, "nonce": self.nonce,
            "op": self.op, "payload": self.payload, "ts": self.ts,
            "idem": self.idem, "prev_hash": self.prev_hash, "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Op":
        return cls(
            d["seq"], d["client"], d["nonce"], d["op"], d["payload"],
            d["ts"], d.get("idem"), d["prev_hash"], d["hash"],
        )


class OpLog:
    def __init__(self) -> None:
        self.ops: List[Op] = []
        self._last_nonce: Dict[str, int] = {}
        self._idem: Dict[str, int] = {}  # idem key -> seq of original op
        self._head = GENESIS

    def append(
        self,
        client: str,
        nonce: int,
        op: str,
        payload: dict,
        idem: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> Tuple[Op, bool]:
        """Append an op. Returns (op, created). `created` is False when the op was
        deduplicated by its idempotency key (a no-op replay-safe return)."""
        # Idempotency: a known key returns the original op without re-appending.
        if idem is not None and idem in self._idem:
            return self.ops[self._idem[idem]], False

        # Replay protection: nonce must strictly exceed the client's last nonce.
        last = self._last_nonce.get(client, 0)
        if nonce <= last:
            raise ReplayError(
                f"replay/stale nonce for client '{client}': {nonce} <= {last}"
            )

        seq = len(self.ops)
        ts = time.time() if ts is None else ts
        body = {
            "seq": seq, "client": client, "nonce": nonce,
            "op": op, "payload": payload, "ts": ts, "idem": idem,
        }
        h = blake(self._head.encode() + canon(body))
        rec = Op(seq, client, nonce, op, payload, ts, idem, self._head, h)

        self.ops.append(rec)
        self._last_nonce[client] = nonce
        if idem is not None:
            self._idem[idem] = seq
        self._head = h
        return rec, True

    def load(self, ops: List[Op]) -> None:
        """Rehydrate the log from persisted ops WITHOUT recomputing hashes, so the
        original chain (and thus verify() and the head commitment) is preserved
        exactly across a restart. Nonce, idempotency, and head state are restored
        from the ops themselves — replay protection survives a reload."""
        self.ops = list(ops)
        self._last_nonce = {}
        self._idem = {}
        for o in self.ops:
            if o.nonce > self._last_nonce.get(o.client, 0):
                self._last_nonce[o.client] = o.nonce
            if o.idem is not None and o.idem not in self._idem:
                self._idem[o.idem] = o.seq
        self._head = self.ops[-1].hash if self.ops else GENESIS

    def verify(self) -> bool:
        """Re-walk the chain and confirm no op has been tampered with."""
        prev = GENESIS
        for o in self.ops:
            body = {
                "seq": o.seq, "client": o.client, "nonce": o.nonce,
                "op": o.op, "payload": o.payload, "ts": o.ts, "idem": o.idem,
            }
            if o.prev_hash != prev:
                return False
            if o.hash != blake(prev.encode() + canon(body)):
                return False
            prev = o.hash
        return True

    @property
    def head(self) -> str:
        return self._head

    def slice_until(self, as_of: int) -> List[Op]:
        return [o for o in self.ops if o.seq <= as_of]

    def __len__(self) -> int:
        return len(self.ops)
