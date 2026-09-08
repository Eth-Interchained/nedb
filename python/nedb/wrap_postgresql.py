"""
nedb.wrap_postgresql — wrap an existing psycopg/DB-API connection with NEDB's layer-2.

ONE LINE. Your existing Postgres code doesn't change. New parts of your app get
time-travel, bi-temporal causal provenance, and NQL on top of your tables.

    from nedb import wrap_postgresql
    import psycopg2                 # or psycopg (v3) — any DB-API 2.0 conn

    conn = wrap_postgresql(psycopg2.connect("dbname=app"), db_name="app")

    # ── Step 1: register table mappings ──────────────────────────────────
    conn.nedb.register("drivers", collection="driver", pk="id")

    # ── Step 2: backfill existing rows into NEDB ─────────────────────────
    conn.nedb.backfill()

    # ── Step 3: enable write shadowing ───────────────────────────────────
    conn.nedb.shadow_writes = True

    # Your app runs unchanged — and each shadow_row() call chains the write:
    cur = conn.cursor()
    cur.execute("INSERT INTO drivers (name, status) VALUES (%s, %s) RETURNING *",
                ("Bob", "active"))
    row = cur.fetchone()
    conn.commit()
    conn.nedb.shadow_row("drivers", row[0], dict_from_cursor(cur, row))

    conn.nedb.query('FROM driver WHERE status = "active"')
    conn.nedb.verify()   # → True

Works with any DB-API 2.0 connection (psycopg2, psycopg 3). Engine selection
mirrors wrap_redis: backend="auto" → nedbd (if nedbd_url=) → embedded v2/v3 DAG
(if the Rust wheel is installed) → v1 AOF engine.

Isolation guarantee: NEDB NEVER writes to your tables. Shadow data lives only
in the NEDB engine.

© INTERCHAINED LLC × Claude Sonnet 4.6
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .wrap_core import WrapSurface, open_engine


class PostgresSurface(WrapSurface):
    """The `.nedb` attribute of a wrapped Postgres connection."""

    def __init__(self, conn: Any, db_name: str, engine=None, persist=None):
        super().__init__(db_name, engine=engine, persist=persist)
        self._conn = conn
        self._pks: Dict[str, str] = {}   # table → primary-key column

    def register(self, pattern: str, collection: str,             # type: ignore[override]
                 id_extractor=None, value_parser=None, value_type: str = "string",
                 pk: Optional[str] = None):
        """register(table, collection, pk="id") — pk names the row-id column."""
        surface = super().register(pattern, collection,
                                   id_extractor, value_parser, value_type)
        if pk:
            self._pks[pattern] = pk
        return surface

    def _host_scan(self, mapping, batch_size: int):
        """Yield (pk_value, row_dict) for every row of the mapped table."""
        table = mapping.pattern
        pk = self._pks.get(table)
        cur = self._conn.cursor()
        try:
            if pk:
                cur.execute(f'SELECT * FROM "{table}" ORDER BY "{pk}"')
            else:
                cur.execute(f'SELECT * FROM "{table}"')
            if cur.description is None:
                return
            cols = [d[0] for d in cur.description]
            pk = pk or cols[0]
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    d = dict(zip(cols, row))
                    yield str(d.get(pk, id(d))), d
        except Exception:
            return
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _shadow_doc(self, mapping, key, args, kwargs) -> Optional[Dict[str, Any]]:
        return None  # Postgres shadowing is explicit via shadow_row()


class WrappedPostgres:
    """
    Transparent DB-API 2.0 proxy with NEDB shadow layer.

    Surface 1 (cursor/commit/…): every call passes through unchanged.

    Surface 2 (.nedb.*): full NEDB API + backfill + explicit shadow_row().
    Postgres shadowing is explicit: after your INSERT/UPDATE ... RETURNING,
    call conn.nedb.shadow_row(table, pk, row_dict) to chain it (or wire
    triggers to a notify loop — see README).
    """

    def __init__(self, conn: Any, db_name: str,
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
        object.__setattr__(self, "nedb", PostgresSurface(conn, db_name, engine=engine))

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_conn"), name)

    def shadow_row(self, table: str, pk: Any, row: Optional[Dict[str, Any]],
                   op: str = "UPSERT") -> None:
        """Chain one host row into NEDB. Call after your INSERT/UPDATE.

        Lands in the registered collection for `table` (NQL-queryable via the
        normal FROM <collection> grammar); row=None chains a DELETE tombstone.
        No-ops unless shadow_writes=True.
        """
        nedb = object.__getattribute__(self, "nedb")
        if not nedb.shadow_writes:
            return
        try:
            if row is None:
                nedb.put("__pg_shadow__", f"{table}:del:{pk}",
                         {"table": table, "pk": str(pk), "_op": "DELETE"},
                         client="__shadow__")
                return
            # route into the registered collection when one matches
            coll = next((m.collection for m in nedb._mappings
                         if m.pattern == table), None)
            if coll:
                nedb.put(coll, str(pk),
                         {**dict(row), "_table": table, "_op": op},
                         client="__shadow__")
            else:
                nedb.put("__pg_shadow__", f"{table}:{pk}",
                         {**dict(row), "_table": table, "_op": op},
                         client="__shadow__")
        except Exception:
            pass

    def __repr__(self):
        conn = object.__getattribute__(self, "_conn")
        db = object.__getattribute__(self, "_db_name")
        return f"<WrappedPostgres db_name={db!r} pg={conn!r}>"


def wrap_postgresql(conn: Any, db_name: str = "default",
                    nedbd_url: Optional[str] = None,
                    nedbd_token: Optional[str] = None,
                    backend: str = "auto",
                    dag_path: Optional[str] = None,
                    dag_tmk: Optional[str] = None) -> WrappedPostgres:
    """
    Wrap an existing DB-API 2.0 Postgres connection with NEDB's layer-2.

    Args mirror wrap_redis: backend="auto" picks nedbd (if nedbd_url=) →
    embedded DAG (if the native wheel is installed) → v1 AOF engine.
    """
    return WrappedPostgres(conn, db_name, nedbd_url=nedbd_url,
                           nedbd_token=nedbd_token, backend=backend,
                           dag_path=dag_path, dag_tmk=dag_tmk)
