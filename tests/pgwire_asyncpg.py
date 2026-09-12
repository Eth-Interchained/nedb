#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
asyncpg — the strictest client, and therefore the most useful one.

psycopg2 interpolates parameters client-side and never asks the server what a
placeholder's type is. asyncpg does the opposite: it reads
`ParameterDescription`, BELIEVES it, and refuses the call locally when the
value it was handed does not match. So a wrong OID here is not a slow path or
a degraded result — it is `DataError: expected str, got int`, raised before a
single byte reaches the server.

That is how `WHERE pg_type.oid = $1` was caught being typed `text`: the
parameter sat beside a CATALOGUE column, and the type sampler only knew how to
sample a user collection. psql never noticed, because psql sends everything as
text and lets the server sort it out.
"""
import asyncio

from pgwire_suite import suite


@suite("asyncpg", requires="asyncpg")
def run(fx, c):
    import asyncpg
    asyncio.run(_run(fx, asyncpg, c))


async def _run(fx, asyncpg, c):
    async def connect():
        return await asyncpg.connect(
            host="127.0.0.1", port=fx.pg_port, database=fx.dbname, user="nedb")

    async def refused(name, sql, needle):
        """Assert a refusal on a FRESH connection.

        asyncpg puts a connection into a failed state after a server error, so
        reusing one would make the next assertion measure the connection
        rather than the engine.
        """
        conn = await connect()
        try:
            await conn.fetch(sql)
            c.ok(name, False, "it answered instead of refusing")
        except Exception as e:                                       # noqa: BLE001
            c.ok(name, needle.lower() in str(e).lower(), str(e)[:140])
        finally:
            await conn.close()

    conn = await connect()
    try:
        # asyncpg parses the version string on connect and will not proceed if
        # it cannot.
        c.ok("server version parses", conn.get_server_version().major >= 9,
             str(conn.get_server_version()))

        # ── typed parameters, which is the whole point of this suite ────────
        rows = await conn.fetch(
            "SELECT _id, total FROM orders WHERE status = $1 AND total > $2",
            "paid", 100)
        c.eq("text + int parameters together", sorted(r["_id"] for r in rows), ["1", "3"])

        c.eq("an int parameter against a user column",
             await conn.fetchval("SELECT count(*) FROM orders WHERE total > $1", 50), 2)

        # The one that was broken: a parameter beside a CATALOGUE column. The
        # server advertised text, asyncpg believed it, and refused to send 23.
        c.eq("an int parameter against a CATALOGUE column",
             await conn.fetchval(
                 "SELECT typname FROM pg_catalog.pg_type WHERE oid = $1", 23),
             "int4")

        # A clause position types from the grammar rather than from a column —
        # there is no column to the left of `LIMIT`.
        c.eq("an int parameter in a LIMIT clause",
             len(await conn.fetch("SELECT _id FROM orders LIMIT $1", 2)), 2)

        # ── prepared statements, reused ─────────────────────────────────────
        stmt = await conn.prepare("SELECT status FROM orders WHERE _id = $1")
        c.eq("a prepared statement reused across values",
             [await stmt.fetchval("1"), await stmt.fetchval("2")], ["paid", "open"])

        # ── the catalogue, over the extended protocol ───────────────────────
        c.eq("relations are listed",
             sorted(r["relname"] for r in await conn.fetch(
                 "SELECT relname FROM pg_catalog.pg_class WHERE relkind = 'r'")),
             ["drivers", "orders"])
        c.eq("information_schema is readable",
             sorted(r["table_name"] for r in await conn.fetch(
                 "SELECT table_name FROM information_schema.tables")),
             ["drivers", "orders"])

        # ── a qualified predicate, which every ORM writes ───────────────────
        c.eq("a QUALIFIED column in WHERE finds its field",
             [r["_id"] for r in await conn.fetch(
                 "SELECT orders._id FROM orders WHERE orders.status = $1", "open")],
             ["2"])
        c.eq("a table ALIAS works as a qualifier",
             [r["status"] for r in await conn.fetch(
                 "SELECT o.status FROM orders o WHERE o.total = $1", 40)],
             ["open"])

        # ── grouped queries, the shape an ORM emits ─────────────────────────
        got = sorted((r["status"], r["n"]) for r in await conn.fetch(
            "SELECT orders.status, count(*) AS n FROM orders GROUP BY orders.status"))
        c.eq("a grouped query with a mixed select list", got, [("open", 1), ("paid", 2)])

        # ── time travel, through a driver ───────────────────────────────────
        # The point of the endpoint: ordinary SQL, and history for free.
        before = await conn.fetchval("SELECT _seq FROM orders WHERE _id = '1'")
        await conn.execute("UPDATE orders SET total = 999 WHERE _id = '1'")
        c.eq("an UPDATE is visible",
             await conn.fetchval("SELECT total FROM orders WHERE _id = '1'"), 999)
        c.eq("...and the prior value is still readable at its own seq",
             await conn.fetchval(
                 "SELECT total FROM orders AS OF SYSTEM TIME $1 WHERE _id = '1'",
                 before),
             120)
    finally:
        await conn.close()

    # ── boundaries, each named ──────────────────────────────────────────────
    await refused("DDL is refused by name", "CREATE TABLE t (a int)", "DDL")
    await refused("TRUNCATE is refused, and says why", "TRUNCATE orders", "append-only")
    await refused("an unknown qualifier is refused, not answered empty",
                  "SELECT _id FROM orders WHERE nosuch.status = 'x'",
                  "no table or alias")
