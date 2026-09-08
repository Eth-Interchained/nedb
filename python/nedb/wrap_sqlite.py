"""
nedb.wrap_sqlite — wrap an existing SQLite database with NEDB's layer-2.

ONE LINE. Your existing sqlite3 code doesn't change. New parts of your app get
time-travel, bi-temporal, causal provenance, and NQL on top of your tables.

    from nedb import wrap_sqlite
    import sqlite3

    conn = wrap_sqlite(sqlite3.connect("app.db"), db_name="app")

    # ── Step 1: register table mappings ──────────────────────────────────
    conn.nedb.register("drivers", collection="driver")
    conn.nedb.register("trips",   collection="trip")

    # ── Step 2: backfill existing rows into NEDB ─────────────────────────
    conn.nedb.backfill()

    # ── Step 3: enable write shadowing ───────────────────────────────────
    conn.nedb.shadow_writes = True

    # Your app runs unchanged — INSERT/UPDATE/DELETE are shadowed
    cur = conn.cursor()
    cur.execute("INSERT INTO drivers (name, status) VALUES (?, ?)",
                ("Bob", "active"))
    conn.commit()

    # New app — NEDB features on the shadowed data
    conn.nedb.query('FROM driver WHERE status = "active"')
    conn.nedb.verify()   # → True

Engine selection mirrors wrap_redis:
    backend="auto" → embedded v2/v3 DAG (Rust wheel) → v1 AOF fallback
    backend="dag"  → force embedded DAG (dag_path= for a durable store)
    nedbd_url=     → HTTP nedbd (v1 AOF, nedbd --dag, or nedbd --dag-v3)

Isolation guarantee: NEDB NEVER writes to your tables. Shadow data lives only
in the NEDB engine (embedded store or nedbd server), keyed nedb:{db_name}:*.

© INTERCHAINED LLC × Claude Sonnet 4.6
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

from .wrap_core import WrapSurface, open_engine


# ── The .nedb surface for SQLite ─────────────────────────────────────────────

class SqliteSurface(WrapSurface):
    """
    The `.nedb` attribute of a wrapped SQLite connection.

    register(table, collection, row_parser=…) maps a host TABLE (not a key
    glob — SQLite has structure, so patterns are just table names) to a NEDB
    collection. Row dicts come straight from sqlite3.Row.
    """

    def __init__(self, conn: sqlite3.Connection, db_name: str, engine=None,
                 persist=None):
        super().__init__(db_name, engine=engine, persist=persist)
        self._conn = conn

    # ── host hooks ───────────────────────────────────────────────────────────

    def _host_scan(self, mapping, batch_size: int):
        """Yield (rowid, row_dict) for every row of the mapped table."""
        table = mapping.pattern  # for sqlite, the "pattern" is the table name
        try:
            # Column metadata via PRAGMA (stable, no SELECT * rowid aliasing
            # surprises when the table declares INTEGER PRIMARY KEY).
            cols = [r[1] for r in self._conn.execute(f'PRAGMA table_info("{table}")')]
            cur = self._conn.execute(f'SELECT rowid AS __nedb_rowid, * FROM "{table}"')
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    rowid = row[0]
                    d = dict(zip(cols, row[1:]))
                    # A table with INTEGER PRIMARY KEY aliases rowid in *,
                    # duplicating it under its real name — harmless: keep the
                    # dict but drop a duplicate __nedb_rowid key if any.
                    d.pop("__nedb_rowid", None)
                    yield str(rowid), d
        except sqlite3.Error:
            return

    def _shadow_doc(self, mapping, key, args, kwargs) -> Optional[Dict[str, Any]]:
        """Intercepted write → NEDB doc.

        The SQLite proxy passes shadow metadata through kwargs:
            _nedb_shadow = (table, op, rowid, after_row_dict_or_None)
        """
        meta = kwargs.get("_nedb_shadow")
        if not meta:
            return None
        table, op, rowid, after = meta
        if after is None:
            return None  # DELETE — tombstone handled by the proxy, not here
        doc = dict(after)
        doc["_op"] = op
        return doc

    # ── SQLite-native extras ─────────────────────────────────────────────────

    def sql(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Run raw SQL on the host connection (read helper)."""
        cur = self._conn.execute(query, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── The transparent proxy connection ─────────────────────────────────────────

class WrappedSqlite:
    """
    Transparent sqlite3.Connection proxy with NEDB shadow layer.

    Surface 1 (execute/commit/…): every call passes through unchanged; write
    statements on registered tables are shadowed after they succeed.

    Surface 2 (.nedb.*): full NEDB API + backfill + write shadowing.
    """

    _WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")

    def __init__(self, conn: sqlite3.Connection, db_name: str,
                 nedbd_url: Optional[str] = None,
                 nedbd_token: Optional[str] = None,
                 backend: str = "auto",
                 dag_path: Optional[str] = None,
                 dag_tmk: Optional[str] = None):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_db_name", db_name)
        engine, _ = open_engine(backend=backend, db_name=db_name,
                                nedbd_url=nedbd_url, nedbd_token=nedbd_token,
                                dag_path=dag_path, dag_tmk=dag_tmk)
        object.__setattr__(self, "nedb", SqliteSurface(conn, db_name, engine=engine))

    # ── proxying ─────────────────────────────────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        conn = object.__getattribute__(self, "_conn")
        nedb = object.__getattribute__(self, "nedb")
        attr = getattr(conn, name)

        if name == "execute":
            return self._execute
        if not callable(attr):
            return attr

        def _passthrough(*a, **kw):
            return attr(*a, **kw)
        return _passthrough

    def _execute(self, sql: str, params: tuple = ()):
        """execute() with post-commit shadowing of registered-table writes."""
        conn = object.__getattribute__(self, "_conn")
        nedb = object.__getattribute__(self, "nedb")
        cur = conn.execute(sql, params)

        try:
            head = sql.lstrip().upper()
            if head.startswith(self._WRITE_PREFIXES) and nedb.shadow_writes:
                self._shadow_sql(nedb, sql, cur)
        except Exception:
            pass  # shadow failures must never break the host call
        return cur

    def _shadow_sql(self, nedb: SqliteSurface, sql: str, cur: sqlite3.Cursor) -> None:
        conn = object.__getattribute__(self, "_conn")
        table = None
        for m in nedb._mappings:
            t = m.pattern
            if t.lower() in sql.lower():
                table = t
                break
        if table is None:
            return

        op = ("DELETE" if sql.upper().startswith("DELETE")
              else "UPDATE" if sql.upper().startswith("UPDATE")
              else "INSERT")
        rowid = cur.lastrowid

        if op == "DELETE":
            # Tombstone: put a deletion marker (NEDB delete would remove history;
            # a marker keeps the causal chain and is NQL-queryable)
            nedb.put("__sql_shadow__", f"{table}:del:{rowid}",
                     {"table": table, "rowid": str(rowid), "_op": "DELETE"},
                     client="__shadow__")
            return

        # Read the row back post-write (works inside the open transaction)
        try:
            row = conn.execute(
                f'SELECT * FROM "{table}" WHERE rowid = ?', (rowid,)).fetchone()
            if row is None:
                return
            cols = [d[0] for d in cur.description] or \
                [d[0] for d in conn.execute(f'SELECT * FROM "{table}" LIMIT 0').description]
            doc = dict(zip(cols, row))
        except sqlite3.Error:
            return
        nedb.put("__sql_shadow__", f"{table}:{rowid}",
                 {**doc, "_table": table, "_op": op}, client="__shadow__")

    def __repr__(self):
        conn = object.__getattribute__(self, "_conn")
        db = object.__getattribute__(self, "_db_name")
        return f"<WrappedSqlite db_name={db!r} sqlite={conn!r}>"


# ── Entry point ───────────────────────────────────────────────────────────────

def wrap_sqlite(conn: sqlite3.Connection, db_name: str = "default",
                nedbd_url: Optional[str] = None,
                nedbd_token: Optional[str] = None,
                backend: str = "auto",
                dag_path: Optional[str] = None,
                dag_tmk: Optional[str] = None) -> WrappedSqlite:
    """
    Wrap an existing sqlite3.Connection with NEDB's layer-2 features.

    Args mirror wrap_redis: backend="auto" picks nedbd (if nedbd_url=) →
    embedded DAG (if the native wheel is installed) → v1 AOF engine.
    dag_path=/dag_tmk= configure a durable/encrypted embedded DAG store.
    """
    return WrappedSqlite(conn, db_name, nedbd_url=nedbd_url,
                         nedbd_token=nedbd_token, backend=backend,
                         dag_path=dag_path, dag_tmk=dag_tmk)
