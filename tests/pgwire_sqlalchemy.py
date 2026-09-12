#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
SQLAlchemy — reflection and the ORM, which is what "a Python app can use it"
actually means.

# Why this suite is the one that mattered

Three separate defects lived behind it, and psql could see none of them.

1. It could not CONNECT. The dialect opens every connection with
   `select pg_catalog.version()` — a relation-free SELECT calling a catalogue
   function. The endpoint answered a small table of exact spellings
   (`SELECT VERSION()`, `SELECT 1`, `SELECT CURRENT_SCHEMA`) and refused
   everything else with "SELECT without FROM is not supported", so dialect
   initialisation failed on its first statement and no application got further.

2. Reflection needed `GROUP BY` and `array_agg(x ORDER BY y)`. Column, primary
   key and index reflection are all built on them, and without reflection
   `MetaData.reflect()`, `autoload_with` and Alembic cannot run.

3. Every FILTERED query returned nothing. `WHERE orders.status = 'paid'`
   searched for a field literally named "orders.status" and answered ZERO ROWS
   with no error — so `filter_by`, `in_`, `like` and `.get(pk)` all silently
   lied. That is the failure this engine exists to not have.

# What is asserted

Connect, then reflect, then query — in that order, because each depends on the
one before. The ORM section is deliberately last: if `get_columns` is broken,
an ORM failure tells you nothing about the ORM.
"""
from pgwire_suite import suite


@suite("sqlalchemy", requires="sqlalchemy")
def run(fx, c):
    import sqlalchemy as sa
    from sqlalchemy.orm import Session, declarative_base

    eng = sa.create_engine(
        f"postgresql+psycopg2://nedb@127.0.0.1:{fx.pg_port}/{fx.dbname}")

    # ── 1. connect ──────────────────────────────────────────────────────────
    # `create_engine` is lazy; the dialect initialises on first connect, and
    # that is where `select pg_catalog.version()` is sent.
    with eng.connect() as conn:
        c.ok("the dialect initialises (select pg_catalog.version())", True)
        c.eq("a literal select answers",
             conn.execute(sa.text("SELECT 1")).scalar(), "1")
        c.eq("a bound parameter round-trips",
             [r[0] for r in conn.execute(
                 sa.text("SELECT _id FROM orders WHERE status = :s"),
                 {"s": "open"})],
             ["2"])

    # ── 2. reflect ──────────────────────────────────────────────────────────
    insp = sa.inspect(eng)
    c.ok("has_table finds a collection", insp.has_table("orders"))
    c.ok("has_table is honest about one that does not exist",
         not insp.has_table("nosuchcollection"))
    c.eq("get_table_names lists the collections",
         sorted(insp.get_table_names()), ["drivers", "orders"])
    cols = [col["name"] for col in insp.get_columns("orders")]
    c.ok("get_columns returns the observed fields",
         {"status", "total"} <= set(cols), str(cols))
    c.ok("...including engine metadata as columns", "_seq" in cols, str(cols))
    # NEDB has no declared keys or indexes, and saying so is the truthful
    # answer — an invented primary key would make an ORM write UPDATEs against
    # a column that means nothing.
    c.eq("get_pk_constraint reports no key",
         insp.get_pk_constraint("orders")["constrained_columns"], [])
    c.eq("get_indexes reports none", insp.get_indexes("orders"), [])
    c.eq("get_view_names reports none", insp.get_view_names(), [])
    c.ok("get_schema_names includes public",
         "public" in insp.get_schema_names(), str(insp.get_schema_names()))

    md = sa.MetaData()
    md.reflect(bind=eng)
    c.eq("MetaData.reflect() discovers the whole database",
         sorted(md.tables), ["drivers", "orders"])

    # ── 3. Core queries ─────────────────────────────────────────────────────
    # SQLAlchemy qualifies every column it emits, which is what made the
    # silent-empty bug universal rather than occasional.
    t = sa.Table("orders", sa.MetaData(), autoload_with=eng)
    with eng.connect() as conn:
        q = lambda stmt: conn.execute(stmt).fetchall()               # noqa: E731
        c.eq("select two columns", len(q(sa.select(t.c.status, t.c.total))), 3)
        c.eq("a filtered select (the qualified WHERE)",
             sorted(r[0] for r in q(sa.select(t.c._id).where(t.c.status == "paid"))),
             ["1", "3"])
        c.eq("in_()",
             len(q(sa.select(t.c._id).where(t.c.status.in_(["paid", "open"])))), 3)
        c.eq("like()",
             sorted(r[0] for r in q(sa.select(t.c._id).where(t.c.status.like("pa%")))),
             ["1", "3"])
        c.eq("order_by descending",
             [r[0] for r in q(sa.select(t.c.total).order_by(t.c.total.desc()))],
             [300, 120, 40])
        c.eq("limit", len(q(sa.select(t.c._id).limit(2))), 2)
        c.eq("count over the table",
             conn.execute(sa.select(sa.func.count()).select_from(t)).scalar(), 3)
        c.eq("group_by with count",
             sorted(q(sa.select(t.c.status, sa.func.count()).group_by(t.c.status))),
             [("open", 1), ("paid", 2)])
        c.eq("group_by with a named aggregate alongside count",
             sorted(q(sa.select(t.c.status, sa.func.count(), sa.func.sum(t.c.total))
                      .group_by(t.c.status))),
             [("open", 1, 40), ("paid", 2, 420)])

    # ── 4. the ORM ──────────────────────────────────────────────────────────
    Base = declarative_base()

    class Order(Base):
        __tablename__ = "orders"
        # NEDB declares no primary key, so the mapping names one. `_id` IS the
        # identity of a document, so this is a description rather than an
        # invention.
        _id = sa.Column(sa.Text, primary_key=True)
        status = sa.Column(sa.Text)
        total = sa.Column(sa.BigInteger)

    with Session(eng) as s:
        c.eq("query().all()", len(s.query(Order).all()), 3)
        c.eq("filter_by()",
             sorted(o._id for o in s.query(Order).filter_by(status="paid").all()),
             ["1", "3"])
        c.eq("filter() with an operator",
             sorted(o._id for o in s.query(Order).filter(Order.total > 100).all()),
             ["1", "3"])
        got = s.get(Order, "1")
        c.ok("get() by primary key", got is not None and got.total == 120,
             str(got and got.total))
        c.eq("order_by + limit",
             [o._id for o in s.query(Order).order_by(Order.total.desc()).limit(1)],
             ["3"])
        # `.count()` wraps the whole query in a derived table. Counting rows
        # that ARE the inner query's rows is counting the inner query, so the
        # endpoint flattens it — and refuses to when a LIMIT or DISTINCT inside
        # would make the two counts different numbers.
        c.eq("count()", s.query(Order).count(), 3)
        c.eq("count() with a filter",
             s.query(Order).filter(Order.status == "paid").count(), 2)
