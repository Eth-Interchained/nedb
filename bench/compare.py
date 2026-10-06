#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: MIT
"""
NEDB vs SQLite · PostgreSQL · Redis · MongoDB — one workload, every engine.

The contract (docs/BENCHMARKS.md prints it next to every result):

* ONE dataset. customers / products / orders / order_items, generated once
  from a fixed seed and handed byte-identical to every engine.
* ONE set of questions, asked the same number of times in the same order.
  Each engine answers in its own idiomatic best form — SQL JOIN on SQLite and
  PostgreSQL, $lookup pipelines on MongoDB, neSQL on NEDB, and an explicit
  application-side join on Redis (which has no join; that is labelled, not
  hidden).
* ONE audit requirement. "Every past version of an order is kept, and you can
  ask what the data looked like before the refund wave." NEDB does this
  natively. Every other engine gets it the way it is actually built in
  production — a history table written in the same transaction, a history
  collection inside a multi-document transaction, a Redis list inside
  MULTI/EXEC — and pays for it in the write path AND in the query.
* ONE oracle. Every answer is checked against the value computed from the
  seed data in plain Python. An engine that answers wrong fails the run: a
  fast wrong answer is not a result.
* MATCHED durability profiles, not shipped defaults:
    durable  — every commit is on disk before the ack
    relaxed  — each engine's bounded (~1 s) loss-window mode
  A leg that cannot honour a profile is reported N/A with the reason.
* ONE client language (Python), one machine, engines run one after another.

Run locally (all servers optional — a missing server is SKIPPED loudly):

    python3 bench/compare.py --orders 5000 --json out.json --markdown docs/BENCHMARKS.md

Environment:
    PG_DSN      postgresql://postgres:bench@127.0.0.1:5432/postgres
    REDIS_URL   redis://127.0.0.1:6379/0
    MONGO_URL   mongodb://127.0.0.1:27017/?replicaSet=rs0&directConnection=true
    NEDBD_BIN   path to the nedbd-v2 binary (default: shipped inside the wheel)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import random
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

SEED = 19901030
CITIES = ["winter park", "orlando", "maitland", "oviedo", "apopka", "sanford",
          "baldwin park", "college park", "altamonte", "winter garden"]
STATUSES = ["paid", "pending", "shipped", "cancelled", "failed"]
PROFILES = ("durable", "relaxed")

ENGINES = ("nedb", "nedbd", "sqlite", "postgres", "redis", "mongo")
ENGINE_LABEL = {
    "nedb": "NEDB (embedded)",
    "nedbd": "NEDB (nedbd HTTP)",
    "sqlite": "SQLite",
    "postgres": "PostgreSQL",
    "redis": "Redis",
    "mongo": "MongoDB",
}
TRANSPORT = {
    "nedb": "in-process",
    "nedbd": "HTTP/1.1 keep-alive, loopback",
    "sqlite": "in-process",
    "postgres": "TCP, loopback",
    "redis": "TCP, loopback",
    "mongo": "TCP, loopback",
}


class BenchFailure(RuntimeError):
    """An engine gave an answer that disagrees with the oracle."""


# ─────────────────────────────────────────────────────────────── dataset ────

class Dataset:
    """The single source of truth. Every engine loads exactly these rows."""

    def __init__(self, n_orders: int, items_per_order: int = 3, seed: int = SEED):
        rng = random.Random(seed)
        self.n_customers = max(10, n_orders // 10)
        self.n_products = 200
        self.customers = [
            {"id": f"c{i:07d}", "name": f"customer {i}", "city": CITIES[rng.randrange(len(CITIES))]}
            for i in range(self.n_customers)
        ]
        self.products = [
            {"id": f"p{i:05d}", "title": f"product {i}", "price": rng.randrange(100, 20000)}
            for i in range(self.n_products)
        ]
        self.orders: List[Dict[str, Any]] = []
        self.items: List[Dict[str, Any]] = []
        base = _dt.datetime(2026, 1, 1)
        for i in range(n_orders):
            oid = f"o{i:08d}"
            total = 0
            for j in range(items_per_order):
                p = self.products[rng.randrange(self.n_products)]
                qty = rng.randrange(1, 5)
                total += qty * p["price"]
                self.items.append({"id": f"{oid}-{j}", "order_id": oid,
                                   "product_id": p["id"], "qty": qty, "price": p["price"]})
            self.orders.append({
                "id": oid,
                "customer_id": self.customers[rng.randrange(self.n_customers)]["id"],
                "total": total,                      # integer cents — no float drift
                "status": STATUSES[rng.randrange(len(STATUSES))],
                "created_at": (base + _dt.timedelta(minutes=i)).isoformat(),
            })
        self.order_by_id = {o["id"]: o for o in self.orders}
        self.city_of = {c["id"]: c["city"] for c in self.customers}
        self.title_of = {p["id"]: p["title"] for p in self.products}
        self.items_of: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for it in self.items:
            self.items_of[it["order_id"]].append(it)
        self.orders_of: Dict[str, List[str]] = defaultdict(list)
        for o in self.orders:
            self.orders_of[o["customer_id"]].append(o["id"])

    def revenue_by_city(self, orders: List[Dict[str, Any]]) -> Dict[str, int]:
        out: Dict[str, int] = defaultdict(int)
        for o in orders:
            if o["status"] == "paid":
                out[self.city_of[o["customer_id"]]] += o["total"]
        return dict(out)


class Plan:
    """Which keys each phase touches. Drawn once, shared by every engine."""

    def __init__(self, ds: Dataset, ops: int, seed: int = SEED + 1):
        rng = random.Random(seed)
        ids = [o["id"] for o in ds.orders]
        self.point = [rng.choice(ids) for _ in range(ops)]
        custs = [c["id"] for c in ds.customers]
        self.customer = [rng.choice(custs) for _ in range(max(1, ops // 2))]
        self.detail = [rng.choice(ids) for _ in range(max(1, ops // 4))]
        paid = [o["id"] for o in ds.orders if o["status"] == "paid"]
        n_upd = min(len(paid), max(1, ops // 2))
        self.refund = rng.sample(paid, n_upd)                    # the refund wave
        self.as_of = [rng.choice(self.refund) for _ in range(max(1, ops // 4))]
        self.analytic_repeats = 3

        self.expected_before = ds.revenue_by_city(ds.orders)
        after = [dict(o, status="refunded") if o["id"] in set(self.refund) else o for o in ds.orders]
        self.expected_after = ds.revenue_by_city(after)


# ─────────────────────────────────────────────────────────────── timing ─────

def _pct(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def timed_ops(fn: Callable[[Any], Any], keys: List[Any], check: Callable[[Any, Any], None]) -> Dict[str, float]:
    """Run fn(key) per key; per-op latency; check(key, answer) against the oracle."""
    # warm-up: the first 5 keys, untimed, answers still checked
    for k in keys[:5]:
        check(k, fn(k))
    lat: List[float] = []
    t0 = time.perf_counter()
    for k in keys:
        s = time.perf_counter()
        ans = fn(k)
        lat.append(time.perf_counter() - s)
        check(k, ans)
    total = time.perf_counter() - t0
    return {
        "ops": len(keys),
        "seconds": total,
        "ops_per_s": len(keys) / total if total else 0.0,
        "p50_us": _pct(lat, 50) * 1e6,
        "p99_us": _pct(lat, 99) * 1e6,
    }


# ─────────────────────────────────────────────────────────────── adapters ───
#
# Every adapter implements the same nine operations. Nothing else is timed.
#
#   setup(profile)            fresh empty store, indexes created BEFORE load
#   load(ds, batch)           all four tables, one transaction per batch
#   point_read(oid)           -> status
#   customer_orders(cid)      -> sorted [(order_id, total, status)]
#   order_detail(oid)         -> sorted [(product_title, qty)]   (3-table join)
#   revenue_by_city()         -> {city: sum(total) of paid orders} (join + group)
#   refund_with_history(oid)  status -> 'refunded', keeping the prior version
#   mark()                    opaque point-in-time marker taken before the refunds
#   status_as_of(oid, m)      -> status at marker m
#   revenue_by_city_as_of(m)  -> revenue_by_city as it was at marker m
#   teardown()

class Adapter:
    name = ""
    durability: Dict[str, str] = {}
    history_model = ""
    join_model = ""
    integrity = "none"

    def supports(self, profile: str) -> Optional[str]:
        """None if the profile can be honoured, else the reason it cannot."""
        return None

    def version(self) -> str:
        return "?"

    def verify(self) -> Optional[Dict[str, Any]]:
        return None

    def footprint(self) -> Optional[int]:
        return None


# ── SQL (shared by SQLite and PostgreSQL) ──────────────────────────────────

SQL_DDL = [
    "CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT, city TEXT)",
    "CREATE TABLE products (id TEXT PRIMARY KEY, title TEXT, price BIGINT)",
    "CREATE TABLE orders (id TEXT PRIMARY KEY, customer_id TEXT, total BIGINT, status TEXT, created_at TEXT)",
    "CREATE TABLE order_items (id TEXT PRIMARY KEY, order_id TEXT, product_id TEXT, qty BIGINT, price BIGINT)",
    # the audit requirement: one row per superseded version of an order
    "CREATE TABLE order_history (order_id TEXT, customer_id TEXT, total BIGINT, status TEXT, created_at TEXT, valid_to BIGINT)",
    "CREATE TABLE clock (v BIGINT)",
    "INSERT INTO clock VALUES (0)",
    "CREATE INDEX orders_status ON orders (status)",
    "CREATE INDEX orders_customer ON orders (customer_id)",
    "CREATE INDEX items_order ON order_items (order_id)",
    "CREATE INDEX customers_city ON customers (city)",
    "CREATE INDEX history_order ON order_history (order_id, valid_to)",
]
SQL_DROP = ["DROP TABLE IF EXISTS " + t for t in
            ("order_history", "order_items", "orders", "products", "customers", "clock")]


class SqlAdapter(Adapter):
    join_model = "SQL JOIN"
    history_model = "history table, same transaction"
    P = "?"

    def _cur(self):
        return self.conn.cursor()

    def _q(self, sql: str) -> str:
        return sql.replace("?", self.P)

    def _schema(self):
        cur = self._cur()
        for s in SQL_DROP + SQL_DDL:
            cur.execute(s)
        self.conn.commit()

    def load(self, ds: Dataset, batch: int):
        cur = self._cur()
        ins = {
            "customers": (self._q("INSERT INTO customers VALUES (?,?,?)"),
                          lambda r: (r["id"], r["name"], r["city"])),
            "products": (self._q("INSERT INTO products VALUES (?,?,?)"),
                         lambda r: (r["id"], r["title"], r["price"])),
            "orders": (self._q("INSERT INTO orders VALUES (?,?,?,?,?)"),
                       lambda r: (r["id"], r["customer_id"], r["total"], r["status"], r["created_at"])),
            "order_items": (self._q("INSERT INTO order_items VALUES (?,?,?,?,?)"),
                            lambda r: (r["id"], r["order_id"], r["product_id"], r["qty"], r["price"])),
        }
        for table, rows in (("customers", ds.customers), ("products", ds.products),
                            ("orders", ds.orders), ("order_items", ds.items)):
            sql, f = ins[table]
            for i in range(0, len(rows), batch):
                self._executemany(cur, sql, [f(r) for r in rows[i:i + batch]])
                self.conn.commit()

    def _executemany(self, cur, sql, rows):
        cur.executemany(sql, rows)

    def point_read(self, oid):
        cur = self._cur()
        cur.execute(self._q("SELECT status FROM orders WHERE id = ?"), (oid,))
        r = cur.fetchone()
        return r[0] if r else None

    def customer_orders(self, cid):
        cur = self._cur()
        cur.execute(self._q("SELECT id, total, status FROM orders WHERE customer_id = ?"), (cid,))
        return sorted(tuple(r) for r in cur.fetchall())

    def order_detail(self, oid):
        cur = self._cur()
        cur.execute(self._q(
            "SELECT p.title, i.qty FROM orders o "
            "JOIN order_items i ON i.order_id = o.id "
            "JOIN products p ON p.id = i.product_id WHERE o.id = ?"), (oid,))
        return sorted(tuple(r) for r in cur.fetchall())

    def revenue_by_city(self):
        cur = self._cur()
        cur.execute("SELECT c.city, SUM(o.total) FROM orders o JOIN customers c ON c.id = o.customer_id "
                    "WHERE o.status = 'paid' GROUP BY c.city")
        return {r[0]: int(r[1]) for r in cur.fetchall()}

    def mark(self):
        cur = self._cur()
        cur.execute("SELECT v FROM clock")
        return int(cur.fetchone()[0])

    def refund_with_history(self, oid):
        cur = self._cur()
        cur.execute("UPDATE clock SET v = v + 1")
        cur.execute("SELECT v FROM clock")
        v = cur.fetchone()[0]
        cur.execute(self._q(
            "INSERT INTO order_history SELECT id, customer_id, total, status, created_at, ? "
            "FROM orders WHERE id = ?"), (v, oid))
        cur.execute(self._q("UPDATE orders SET status = 'refunded' WHERE id = ?"), (oid,))
        self.conn.commit()

    def status_as_of(self, oid, m):
        cur = self._cur()
        cur.execute(self._q(
            "SELECT COALESCE("
            " (SELECT h.status FROM order_history h WHERE h.order_id = ? AND h.valid_to > ? "
            "  ORDER BY h.valid_to LIMIT 1),"
            " (SELECT o.status FROM orders o WHERE o.id = ?))"), (oid, m, oid))
        return cur.fetchone()[0]

    def revenue_by_city_as_of(self, m):
        # State of every order at marker m: the earliest version superseded
        # AFTER m, else the current row. (The workload inserts no orders
        # after m, so no created-after filter is needed — stated in the docs.)
        cur = self._cur()
        cur.execute(self._q(
            "WITH h AS ("
            "  SELECT order_id, status, total, "
            "         ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY valid_to) AS rn "
            "  FROM order_history WHERE valid_to > ?), "
            "s AS ("
            "  SELECT o.customer_id, COALESCE(h.status, o.status) AS status, "
            "         COALESCE(h.total, o.total) AS total "
            "  FROM orders o LEFT JOIN h ON h.order_id = o.id AND h.rn = 1) "
            "SELECT c.city, SUM(s.total) FROM s JOIN customers c ON c.id = s.customer_id "
            "WHERE s.status = 'paid' GROUP BY c.city"), (m,))
        return {r[0]: int(r[1]) for r in cur.fetchall()}


class SqliteAdapter(SqlAdapter):
    name = "sqlite"
    durability = {"durable": "journal_mode=WAL, synchronous=FULL",
                  "relaxed": "journal_mode=WAL, synchronous=NORMAL"}

    def setup(self, profile):
        self.dir = tempfile.mkdtemp(prefix="bench-sqlite-")
        self.path = os.path.join(self.dir, "bench.db")
        self.conn = sqlite3.connect(self.path, isolation_level="DEFERRED")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=" + ("FULL" if profile == "durable" else "NORMAL"))
        self._schema()

    def version(self):
        return sqlite3.sqlite_version

    def footprint(self):
        return sum(os.path.getsize(os.path.join(self.dir, f)) for f in os.listdir(self.dir))

    def teardown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class PostgresAdapter(SqlAdapter):
    name = "postgres"
    P = "%s"
    durability = {"durable": "synchronous_commit=on, fsync=on",
                  "relaxed": "synchronous_commit=off (WAL writer flushes ≤ 3×wal_writer_delay)"}

    def __init__(self, dsn):
        import psycopg2  # noqa: F401  (import error → SKIPPED)
        self.dsn = dsn

    def setup(self, profile):
        import psycopg2
        import psycopg2.extras
        self.extras = psycopg2.extras
        self.conn = psycopg2.connect(self.dsn)
        cur = self.conn.cursor()
        cur.execute("SET synchronous_commit = " + ("on" if profile == "durable" else "off"))
        self.conn.commit()
        self._schema()

    def _executemany(self, cur, sql, rows):
        # execute_values is what a real psycopg2 loader uses; plain
        # executemany is a Python loop of round trips and would handicap PG.
        n = sql.count("%s")
        head = sql[: sql.index("VALUES")] + "VALUES %s"
        self.extras.execute_values(cur, head, rows, template="(" + ",".join(["%s"] * n) + ")",
                                   page_size=len(rows))

    def version(self):
        cur = self.conn.cursor()
        cur.execute("SHOW server_version")
        return cur.fetchone()[0]

    def footprint(self):
        cur = self.conn.cursor()
        cur.execute("SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c WHERE c.relname IN "
                    "('customers','products','orders','order_items','order_history')")
        return int(cur.fetchone()[0] or 0)

    def teardown(self):
        cur = self.conn.cursor()
        for s in SQL_DROP:
            cur.execute(s)
        self.conn.commit()
        self.conn.close()


# ── NEDB embedded ──────────────────────────────────────────────────────────

NEDB_INDEXES = [("orders", "status"), ("orders", "customer_id"),
                ("order_items", "order_id"), ("customers", "city")]


def _nedb_strip(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}


class NedbAdapter(Adapter):
    name = "nedb"
    join_model = "neSQL JOIN (native)"
    history_model = "native — every version is kept (AS OF SYSTEM TIME)"
    integrity = "native verify() over the content-addressed DAG"
    durability = {"durable": "flush() after every commit (WAL + MANIFEST fsync)",
                  "relaxed": "manifest ticker, 1000 ms (NEDB_FLUSH_MS default)"}

    def __init__(self):
        import nedb
        from nedb import _native
        self.nedb = nedb
        self.core = _native.NedbCore

    def setup(self, profile):
        self.profile = profile
        self.dir = tempfile.mkdtemp(prefix="bench-nedb-")
        self.db = self.core.open(self.dir)
        for coll, field in NEDB_INDEXES:
            self.db.create_index(coll, field, "eq")

    def _commit(self):
        if self.profile == "durable":
            self.db.flush()

    def _put(self, coll, row):
        doc = {k: v for k, v in row.items() if k != "id"}
        self.db.put(coll, row["id"], json.dumps(doc))

    def load(self, ds, batch):
        for coll, rows in (("customers", ds.customers), ("products", ds.products),
                           ("orders", ds.orders), ("order_items", ds.items)):
            for i in range(0, len(rows), batch):
                for r in rows[i:i + batch]:
                    self._put(coll, r)
                self._commit()

    def _rows(self, sql):
        return [json.loads(r) for r in self.db.query(sql)]

    def point_read(self, oid):
        r = self.db.get("orders", oid)
        return json.loads(r)["status"] if r else None

    def customer_orders(self, cid):
        return sorted((r["_id"], r["total"], r["status"]) for r in
                      self._rows(f"SELECT _id, total, status FROM orders WHERE customer_id = '{cid}'"))

    def order_detail(self, oid):
        return sorted((r["title"], r["qty"]) for r in self._rows(
            "SELECT p.title, i.qty FROM orders o JOIN order_items i ON i.order_id = o._id "
            f"JOIN products p ON p._id = i.product_id WHERE o._id = '{oid}'"))

    def revenue_by_city(self):
        return {r["city"]: int(r["sum"]) for r in self._rows(
            "SELECT c.city, sum(o.total) FROM orders o JOIN customers c ON c._id = o.customer_id "
            "WHERE o.status = 'paid' GROUP BY c.city")}

    def mark(self):
        return self.db.seq() - 1          # seq() is the NEXT sequence

    def refund_with_history(self, oid):
        cur = _nedb_strip(json.loads(self.db.get("orders", oid)))
        cur["status"] = "refunded"
        self.db.put("orders", oid, json.dumps(cur))   # the prior version stays in the DAG
        self._commit()

    def status_as_of(self, oid, m):
        r = self._rows(f"SELECT status FROM orders AS OF SYSTEM TIME {m} WHERE _id = '{oid}'")
        return r[0]["status"] if r else None

    def revenue_by_city_as_of(self, m):
        return {r["city"]: int(r["sum"]) for r in self._rows(
            f"SELECT c.city, sum(o.total) FROM orders AS OF SYSTEM TIME {m} o "
            "JOIN customers c ON c._id = o.customer_id WHERE o.status = 'paid' GROUP BY c.city")}

    def verify(self):
        t = time.perf_counter()
        ok = self.db.verify()
        return {"ok": bool(ok), "ms": (time.perf_counter() - t) * 1e3}

    def version(self):
        return self.nedb.__version__

    def footprint(self):
        self.db.flush()
        total = 0
        for root, _, files in os.walk(self.dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def teardown(self):
        self.db.flush()
        del self.db
        shutil.rmtree(self.dir, ignore_errors=True)


# ── NEDB over HTTP (nedbd) ─────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class NedbdAdapter(NedbAdapter):
    name = "nedbd"
    durability = {"durable": "N/A",
                  "relaxed": "manifest ticker, 1000 ms (nedbd default)"}

    def __init__(self, binary):
        import requests
        self.requests = requests
        self.binary = binary
        if not (binary and os.path.exists(binary)):
            raise RuntimeError(f"nedbd-v2 binary not found at {binary!r} (set NEDBD_BIN)")

    def supports(self, profile):
        if profile == "durable":
            return ("nedbd acknowledges a write before its index entry is fsynced and exposes no "
                    "per-request flush, so it cannot honour 'on disk before the ack'")
        return None

    def setup(self, profile):
        self.profile = profile
        self.dir = tempfile.mkdtemp(prefix="bench-nedbd-")
        self.port = _free_port()
        self.log = open(os.path.join(self.dir, "..", f"nedbd-{self.port}.log"), "w")
        self.proc = subprocess.Popen([self.binary, "--data", os.path.join(self.dir, "data"),
                                      "--port", str(self.port)], stdout=self.log, stderr=subprocess.STDOUT)
        self.base = f"http://127.0.0.1:{self.port}"
        self.s = self.requests.Session()
        deadline = time.time() + 30
        while True:
            try:
                if self.s.get(self.base + "/health", timeout=1).ok:
                    break
            except Exception as e:  # noqa: BLE001 — retried until the deadline, then reported
                last = e
            if self.proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(f"nedbd did not become healthy on :{self.port} "
                                   f"(exit={self.proc.poll()}, last={locals().get('last')!r}); "
                                   f"log: {self.log.name}")
            time.sleep(0.2)
        self._post("/v1/databases", {"name": "bench"})
        self.db_url = self.base + "/v1/databases/bench"
        for coll, field in NEDB_INDEXES:
            self._post(self.db_url[len(self.base):] + "/index", {"coll": coll, "field": field, "kind": "eq"})

    def _post(self, path, body):
        r = self.s.post(self.base + path, json=body, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"nedbd {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def load(self, ds, batch):
        for coll, rows in (("customers", ds.customers), ("products", ds.products),
                           ("orders", ds.orders), ("order_items", ds.items)):
            for i in range(0, len(rows), batch):
                ops = [{"op": "put", "coll": coll, "id": r["id"],
                        "doc": {k: v for k, v in r.items() if k != "id"}} for r in rows[i:i + batch]]
                self._post("/v1/databases/bench/batch", {"ops": ops})

    def _rows(self, sql):
        out = self._post("/v1/databases/bench/query", {"nql": sql})
        rows = out.get("rows", out.get("results"))
        if rows is None:
            raise RuntimeError(f"nedbd /query returned no rows field: keys={list(out)}")
        return [json.loads(r) if isinstance(r, str) else r for r in rows]

    def _get(self, coll, oid):
        r = self.s.get(f"{self.db_url}/rows/{coll}/{oid}", timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"nedbd GET rows -> HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if "row" not in body:
            raise RuntimeError(f"nedbd GET rows returned no 'row' field: keys={list(body)}")
        return body["row"]          # None when the id does not exist (HTTP 200)

    def point_read(self, oid):
        d = self._get("orders", oid)
        return d["status"] if d else None

    def mark(self):
        # Top-level `seq` is the NEXT sequence (same as embedded seq()); the
        # last committed write is tip.seq — the marker AS OF must name.
        body = self.s.get(self.db_url + "/tip", timeout=30).json()
        tip = body.get("tip")
        if not isinstance(tip, dict) or "seq" not in tip:
            raise RuntimeError(f"nedbd /tip returned no tip.seq: {body}")
        return int(tip["seq"])

    def refund_with_history(self, oid):
        cur = _nedb_strip(self._get("orders", oid))
        cur["status"] = "refunded"
        self._post("/v1/databases/bench/put", {"coll": "orders", "id": oid, "doc": cur})

    def verify(self):
        t = time.perf_counter()
        r = self.s.get(self.db_url + "/verify", timeout=300).json()
        ok = r.get("ok", r.get("valid", r.get("verified")))
        return {"ok": bool(ok), "ms": (time.perf_counter() - t) * 1e3}

    def version(self):
        try:
            return self.s.get(self.base + "/health", timeout=5).json().get("version", "?")
        except Exception as e:  # noqa: BLE001
            return f"? ({e})"

    def footprint(self):
        total = 0
        for root, _, files in os.walk(self.dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def teardown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)


# ── Redis ──────────────────────────────────────────────────────────────────

class RedisAdapter(Adapter):
    name = "redis"
    join_model = "application-side join (Redis has no join), pipelined"
    history_model = "per-order history list, MULTI/EXEC with the update"
    durability = {"durable": "appendonly yes, appendfsync always",
                  "relaxed": "appendonly yes, appendfsync everysec"}

    def __init__(self, url):
        import redis
        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.r.ping()

    def setup(self, profile):
        self.r.flushall()
        self.r.config_set("appendonly", "yes")
        self.r.config_set("appendfsync", "always" if profile == "durable" else "everysec")
        self.r.set("clock", 0)

    def load(self, ds, batch):
        r = self.r
        for rows, fn in (
            (ds.customers, lambda p, x: (p.hset(f"c:{x['id']}", mapping={"name": x["name"], "city": x["city"]}),
                                         p.sadd(f"ix:c:city:{x['city']}", x["id"]))),
            (ds.products, lambda p, x: p.hset(f"p:{x['id']}", mapping={"title": x["title"], "price": x["price"]})),
            (ds.orders, lambda p, x: (p.hset(f"o:{x['id']}", mapping={k: v for k, v in x.items() if k != "id"}),
                                      p.sadd(f"ix:o:status:{x['status']}", x["id"]),
                                      p.sadd(f"ix:o:cust:{x['customer_id']}", x["id"]),
                                      p.sadd("ix:o:all", x["id"]))),
            (ds.items, lambda p, x: (p.hset(f"i:{x['id']}", mapping={k: v for k, v in x.items() if k != "id"}),
                                     p.sadd(f"ix:i:order:{x['order_id']}", x["id"]))),
        ):
            for i in range(0, len(rows), batch):
                p = r.pipeline(transaction=True)
                for x in rows[i:i + batch]:
                    fn(p, x)
                p.execute()

    def point_read(self, oid):
        return self.r.hget(f"o:{oid}", "status")

    def customer_orders(self, cid):
        ids = list(self.r.smembers(f"ix:o:cust:{cid}"))
        p = self.r.pipeline(transaction=False)
        for i in ids:
            p.hmget(f"o:{i}", "total", "status")
        return sorted((i, int(t), s) for i, (t, s) in zip(ids, p.execute()))

    def order_detail(self, oid):
        r = self.r
        p = r.pipeline(transaction=False)
        p.exists(f"o:{oid}")
        p.smembers(f"ix:i:order:{oid}")
        exists, item_ids = p.execute()
        if not exists:
            return []
        p = r.pipeline(transaction=False)
        for i in item_ids:
            p.hmget(f"i:{i}", "product_id", "qty")
        items = p.execute()
        p = r.pipeline(transaction=False)
        for pid, _ in items:
            p.hget(f"p:{pid}", "title")
        titles = p.execute()
        return sorted((t, int(q)) for t, (_, q) in zip(titles, items))

    def _revenue(self, rows: List[Tuple[str, int, str]]):
        cids = sorted({c for c, _, _ in rows})
        p = self.r.pipeline(transaction=False)
        for c in cids:
            p.hget(f"c:{c}", "city")
        city = dict(zip(cids, p.execute()))
        out: Dict[str, int] = defaultdict(int)
        for c, total, status in rows:
            if status == "paid":
                out[city[c]] += total
        return dict(out)

    def revenue_by_city(self):
        ids = list(self.r.smembers("ix:o:status:paid"))
        p = self.r.pipeline(transaction=False)
        for i in ids:
            p.hmget(f"o:{i}", "customer_id", "total")
        return self._revenue([(c, int(t), "paid") for c, t in p.execute()])

    def mark(self):
        return int(self.r.get("clock"))

    def refund_with_history(self, oid):
        key = f"o:{oid}"
        with self.r.pipeline(transaction=True) as p:
            while True:
                try:
                    p.watch(key)
                    old = p.hgetall(key)
                    v = self.r.incr("clock")
                    p.multi()
                    p.rpush(f"h:{oid}", json.dumps(dict(old, valid_to=v)))
                    p.sadd("ix:h:touched", oid)
                    p.hset(key, "status", "refunded")
                    p.srem(f"ix:o:status:{old['status']}", oid)
                    p.sadd("ix:o:status:refunded", oid)
                    p.execute()
                    return
                except Exception as e:  # WatchError → retry; anything else is real
                    import redis
                    if isinstance(e, redis.WatchError):
                        continue
                    raise

    @staticmethod
    def _at(history: List[str], m: int) -> Optional[Dict[str, Any]]:
        for h in history:                    # oldest first (RPUSH)
            d = json.loads(h)
            if int(d["valid_to"]) > m:
                return d
        return None

    def status_as_of(self, oid, m):
        p = self.r.pipeline(transaction=False)
        p.lrange(f"h:{oid}", 0, -1)
        p.hget(f"o:{oid}", "status")
        hist, cur = p.execute()
        h = self._at(hist, m)
        return h["status"] if h else cur

    def revenue_by_city_as_of(self, m):
        ids = list(self.r.smembers("ix:o:all"))
        p = self.r.pipeline(transaction=False)
        for i in ids:
            p.hmget(f"o:{i}", "customer_id", "total", "status")
        cur = dict(zip(ids, p.execute()))
        touched = list(self.r.smembers("ix:h:touched"))
        p = self.r.pipeline(transaction=False)
        for i in touched:
            p.lrange(f"h:{i}", 0, -1)
        rows = {i: (c, int(t), s) for i, (c, t, s) in cur.items()}
        for i, hist in zip(touched, p.execute()):
            h = self._at(hist, m)
            if h:
                rows[i] = (h["customer_id"], int(h["total"]), h["status"])
        return self._revenue(list(rows.values()))

    def version(self):
        return self.r.info("server").get("redis_version", "?")

    def footprint(self):
        return int(self.r.info("memory").get("used_memory", 0))

    def teardown(self):
        self.r.flushall()
        self.r.config_set("appendonly", "no")


# ── MongoDB ────────────────────────────────────────────────────────────────

class MongoAdapter(Adapter):
    name = "mongo"
    join_model = "aggregation $lookup"
    history_model = "history collection, multi-document transaction"
    durability = {"durable": "w:1, j:true (journal fsync before ack)",
                  "relaxed": "w:1, j:false (journal commit interval 100 ms)"}

    def __init__(self, url):
        import pymongo
        self.pymongo = pymongo
        self.client = pymongo.MongoClient(url, serverSelectionTimeoutMS=5000)
        self.client.admin.command("ping")
        hello = self.client.admin.command("hello")
        self.replset = bool(hello.get("setName"))

    def supports(self, profile):
        if not self.replset:
            return ("MongoDB is running standalone: multi-document transactions need a replica set, "
                    "and running the history write without one would let Mongo skip the atomicity "
                    "every other engine pays for")
        return None

    def setup(self, profile):
        from pymongo.write_concern import WriteConcern
        self.wc = WriteConcern(w=1, j=(profile == "durable"))
        self.client.drop_database("bench")
        self.db = self.client.get_database("bench", write_concern=self.wc)
        for name in ("customers", "products", "orders", "order_items", "order_history", "clock"):
            self.db.create_collection(name)
        self.db.orders.create_index("status")
        self.db.orders.create_index("customer_id")
        self.db.order_items.create_index("order_id")
        self.db.customers.create_index("city")
        self.db.order_history.create_index([("order_id", 1), ("valid_to", 1)])
        self.db.clock.insert_one({"_id": "clock", "v": 0})

    def load(self, ds, batch):
        for coll, rows in (("customers", ds.customers), ("products", ds.products),
                           ("orders", ds.orders), ("order_items", ds.items)):
            c = self.db[coll]
            for i in range(0, len(rows), batch):
                docs = [dict({k: v for k, v in r.items() if k != "id"}, _id=r["id"]) for r in rows[i:i + batch]]
                c.insert_many(docs, ordered=False)

    def point_read(self, oid):
        d = self.db.orders.find_one({"_id": oid}, {"status": 1})
        return d["status"] if d else None

    def customer_orders(self, cid):
        return sorted((d["_id"], d["total"], d["status"]) for d in
                      self.db.orders.find({"customer_id": cid}, {"total": 1, "status": 1}))

    def order_detail(self, oid):
        pipe = [
            {"$match": {"_id": oid}},
            {"$lookup": {"from": "order_items", "localField": "_id", "foreignField": "order_id", "as": "i"}},
            {"$unwind": "$i"},
            {"$lookup": {"from": "products", "localField": "i.product_id", "foreignField": "_id", "as": "p"}},
            {"$unwind": "$p"},
            {"$project": {"_id": 0, "title": "$p.title", "qty": "$i.qty"}},
        ]
        return sorted((d["title"], d["qty"]) for d in self.db.orders.aggregate(pipe))

    def _city_pipe(self):
        return [
            {"$lookup": {"from": "customers", "localField": "customer_id", "foreignField": "_id", "as": "c"}},
            {"$unwind": "$c"},
            {"$group": {"_id": "$c.city", "sum": {"$sum": "$total"}}},
        ]

    def revenue_by_city(self):
        pipe = [{"$match": {"status": "paid"}}] + self._city_pipe()
        return {d["_id"]: int(d["sum"]) for d in self.db.orders.aggregate(pipe)}

    def mark(self):
        return int(self.db.clock.find_one({"_id": "clock"})["v"])

    def refund_with_history(self, oid):
        def txn(s):
            v = self.db.clock.find_one_and_update(
                {"_id": "clock"}, {"$inc": {"v": 1}},
                return_document=self.pymongo.ReturnDocument.AFTER, session=s)["v"]
            old = self.db.orders.find_one({"_id": oid}, session=s)
            h = {k: val for k, val in old.items() if k != "_id"}
            h.update(order_id=oid, valid_to=v)
            self.db.order_history.insert_one(h, session=s)
            self.db.orders.update_one({"_id": oid}, {"$set": {"status": "refunded"}}, session=s)
        with self.client.start_session() as s:
            s.with_transaction(txn, write_concern=self.wc)

    def status_as_of(self, oid, m):
        h = self.db.order_history.find_one({"order_id": oid, "valid_to": {"$gt": m}},
                                           sort=[("valid_to", 1)], projection={"status": 1})
        if h:
            return h["status"]
        return self.point_read(oid)

    def revenue_by_city_as_of(self, m):
        pipe = [
            {"$lookup": {"from": "order_history", "let": {"oid": "$_id"}, "as": "h", "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$order_id", "$$oid"]}, {"$gt": ["$valid_to", m]}]}}},
                {"$sort": {"valid_to": 1}}, {"$limit": 1},
                {"$project": {"status": 1, "total": 1}}]}},
            {"$addFields": {"h": {"$first": "$h"}}},
            {"$addFields": {"status": {"$ifNull": ["$h.status", "$status"]},
                            "total": {"$ifNull": ["$h.total", "$total"]}}},
            {"$match": {"status": "paid"}},
        ] + self._city_pipe()
        return {d["_id"]: int(d["sum"]) for d in self.db.orders.aggregate(pipe)}

    def version(self):
        return self.client.server_info().get("version", "?")

    def footprint(self):
        return int(self.db.command("dbStats").get("storageSize", 0))

    def teardown(self):
        self.client.drop_database("bench")


# ─────────────────────────────────────────────────────────────── runner ─────

def expect(label, want):
    def check(key, got):
        w = want(key) if callable(want) else want
        if got != w:
            raise BenchFailure(f"{label}({key!r}): engine answered {got!r}, oracle says {w!r}")
    return check


def run_engine(a: Adapter, profile: str, ds: Dataset, plan: Plan, batch: int) -> Dict[str, Any]:
    res: Dict[str, Any] = {"phases": {}}
    a.setup(profile)
    try:
        res["version"] = a.version()
        n_rows = len(ds.customers) + len(ds.products) + len(ds.orders) + len(ds.items)
        t = time.perf_counter()
        a.load(ds, batch)
        dt = time.perf_counter() - t
        res["phases"]["load"] = {"ops": n_rows, "seconds": dt, "ops_per_s": n_rows / dt}

        ph = res["phases"]
        ph["point_read"] = timed_ops(a.point_read, plan.point,
                                     expect("point_read", lambda k: ds.order_by_id[k]["status"]))
        ph["customer_orders"] = timed_ops(
            a.customer_orders, plan.customer,
            expect("customer_orders", lambda k: sorted(
                (o, ds.order_by_id[o]["total"], ds.order_by_id[o]["status"]) for o in ds.orders_of[k])))
        ph["order_detail"] = timed_ops(
            a.order_detail, plan.detail,
            expect("order_detail", lambda k: sorted(
                (ds.title_of[i["product_id"]], i["qty"]) for i in ds.items_of[k])))
        ph["revenue_by_city"] = timed_ops(lambda _: a.revenue_by_city(), list(range(plan.analytic_repeats)),
                                          expect("revenue_by_city", plan.expected_before))

        m = a.mark()
        ph["refund_with_history"] = timed_ops(a.refund_with_history, plan.refund, lambda k, g: None)
        # the writes happened — the current state must reflect every one of them
        for oid in plan.refund[:50]:
            expect("post-refund point_read", "refunded")(oid, a.point_read(oid))
        expect("revenue_by_city (after)", plan.expected_after)("current", a.revenue_by_city())

        ph["status_as_of"] = timed_ops(lambda k: a.status_as_of(k, m), plan.as_of,
                                       expect("status_as_of", "paid"))
        ph["revenue_by_city_as_of"] = timed_ops(lambda _: a.revenue_by_city_as_of(m),
                                                list(range(plan.analytic_repeats)),
                                                expect("revenue_by_city_as_of", plan.expected_before))
        res["verify"] = a.verify()
        if res["verify"] is not None and not res["verify"]["ok"]:
            raise BenchFailure(f"{a.name}: integrity verify() returned false")
        res["footprint_bytes"] = a.footprint()
        res["status"] = "ok"
    finally:
        a.teardown()
    return res


def build_adapters(which: List[str]) -> Tuple[Dict[str, Adapter], Dict[str, str]]:
    made: Dict[str, Adapter] = {}
    skipped: Dict[str, str] = {}
    default_bin = None
    try:
        import nedb
        default_bin = os.path.join(os.path.dirname(nedb.__file__), "nedbd-v2")
    except Exception:  # noqa: BLE001 — nedb itself missing is reported by the nedb leg below
        pass
    factories = {
        "nedb": lambda: NedbAdapter(),
        "nedbd": lambda: NedbdAdapter(os.environ.get("NEDBD_BIN") or default_bin),
        "sqlite": lambda: SqliteAdapter(),
        "postgres": lambda: PostgresAdapter(os.environ.get(
            "PG_DSN", "postgresql://postgres:bench@127.0.0.1:5432/postgres")),
        "redis": lambda: RedisAdapter(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")),
        "mongo": lambda: MongoAdapter(os.environ.get(
            "MONGO_URL", "mongodb://127.0.0.1:27017/?replicaSet=rs0&directConnection=true")),
    }
    for name in which:
        try:
            made[name] = factories[name]()
        except Exception as e:  # noqa: BLE001 — every skip is named, never silent
            skipped[name] = f"{type(e).__name__}: {e}"
            print(f"[bench] SKIP {name}: {skipped[name]}", file=sys.stderr, flush=True)
    return made, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orders", type=int, default=20000)
    ap.add_argument("--ops", type=int, default=2000, help="keys per point-style phase")
    ap.add_argument("--batch", type=int, default=500, help="rows per load transaction")
    ap.add_argument("--engines", default=",".join(ENGINES))
    ap.add_argument("--profiles", default=",".join(PROFILES))
    ap.add_argument("--require", default="", help="comma list of engines that must run (CI sets all)")
    ap.add_argument("--json")
    ap.add_argument("--markdown")
    args = ap.parse_args(argv)

    engines = [e for e in args.engines.split(",") if e]
    profiles = [p for p in args.profiles.split(",") if p]
    ds = Dataset(args.orders)
    plan = Plan(ds, args.ops)
    print(f"[bench] dataset: {len(ds.customers)} customers, {len(ds.products)} products, "
          f"{len(ds.orders)} orders, {len(ds.items)} order_items; refund wave {len(plan.refund)}",
          flush=True)

    adapters, skipped = build_adapters(engines)
    required = [e for e in args.require.split(",") if e]
    missing = [e for e in required if e in skipped]
    if missing:
        for e in missing:
            print(f"[bench] REQUIRED engine unavailable: {e}: {skipped[e]}", file=sys.stderr)
        return 2

    out: Dict[str, Any] = {
        "meta": {
            "date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "commit": os.environ.get("GITHUB_SHA", "local")[:12],
            "run_url": (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
                        f"/actions/runs/{os.environ['GITHUB_RUN_ID']}") if os.environ.get("GITHUB_RUN_ID") else None,
            "runner": f"{platform.system()} {platform.machine()}, {os.cpu_count()} vCPU, Python {platform.python_version()}",
            "orders": args.orders, "ops": args.ops, "batch": args.batch,
            "rows": {"customers": len(ds.customers), "products": len(ds.products),
                     "orders": len(ds.orders), "order_items": len(ds.items)},
            "refund_wave": len(plan.refund),
        },
        "engines": {e: {"label": ENGINE_LABEL[e], "transport": TRANSPORT[e]} for e in engines},
        "skipped": skipped,
        "results": {p: {} for p in profiles},
    }
    for name, a in adapters.items():
        out["engines"][name].update(durability=a.durability, join_model=a.join_model,
                                    history_model=a.history_model, integrity=a.integrity)

    failed = False
    for profile in profiles:
        for name in engines:
            if name not in adapters:
                continue
            a = adapters[name]
            why = a.supports(profile)
            if why:
                out["results"][profile][name] = {"status": "n/a", "reason": why}
                print(f"[bench] {profile:8s} {name:9s} N/A — {why}", flush=True)
                continue
            print(f"[bench] {profile:8s} {name:9s} running…", flush=True)
            try:
                r = run_engine(a, profile, ds, plan, args.batch)
            except BenchFailure as e:
                failed = True
                r = {"status": "wrong-answer", "reason": str(e)}
                print(f"[bench] {profile:8s} {name:9s} WRONG ANSWER — {e}", file=sys.stderr, flush=True)
            except Exception as e:  # noqa: BLE001 — recorded and fails the run
                failed = True
                r = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
                print(f"[bench] {profile:8s} {name:9s} ERROR — {r['reason']}", file=sys.stderr, flush=True)
            out["results"][profile][name] = r
            if r.get("status") == "ok":
                summary = "  ".join(f"{k}={v['ops_per_s']:,.0f}/s" for k, v in r["phases"].items())
                print(f"[bench] {profile:8s} {name:9s} ok  {summary}", flush=True)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
    if args.markdown:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from compare_report import render
        with open(args.markdown, "w") as f:
            f.write(render(out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
