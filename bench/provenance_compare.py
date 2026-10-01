#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: MIT
"""
NEDB provenance-cost benchmark.

This benchmark deliberately answers TWO different questions in one run:

1. Side-by-side protocol A/B:
     SQLite raw      vs SQLite + NEDB shadow writes
     PostgreSQL raw  vs PostgreSQL + NEDB shadow writes
     Redis raw       vs Redis + NEDB shadow writes
     MongoDB raw     vs MongoDB + NEDB shadow writes

   The ratio is the measured cost of adding NEDB provenance to that protocol.

2. Provenance-enabled head-to-head:
     NEDB native vs SQLite+NEDB vs PostgreSQL+NEDB vs Redis+NEDB vs MongoDB+NEDB

The top-level workload is identical for every leg:
  * deterministic documents from one seed
  * insert the same rows
  * read the same ids in the same order
  * update the same ids in the same order
  * verify current answers against one Python oracle

The provenance legs MUST:
  * use NEDB's shipped wrap_* adapter
  * have shadow_writes=True
  * use a durable embedded DAG path
  * checkpoint NEDB at the same write-batch boundaries used by the workload
  * finish with a non-empty NEDB chain
  * finish with verify() == True
  * contain exactly the expected number of current shadow documents

No hand-rolled history tables, lists, collections, triggers, or audit code are
allowed in this benchmark. The point is to measure the adapter NEDB actually
ships, not a substitute implementation.

MongoDB note: the current wrap_mongo contract requires an explicit
nedb.shadow_row(...) after a host write. That call is included in the timed
provenance leg because it is the adapter's real write path today.

Run locally:
  python bench/provenance_compare.py --rows 2000 --ops 400

Environment:
  PG_DSN    postgresql://postgres:bench@127.0.0.1:5432/postgres
  REDIS_URL redis://127.0.0.1:6379/0
  MONGO_URL mongodb://127.0.0.1:27017/
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import platform
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional

SEED = 19901030
STATUSES = ("paid", "pending", "shipped", "cancelled")


class BenchFailure(RuntimeError):
    pass


def dataset(n: int) -> List[Dict[str, Any]]:
    rng = random.Random(SEED)
    return [
        {
            "id": f"r{i:08d}",
            "customer": f"c{rng.randrange(max(10, n // 10)):06d}",
            "total": rng.randrange(100, 100_000),
            "status": STATUSES[rng.randrange(len(STATUSES))],
            "version": 1,
        }
        for i in range(n)
    ]


def updated(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["total"] += 7
    out["status"] = "audited"
    out["version"] += 1
    return out


def percentile(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round((p / 100.0) * (len(ys) - 1)))))
    return ys[i]


def timed_reads(fn, ids: List[str], oracle: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    for rid in ids[:5]:
        got = fn(rid)
        if got != oracle[rid]:
            raise BenchFailure(f"warm-up read {rid}: got {got!r}, expected {oracle[rid]!r}")

    lat: List[float] = []
    start = time.perf_counter()
    for rid in ids:
        s = time.perf_counter()
        got = fn(rid)
        lat.append(time.perf_counter() - s)
        if got != oracle[rid]:
            raise BenchFailure(f"read {rid}: got {got!r}, expected {oracle[rid]!r}")
    elapsed = time.perf_counter() - start
    return {
        "ops": len(ids),
        "seconds": elapsed,
        "ops_per_s": len(ids) / elapsed if elapsed else 0.0,
        "p50_us": percentile(lat, 50) * 1e6,
        "p99_us": percentile(lat, 99) * 1e6,
    }


def timed_writes(rows: List[Dict[str, Any]], fn, commit, batch: int) -> Dict[str, float]:
    start = time.perf_counter()
    for i, row in enumerate(rows, 1):
        fn(row)
        if i % batch == 0:
            commit()
    if len(rows) % batch:
        commit()
    elapsed = time.perf_counter() - start
    return {
        "ops": len(rows),
        "seconds": elapsed,
        "ops_per_s": len(rows) / elapsed if elapsed else 0.0,
    }


class Protocol:
    name = ""
    label = ""
    transport = ""

    def __init__(self, shadow: bool):
        self.shadow = shadow
        self.shadow_dir: Optional[str] = None
        self.surface = None

    @property
    def case_name(self) -> str:
        return f"{self.name}+nedb" if self.shadow else f"{self.name}-raw"

    @property
    def case_label(self) -> str:
        return f"{self.label} + NEDB" if self.shadow else self.label

    def setup(self) -> None:
        raise NotImplementedError

    def insert(self, row: Dict[str, Any]) -> None:
        raise NotImplementedError

    def update(self, row: Dict[str, Any]) -> None:
        raise NotImplementedError

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def commit(self) -> None:
        """Finish one workload batch.

        Raw SQL engines commit their transaction here. Provenance legs also
        checkpoint the embedded NEDB DAG here, so the write-batch boundary is
        durable on the provenance side rather than merely resident in memory.
        """
        if self.shadow and self.surface is not None:
            self.surface.checkpoint()

    def version(self) -> str:
        return "?"

    def provenance_check(self, expected_rows: int) -> Optional[Dict[str, Any]]:
        if not self.shadow:
            return None
        if self.surface is None:
            raise BenchFailure(f"{self.name}: shadow=True but no NEDB surface")

        errors = int(getattr(self.surface, "shadow_errors", 0))
        if errors:
            raise BenchFailure(
                f"{self.name}: NEDB adapter swallowed {errors} shadow errors; "
                f"last={getattr(self.surface, 'last_shadow_error', None)!r}"
            )

        unmirrored = sorted(getattr(self.surface, "unmirrored_tables", set()))
        if unmirrored:
            raise BenchFailure(f"{self.name}: unmirrored tables: {unmirrored}")

        seq = int(self.surface.seq)
        if seq < 0:
            raise BenchFailure(f"{self.name}: NEDB chain is empty (seq={seq})")

        t0 = time.perf_counter()
        ok = bool(self.surface.verify())
        verify_ms = (time.perf_counter() - t0) * 1000.0
        if not ok:
            raise BenchFailure(f"{self.name}: NEDB verify() returned false")

        rows = self.surface.query("FROM bench")
        if len(rows) != expected_rows:
            raise BenchFailure(
                f"{self.name}: NEDB shadow has {len(rows)} current docs, expected {expected_rows}"
            )

        return {
            "ok": True,
            "seq": seq,
            "head": str(self.surface.head),
            "current_docs": len(rows),
            "verify_ms": verify_ms,
            "shadow_errors": errors,
        }

    def teardown(self) -> None:
        if self.shadow_dir:
            shutil.rmtree(self.shadow_dir, ignore_errors=True)


class SQLiteProtocol(Protocol):
    name = "sqlite"
    label = "SQLite"
    transport = "in-process"

    def setup(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="nedb-prov-sqlite-")
        path = os.path.join(self.dir, "bench.db")
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA synchronous=FULL")
        raw.execute(
            "CREATE TABLE bench ("
            "id TEXT PRIMARY KEY, customer TEXT, total INTEGER, status TEXT, version INTEGER)"
        )
        raw.commit()
        self.raw = raw
        if self.shadow:
            from nedb import wrap_sqlite

            self.shadow_dir = tempfile.mkdtemp(prefix="nedb-shadow-sqlite-")
            wrapped = wrap_sqlite(
                raw,
                db_name="bench_sqlite",
                backend="dag",
                dag_path=self.shadow_dir,
            )
            wrapped.nedb.auto_discover = False
            wrapped.nedb.register("bench", "bench", pk="id")
            wrapped.nedb.strict_shadow = True
            wrapped.nedb.shadow_writes = True
            self.db = wrapped
            self.surface = wrapped.nedb
        else:
            self.db = raw

    def insert(self, row):
        self.db.execute(
            "INSERT INTO bench (id, customer, total, status, version) VALUES (?,?,?,?,?)",
            (row["id"], row["customer"], row["total"], row["status"], row["version"]),
        )

    def update(self, row):
        self.db.execute(
            "UPDATE bench SET customer=?, total=?, status=?, version=? WHERE id=?",
            (row["customer"], row["total"], row["status"], row["version"], row["id"]),
        )

    def get(self, rid):
        r = self.db.execute(
            "SELECT id, customer, total, status, version FROM bench WHERE id=?", (rid,)
        ).fetchone()
        if not r:
            return None
        return dict(zip(("id", "customer", "total", "status", "version"), r))

    def count(self):
        return int(self.db.execute("SELECT COUNT(*) FROM bench").fetchone()[0])

    def commit(self):
        self.db.commit()
        super().commit()

    def version(self):
        return sqlite3.sqlite_version

    def teardown(self):
        try:
            self.raw.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)
            super().teardown()


class PostgresProtocol(Protocol):
    name = "postgres"
    label = "PostgreSQL"
    transport = "TCP loopback"

    def __init__(self, shadow: bool, dsn: str):
        super().__init__(shadow)
        self.dsn = dsn

    def setup(self):
        import psycopg2

        raw = psycopg2.connect(self.dsn)
        cur = raw.cursor()
        cur.execute("SET synchronous_commit = on")
        cur.execute("DROP TABLE IF EXISTS bench")
        cur.execute(
            "CREATE TABLE bench ("
            "id TEXT PRIMARY KEY, customer TEXT, total BIGINT, status TEXT, version INTEGER)"
        )
        raw.commit()
        self.raw = raw
        if self.shadow:
            from nedb import wrap_postgresql

            self.shadow_dir = tempfile.mkdtemp(prefix="nedb-shadow-postgres-")
            wrapped = wrap_postgresql(
                raw,
                db_name="bench_postgres",
                backend="dag",
                dag_path=self.shadow_dir,
            )
            wrapped.nedb.auto_discover = False
            wrapped.nedb.register("bench", "bench", pk="id")
            wrapped.nedb.strict_shadow = True
            wrapped.nedb.shadow_writes = True
            self.db = wrapped
            self.surface = wrapped.nedb
        else:
            self.db = raw

    def insert(self, row):
        cur = self.db.cursor()
        try:
            cur.execute(
                "INSERT INTO bench (id, customer, total, status, version) VALUES (%s,%s,%s,%s,%s)",
                (row["id"], row["customer"], row["total"], row["status"], row["version"]),
            )
        finally:
            cur.close()

    def update(self, row):
        cur = self.db.cursor()
        try:
            cur.execute(
                "UPDATE bench SET customer=%s, total=%s, status=%s, version=%s WHERE id=%s",
                (row["customer"], row["total"], row["status"], row["version"], row["id"]),
            )
        finally:
            cur.close()

    def get(self, rid):
        cur = self.db.cursor()
        try:
            cur.execute(
                "SELECT id, customer, total, status, version FROM bench WHERE id=%s", (rid,)
            )
            r = cur.fetchone()
        finally:
            cur.close()
        if not r:
            return None
        return dict(zip(("id", "customer", "total", "status", "version"), r))

    def count(self):
        cur = self.db.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM bench")
            return int(cur.fetchone()[0])
        finally:
            cur.close()

    def commit(self):
        self.db.commit()
        super().commit()

    def version(self):
        cur = self.raw.cursor()
        try:
            cur.execute("SHOW server_version")
            return str(cur.fetchone()[0])
        finally:
            cur.close()

    def teardown(self):
        try:
            try:
                cur = self.raw.cursor()
                cur.execute("DROP TABLE IF EXISTS bench")
                self.raw.commit()
                cur.close()
            finally:
                self.raw.close()
        finally:
            super().teardown()


class RedisProtocol(Protocol):
    name = "redis"
    label = "Redis"
    transport = "TCP loopback"

    def __init__(self, shadow: bool, url: str):
        super().__init__(shadow)
        self.url = url

    def setup(self):
        import redis

        raw = redis.Redis.from_url(self.url, decode_responses=True)
        raw.flushdb()
        self.raw = raw
        if self.shadow:
            from nedb import wrap_redis

            self.shadow_dir = tempfile.mkdtemp(prefix="nedb-shadow-redis-")
            wrapped = wrap_redis(
                raw,
                db_name="bench_redis",
                backend="dag",
                dag_path=self.shadow_dir,
            )
            wrapped.nedb.register("bench:*", "bench", value_parser=json.loads)
            wrapped.nedb.strict_shadow = True
            wrapped.nedb.shadow_writes = True
            self.db = wrapped
            self.surface = wrapped.nedb
        else:
            self.db = raw

    @staticmethod
    def key(rid):
        return f"bench:{rid}"

    def insert(self, row):
        self.db.set(self.key(row["id"]), json.dumps(row, separators=(",", ":")))

    def update(self, row):
        self.db.set(self.key(row["id"]), json.dumps(row, separators=(",", ":")))

    def get(self, rid):
        raw = self.db.get(self.key(rid))
        return json.loads(raw) if raw is not None else None

    def count(self):
        n = 0
        for _ in self.raw.scan_iter(match="bench:*", count=500):
            n += 1
        return n

    def version(self):
        return str(self.raw.info("server").get("redis_version", "?"))

    def teardown(self):
        try:
            self.raw.flushdb()
            self.raw.close()
        finally:
            super().teardown()


class MongoProtocol(Protocol):
    name = "mongo"
    label = "MongoDB"
    transport = "TCP loopback"

    def __init__(self, shadow: bool, url: str):
        super().__init__(shadow)
        self.url = url
        self.db_name = "bench_provenance"

    def setup(self):
        from pymongo import MongoClient

        raw = MongoClient(self.url, w=1, journal=True)
        raw.drop_database(self.db_name)
        self.raw = raw
        if self.shadow:
            from nedb import wrap_mongo

            self.shadow_dir = tempfile.mkdtemp(prefix="nedb-shadow-mongo-")
            wrapped = wrap_mongo(
                raw,
                db_name="bench_mongo",
                backend="dag",
                dag_path=self.shadow_dir,
            )
            wrapped.nedb.auto_discover = False
            wrapped.nedb.register(f"{self.db_name}.bench", "bench")
            wrapped.nedb.strict_shadow = True
            wrapped.nedb.shadow_writes = True
            self.db = wrapped[self.db_name]
            self.surface = wrapped.nedb
        else:
            self.db = raw[self.db_name]

    def _shadow(self, row):
        if self.shadow:
            # Current wrap_mongo is intentionally explicit: this IS the shipped
            # adapter write path and therefore belongs inside the timed leg.
            self.surface.shadow_row(
                f"{self.db_name}.bench",
                "bench",
                {"_id": row["id"], **{k: v for k, v in row.items() if k != "id"}},
                op="UPSERT",
            )

    def insert(self, row):
        doc = {"_id": row["id"], **{k: v for k, v in row.items() if k != "id"}}
        self.db.bench.insert_one(doc)
        self._shadow(row)

    def update(self, row):
        body = {k: v for k, v in row.items() if k != "id"}
        self.db.bench.update_one({"_id": row["id"]}, {"$set": body})
        self._shadow(row)

    def get(self, rid):
        d = self.db.bench.find_one({"_id": rid})
        if not d:
            return None
        d = dict(d)
        d["id"] = str(d.pop("_id"))
        return d

    def count(self):
        return int(self.db.bench.count_documents({}))

    def commit(self):
        # journal=True makes each acknowledged Mongo write journaled. The NEDB side
        # still checkpoints at the common workload batch boundary.
        super().commit()

    def version(self):
        return str(self.raw.server_info().get("version", "?"))

    def teardown(self):
        try:
            self.raw.drop_database(self.db_name)
            self.raw.close()
        finally:
            super().teardown()


class NativeNEDB:
    name = "nedb"
    label = "NEDB native"
    transport = "in-process"

    def setup(self):
        import nedb
        from nedb import _native

        if not nedb.__has_native__:
            raise RuntimeError("native NEDB extension is required")
        self.dir = tempfile.mkdtemp(prefix="nedb-native-bench-")
        self.db = _native.NedbCore.open(self.dir)

    def insert(self, row):
        doc = {k: v for k, v in row.items() if k != "id"}
        self.db.put("bench", row["id"], json.dumps(doc, separators=(",", ":")))

    def update(self, row):
        self.insert(row)

    def get(self, rid):
        raw = self.db.get("bench", rid)
        if not raw:
            return None
        d = json.loads(raw)
        # Native nodes expose engine metadata beside the user document.
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        d["id"] = rid
        return {k: d[k] for k in ("id", "customer", "total", "status", "version")}

    def count(self):
        return len(self.db.query("FROM bench"))

    def commit(self):
        self.db.flush()

    def version(self):
        import nedb

        return nedb.__version__

    def provenance_check(self, expected_rows):
        seq = int(self.db.seq())
        if seq < 0:
            raise BenchFailure("NEDB native chain is empty")
        t0 = time.perf_counter()
        ok = bool(self.db.verify())
        verify_ms = (time.perf_counter() - t0) * 1000.0
        if not ok:
            raise BenchFailure("NEDB native verify() returned false")
        current = self.count()
        if current != expected_rows:
            raise BenchFailure(f"NEDB native has {current} docs, expected {expected_rows}")
        return {
            "ok": True,
            "seq": seq,
            "head": str(self.db.head()),
            "current_docs": current,
            "verify_ms": verify_ms,
            "shadow_errors": 0,
        }

    def teardown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run_case(a, rows, read_ids, update_ids, batch) -> Dict[str, Any]:
    oracle = {r["id"]: dict(r) for r in rows}
    a.setup()
    try:
        out: Dict[str, Any] = {
            "label": a.case_label if isinstance(a, Protocol) else a.label,
            "version": a.version(),
            "transport": a.transport,
            "phases": {},
        }

        out["phases"]["load"] = timed_writes(rows, a.insert, a.commit, batch)
        if a.count() != len(rows):
            raise BenchFailure(f"{out['label']}: host count after load is {a.count()}, expected {len(rows)}")

        out["phases"]["point_read"] = timed_reads(a.get, read_ids, oracle)

        changed = [updated(oracle[rid]) for rid in update_ids]
        out["phases"]["update"] = timed_writes(changed, a.update, a.commit, batch)
        for row in changed:
            oracle[row["id"]] = row

        # Correctness is outside the timer. Every updated key must now match.
        for rid in update_ids:
            got = a.get(rid)
            if got != oracle[rid]:
                raise BenchFailure(
                    f"{out['label']}: post-update {rid}: got {got!r}, expected {oracle[rid]!r}"
                )

        out["phases"]["post_update_read"] = timed_reads(a.get, read_ids, oracle)
        out["provenance"] = a.provenance_check(len(rows))
        out["status"] = "ok"
        return out
    finally:
        a.teardown()


def ratio(shadow: Dict[str, Any], raw: Dict[str, Any], phase: str) -> float:
    a = float(shadow["phases"][phase]["seconds"])
    b = float(raw["phases"][phase]["seconds"])
    return a / b if b else 0.0


def fmt_rate(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M/s"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K/s"
    return f"{v:.0f}/s"


def fmt_cost(v: float) -> str:
    return f"{v:.2f}×"


def render(out: Dict[str, Any]) -> str:
    meta = out["meta"]
    lines = [
        "# NEDB provenance-cost benchmark",
        "",
        f"> Run {meta['date']} · commit \`{meta['commit']}\` · {meta['runner']}",
        "",
        "## Contract",
        "",
        "This run separates **protocol speed** from **the cost of provenance**.",
        "Each host protocol is run twice on a fresh store: raw, then through NEDB's",
        "shipped \`wrap_*\` adapter with \`shadow_writes=True\`. The same deterministic",
        "rows, read ids, update ids, ordering, batch boundaries, and correctness oracle",
        "are used for both legs. Provenance legs use a durable embedded DAG path and",
        "checkpoint NEDB at each workload write-batch boundary.",
        "",
        "**No hand-rolled history implementation is used.** The provenance result is",
        "valid only if the NEDB chain is non-empty, contains the expected current",
        "document count, reports zero observable shadow errors, and \`verify()\` passes.",
        "",
        "MongoDB's current wrapper requires explicit \`nedb.shadow_row(...)\` after the",
        "host write; that shipped adapter call is included inside its timed provenance leg.",
        "",
        f"Dataset: **{meta['rows']:,} rows** · reads: **{meta['read_ops']:,}** · "
        f"updates: **{meta['update_ops']:,}** · write batch: **{meta['batch']:,}**",
        "",
        "## Side-by-side: cost of adding NEDB provenance",
        "",
        "| Protocol | Raw load | + NEDB load | Load cost | Raw reads | + NEDB reads | Read cost | Raw updates | + NEDB updates | Update cost | Write cost¹ | Provenance |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    for name in ("sqlite", "postgres", "redis", "mongo"):
        raw = out["results"][f"{name}-raw"]
        sh = out["results"][f"{name}+nedb"]
        if raw.get("status") != "ok" or sh.get("status") != "ok":
            lines.append(
                f"| {name} | — | — | — | — | — | — | — | — | — | — | ERROR |"
            )
            continue
        lc = ratio(sh, raw, "load")
        rc = ratio(sh, raw, "point_read")
        uc = ratio(sh, raw, "update")
        write_cost = math.sqrt(max(lc, 1e-12) * max(uc, 1e-12))
        pv = sh["provenance"]
        lines.append(
            f"| {raw['label']} | {fmt_rate(raw['phases']['load']['ops_per_s'])} | "
            f"{fmt_rate(sh['phases']['load']['ops_per_s'])} | {fmt_cost(lc)} | "
            f"{fmt_rate(raw['phases']['point_read']['ops_per_s'])} | "
            f"{fmt_rate(sh['phases']['point_read']['ops_per_s'])} | {fmt_cost(rc)} | "
            f"{fmt_rate(raw['phases']['update']['ops_per_s'])} | "
            f"{fmt_rate(sh['phases']['update']['ops_per_s'])} | {fmt_cost(uc)} | "
            f"**{fmt_cost(write_cost)}** | verify ✓ · seq {pv['seq']} |"
        )

    lines += [
        "",
        "¹ **Write cost** is the geometric mean of load-cost and update-cost. "
        "A value of 1.00× means the provenance-enabled path took the same wall time as raw.",
        "",
        "## Head-to-head: provenance enabled",
        "",
        "| Workload | NEDB native | SQLite + NEDB | PostgreSQL + NEDB | Redis + NEDB | MongoDB + NEDB |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    keys = ("nedb", "sqlite+nedb", "postgres+nedb", "redis+nedb", "mongo+nedb")
    phase_names = [
        ("load", "Load / initial provenance"),
        ("point_read", "Point read"),
        ("update", "Update + provenance"),
        ("post_update_read", "Point read after updates"),
    ]
    for phase, label in phase_names:
        cells = []
        for key in keys:
            r = out["results"].get(key, {})
            if r.get("status") == "ok":
                cells.append(fmt_rate(r["phases"][phase]["ops_per_s"]))
            else:
                cells.append("—")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Provenance proof",
        "",
        "| Leg | Current docs | Sequence | verify() | verify time |",
        "|---|---:|---:|:---:|---:|",
    ]
    for key in keys:
        r = out["results"].get(key, {})
        p = r.get("provenance") if r.get("status") == "ok" else None
        if p:
            lines.append(
                f"| {r['label']} | {p['current_docs']:,} | {p['seq']:,} | "
                f"{'✓' if p['ok'] else '✗'} | {p['verify_ms']:.1f} ms |"
            )

    lines += [
        "",
        "## Interpretation",
        "",
        "- The **A/B table** is the cost-of-provenance result. Compare each protocol only",
        "  with itself: same host, same client, same runner, same workload; the material",
        "  change is NEDB shadowing + checkpointing.",
        "- The **head-to-head table** compares the complete provenance-enabled paths.",
        "  It intentionally includes each protocol's transport and adapter mechanics.",
        "- GitHub-hosted runners are shared VMs. Compare numbers **within one run**; do",
        "  not treat small differences across separate runs as meaningful.",
        "- Read-cost can be near 1.00× because reads pass through the wrappers without",
        "  creating provenance. Write-cost is where the NEDB chain is deliberately paid.",
        "",
    ]
    return "\n".join(lines)


def build_cases(require_all: bool):
    pg = os.environ.get("PG_DSN", "postgresql://postgres:bench@127.0.0.1:5432/postgres")
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    mongo = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017/")

    factories = {
        "nedb": lambda: NativeNEDB(),
        "sqlite-raw": lambda: SQLiteProtocol(False),
        "sqlite+nedb": lambda: SQLiteProtocol(True),
        "postgres-raw": lambda: PostgresProtocol(False, pg),
        "postgres+nedb": lambda: PostgresProtocol(True, pg),
        "redis-raw": lambda: RedisProtocol(False, redis_url),
        "redis+nedb": lambda: RedisProtocol(True, redis_url),
        "mongo-raw": lambda: MongoProtocol(False, mongo),
        "mongo+nedb": lambda: MongoProtocol(True, mongo),
    }
    made = {}
    errors = {}
    for key, factory in factories.items():
        try:
            made[key] = factory()
        except Exception as e:
            errors[key] = f"{type(e).__name__}: {e}"
            print(f"[provenance-bench] unavailable {key}: {errors[key]}", file=sys.stderr)
    if require_all and errors:
        raise RuntimeError("required benchmark cases unavailable: " + json.dumps(errors, sort_keys=True))
    return made, errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=2000)
    ap.add_argument("--ops", type=int, default=400)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--json")
    ap.add_argument("--markdown")
    ap.add_argument("--require-all", action="store_true")
    args = ap.parse_args(argv)

    if args.rows < 10:
        ap.error("--rows must be >= 10")
    if args.ops < 1:
        ap.error("--ops must be >= 1")
    if args.batch < 1:
        ap.error("--batch must be >= 1")

    rows = dataset(args.rows)
    by_id = {r["id"]: r for r in rows}
    rng = random.Random(SEED + 1)
    ids = list(by_id)
    read_ids = [rng.choice(ids) for _ in range(args.ops)]
    update_ids = rng.sample(ids, min(args.ops, len(ids)))

    cases, unavailable = build_cases(args.require_all)
    order = (
        "nedb",
        "sqlite-raw", "sqlite+nedb",
        "postgres-raw", "postgres+nedb",
        "redis-raw", "redis+nedb",
        "mongo-raw", "mongo+nedb",
    )

    out: Dict[str, Any] = {
        "meta": {
            "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "commit": os.environ.get("GITHUB_SHA", "local")[:12],
            "runner": (
                f"{platform.system()} {platform.machine()}, {os.cpu_count()} vCPU, "
                f"Python {platform.python_version()}"
            ),
            "rows": len(rows),
            "read_ops": len(read_ids),
            "update_ops": len(update_ids),
            "batch": args.batch,
            "seed": SEED,
        },
        "unavailable": unavailable,
        "results": {},
    }

    failed = False
    for key in order:
        if key not in cases:
            continue
        print(f"[provenance-bench] {key}: running", flush=True)
        try:
            r = run_case(cases[key], rows, read_ids, update_ids, args.batch)
            print(
                f"[provenance-bench] {key}: "
                f"load={r['phases']['load']['ops_per_s']:.0f}/s "
                f"read={r['phases']['point_read']['ops_per_s']:.0f}/s "
                f"update={r['phases']['update']['ops_per_s']:.0f}/s",
                flush=True,
            )
        except Exception as e:
            failed = True
            r = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
            print(f"[provenance-bench] {key}: ERROR {r['reason']}", file=sys.stderr, flush=True)
        out["results"][key] = r

    if args.require_all:
        missing = [k for k in order if out["results"].get(k, {}).get("status") != "ok"]
        if missing:
            failed = True
            print(f"[provenance-bench] required cases failed: {missing}", file=sys.stderr)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(render(out))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
