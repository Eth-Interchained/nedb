"""
nedb.wrap_core — the shared wrap_* surface.

One engine-agnostic implementation of the NEDB layer-2 contract:

    register(pattern, collection, ...)   → teach NEDB about the host DB's shape
    backfill()                           → one-time import of existing data
    shadow_writes = True                 → auto-chain future host-DB writes
    .nedb.<full NEDB API>                → put/get/query/TRACE/AS OF/verify/…

Three backends, one surface:
    v1 in-process AOF engine   (NEDB() + a host-specific Backend)
    embedded v2/v3 DAG         (backends.dag.DagBackend — Rust native core)
    HTTP nedbd                 (NedBdProxy — v1 AOF, v2 DAG, or v3 --dag-v3)

Host adapters (wrap_redis / wrap_sqlite / wrap_mysql) supply:
    _host_scan(mapping)              → iterate existing host records
    _host_doc(mapping, key, value)   → host value → NEDB doc
    _shadow_put(mapping, doc_id, d)  → write a shadowed doc

© INTERCHAINED LLC × Claude Sonnet 4.6
"""
from __future__ import annotations

import fnmatch
import json
from typing import Any, Callable, Dict, List, Optional

from .engine import NEDB as _NEDB
from .wrap_redis import CollectionMapping, NedBdProxy  # reuse, don't duplicate
from .backends.dag import DagBackend, has_dag_native


# ── Engine selection ─────────────────────────────────────────────────────────

class _MemoryPersist:
    """No-op persistence for engines that persist themselves (DAG, nedbd)."""

    def append(self, _line: str) -> None:
        pass

    def publish_ops(self, _lines) -> None:
        pass

    def read_all(self):
        return []


def open_engine(
    backend: str = "auto",            # "auto" | "aof" | "dag" | "nedbd"
    db_name: str = "default",
    nedbd_url: Optional[str] = None,
    nedbd_token: Optional[str] = None,
    dag_path: Optional[str] = None,
    dag_tmk: Optional[str] = None,
):
    """
    Resolve the NEDB engine handle for a wrap_* surface.

    backend="auto": nedbd_url wins if given; else embedded DAG if the native
    core is importable; else v1 in-process AOF engine.
    """
    if backend == "nedbd" or (backend == "auto" and nedbd_url):
        return NedBdProxy(nedbd_url, db_name, token=nedbd_token), None

    if backend == "dag" or (backend == "auto" and has_dag_native()):
        core = DagBackend(path=dag_path, tmk=dag_tmk)
        return core, None

    if backend in ("aof", "auto"):
        return _NEDB(), None

    raise ValueError(f"unknown backend {backend!r} "
                     "(expected 'auto' | 'aof' | 'dag' | 'nedbd')")


# ── The shared surface ───────────────────────────────────────────────────────

class WrapSurface:
    """
    The `.nedb` attribute of any wrapped host connection.

    Same contract as NEDBSurface in wrap_redis, generalized:
    register → backfill → shadow_writes=True → full NEDB API.
    """

    def __init__(self, db_name: str, engine=None, persist=None):
        self._db_name = db_name
        self._mappings: List[CollectionMapping] = []
        self.shadow_writes: bool = False
        self._backfilled: bool = False
        self._db = engine
        self._persist = persist or _MemoryPersist()

    # ── host hooks (override in the host adapter) ────────────────────────────

    def _host_scan(self, mapping: CollectionMapping, batch_size: int):
        """Yield (key, raw_value) pairs from the host DB. Override me."""
        return iter(())

    def _shadow_doc(self, mapping: CollectionMapping, key: str,
                    args: tuple, kwargs: dict) -> Optional[Dict[str, Any]]:
        """Host write command → NEDB doc, or None to skip. Override me."""
        return None

    # ── registration / backfill (shared) ─────────────────────────────────────

    def register(self, pattern: str, collection: str,
                 id_extractor: Optional[Callable[[str], str]] = None,
                 value_parser: Optional[Callable[[Any], dict]] = None,
                 value_type: str = "string") -> "WrapSurface":
        self._mappings.append(CollectionMapping(
            pattern, collection, id_extractor, value_parser, value_type))
        return self

    def _mapping_for(self, key: str) -> Optional[CollectionMapping]:
        for m in self._mappings:
            if m.matches(key):
                return m
        return None

    def backfill(self, pattern: Optional[str] = None,
                 collection: Optional[str] = None,
                 id_extractor: Optional[Callable[[str], str]] = None,
                 value_parser: Optional[Callable[[Any], dict]] = None,
                 value_type: str = "string",
                 batch_size: int = 200) -> int:
        if pattern is not None:
            mappings = [CollectionMapping(pattern, collection or pattern.split(":")[0],
                                          id_extractor, value_parser, value_type)]
        else:
            mappings = list(self._mappings)
        total = 0
        for m in mappings:
            for key, raw in self._host_scan(m, batch_size):
                doc_id = m.extract_id(key)
                doc = m.parse_value(raw)
                doc.setdefault("_source", "backfill")
                try:
                    self._db.put(m.collection, doc_id, doc, client="__backfill__")
                    total += 1
                except Exception:
                    continue
        self._backfilled = True
        return total

    # ── shadowing (shared) ───────────────────────────────────────────────────

    def shadow(self, cmd: str, key: str, *args, **kwargs) -> None:
        """Route a host write command into NEDB. Failures never propagate."""
        if not self.shadow_writes:
            return
        try:
            m = self._mapping_for(key)
            if m is None:
                self._db.put("__shadow_raw__", key,
                             {"cmd": cmd, "key": key, "_source": "shadow_raw"},
                             client="__shadow__")
                self._persist_after()
                return
            doc = self._shadow_doc(m, key, args, kwargs)
            if doc is None:
                return
            doc.setdefault("_source", "shadow")
            self._db.put(m.collection, m.extract_id(key), doc,
                         client="__shadow__")
            self._persist_after()
        except Exception:
            pass

    def _persist_after(self) -> None:
        # DAG + nedbd persist themselves; v1 AOF needs the last op appended
        eng = self._db
        if isinstance(eng, _NEDB) and getattr(eng, "log", None) and eng.log.ops:
            last = eng.log.ops[-1]
            self._persist.append(json.dumps(last.to_dict()))

    # ── full NEDB API (shared across every backend) ──────────────────────────

    def put(self, coll, id, doc, **kw):
        r = self._db.put(coll, id, doc, **kw)
        self._persist_after()
        return r

    def get(self, coll, id, as_of=None):
        return self._db.get(coll, id, as_of=as_of)

    def query(self, nql):
        return self._db.query(nql)

    def create_index(self, coll, field, kind="eq"):
        self._db.create_index(coll, field, kind)

    def delete(self, coll, id, **kw):
        self._db.delete(coll, id)
        self._persist_after()

    def link(self, frm, rel, to, **kw):
        self._db.link(frm, rel, to)
        self._persist_after()

    def unlink(self, frm, rel, to, **kw):
        self._db.unlink(frm, rel, to)
        self._persist_after()

    def neighbors(self, frm, rel, as_of=None):
        return self._db.neighbors(frm, rel, as_of=as_of)

    def inbound(self, to, rel, as_of=None):
        return self._db.inbound(to, rel, as_of=as_of)

    def verify(self) -> bool:
        return self._db.verify()

    @property
    def head(self) -> str:
        return self._db.head

    @property
    def seq(self) -> int:
        return self._db.seq

    def checkpoint(self) -> str:
        return self._db.checkpoint()

    # ── DAG-native passthroughs (no-op/None on other backends) ──────────────

    def tip(self):
        if hasattr(self._db, "tip"):
            return self._db.tip()
        return None

    def tip_collection(self, coll):
        if hasattr(self._db, "tip_collection"):
            return self._db.tip_collection(coll)
        return None

    def since(self, after_seq: int, limit: int = 0):
        if hasattr(self._db, "since"):
            return self._db.since(after_seq, limit)
        raise RuntimeError("changefeed requires the DAG backend "
                           "(embedded native or nedbd --dag)")

    def scan_status(self):
        if hasattr(self._db, "scan_status"):
            return self._db.scan_status()
        raise RuntimeError("scan_status requires the DAG backend")

    # ── introspection ────────────────────────────────────────────────────────

    @property
    def engine_kind(self) -> str:
        if isinstance(self._db, NedBdProxy):
            return "nedbd-http"
        if isinstance(self._db, DagBackend):
            return "dag-embedded"
        return "aof-embedded"

    def __repr__(self):
        return (f"<WrapSurface db={self._db_name!r} "
                f"engine={self.engine_kind} "
                f"mappings={len(self._mappings)}>")
