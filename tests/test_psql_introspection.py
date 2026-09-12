#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
psql's backslash commands, driven by the REAL psql binary.

# Why this suite exists

`\\dt` is the second thing anybody types after connecting. It used to come
back empty, which does not read as "unsupported" — it reads as *"this database
is broken"* or *"my data is gone"*.

Making it work is not a matter of listing tables. psql's `\\dt` is ONE
statement containing:

  * two `LEFT JOIN`s,
  * a nine-branch `CASE`,
  * two scalar function calls (`pg_get_userbyid`, `pg_table_is_visible`),
  * four qualified column references across three aliased tables,
  * an `IN ('r','p','')` list including an empty string,
  * a `!~` regex,
  * and `ORDER BY 1,2` — by ORDINAL.

Every one of those has to work or the command does not. None of them can be
expressed in NQL, which is why they run through a real SQL engine
(`sqlselect.rs`) rather than through the SQL→NQL translation.

# Why the REAL psql

An assertion written against a query *I* typed proves only that I can type the
query I implemented. psql's actual SQL is the specification, it changes between
versions, and it is full of constructs nobody would write by hand
(`OPERATOR(pg_catalog.~)`, `COLLATE pg_catalog."C"`, `E'\\n'`). So the binary
runs, and its exit status and output are the verdict.

# The whole `\\d` family

`\\dp`, `\\dT` and `\\d <table>` used to be refused by name — they need the
`ARRAY(...)` constructor, correlated subqueries, `EXISTS`, `= ANY(...)` and
regex groups (`^(orders)$`). All of that now runs, along with `UNION ALL` over
a derived table (`\\dd`), `LATERAL` (`\\dP+`), `IS DISTINCT FROM` (`\\dconfig`)
and aggregates. So the suite drives EVERY psql 17 backslash-describe command
and asserts exit 0. Two exit 1 on a fresh Postgres too (`\\dx+`, `\\dRp+`:
psql's own "Did not find any ..." when a `+` listing is empty) and are
asserted as exactly that, not skipped.

What is still refused, by name: `GROUP BY` on the catalogue path.

Run: python3 tests/test_psql_introspection.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL  ") + name + (f"  — {detail}" if detail else ""))


def section(title):
    print(f"\n  ── {title} " + "─" * max(2, 52 - len(title)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_nedbd():
    for cand in (
        os.path.join(ROOT, "rust", "target", "release", "nedbd"),
        os.path.join(ROOT, "rust", "target", "debug", "nedbd"),
        shutil.which("nedbd-v2") or "",
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


def find_psql():
    """The real psql binary. `pgserver` ships one; otherwise use the system's."""
    try:
        import pgserver
        cand = os.path.join(
            os.path.dirname(pgserver.__file__), "pginstall", "bin", "psql")
        if os.path.exists(cand):
            return cand
    except ImportError:
        pass
    return shutil.which("psql")


BIN = find_nedbd()
if not BIN:
    print("SKIP: no nedbd binary — cargo build --release --bin nedbd -p nedb-engine")
    sys.exit(0)

PSQL = find_psql()
if not PSQL:
    # A self-skipping test is indistinguishable from a passing one, so CI sets
    # NEDB_REQUIRE_PSQL=1 and a missing binary becomes a failure there.
    if os.environ.get("NEDB_REQUIRE_PSQL") == "1":
        print("FAIL: NEDB_REQUIRE_PSQL=1 but no psql binary was found")
        sys.exit(1)
    print("SKIP: no psql binary found (pip install pgserver)")
    print("      set NEDB_REQUIRE_PSQL=1 to make this a failure instead")
    sys.exit(0)


def http(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def run_psql(port, arg):
    """Run one psql command. Returns (ok, stdout, stderr)."""
    r = subprocess.run(
        [PSQL, "-h", "127.0.0.1", "-p", str(port), "-d", "shop", "-U", "nedb",
         "--no-psqlrc", "-c", arg],
        capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0, r.stdout, r.stderr


def main():
    tmp = tempfile.mkdtemp(prefix="nedb-psql-")
    http_port, pg_port = free_port(), free_port()
    proc = subprocess.Popen(
        [BIN, "--data", os.path.join(tmp, "data"),
         "--port", str(http_port), "--pg-port", str(pg_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        env={**os.environ, "NEDBD_SWEEP_S": "0"},
    )
    try:
        for _ in range(60):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{http_port}/health", timeout=2).read()
                break
            except Exception:                                       # noqa: BLE001
                continue
        else:
            sys.exit("nedbd never came up")

        http(http_port, "POST", "/v1/databases", {"name": "shop"})
        for i, d in [("1", {"status": "paid", "total": 120}),
                     ("2", {"status": "open", "total": 40})]:
            http(http_port, "POST", "/v1/databases/shop/put",
                 {"coll": "orders", "id": i, "doc": d})
        http(http_port, "POST", "/v1/databases/shop/put",
             {"coll": "drivers", "id": "d1", "doc": {"name": "Bob", "active": True}})

        # ── THE two commands Oracle asked for ───────────────────────────────
        section(r"\dn and \dt — the acceptance criteria")

        ok, out, err = run_psql(pg_port, r"\dn")
        check(r"psql \dn exits 0", ok, err.strip()[:120])
        # `pg_catalog` is excluded by psql's `!~ '^pg_'`, `information_schema`
        # by its `<>` — leaving exactly the schema a user cares about.
        check(r"\dn lists public and nothing else",
              "public" in out and "pg_catalog" not in out, out.strip()[:160])
        check(r"\dn reports an owner (the pg_get_userbyid call resolved)",
              "nedb" in out, out.strip()[:160])

        ok, out, err = run_psql(pg_port, r"\dt")
        check(r"psql \dt exits 0", ok, err.strip()[:120])
        check(r"\dt lists BOTH collections as tables",
              "orders" in out and "drivers" in out, out.strip()[:200])
        # The nine-branch CASE resolved relkind 'r' to the word "table".
        check(r"\dt resolves the nine-branch CASE to 'table'",
              "table" in out, out.strip()[:200])
        check(r"\dt reports the schema from the LEFT JOIN",
              "public" in out, out.strip()[:200])
        # ORDER BY 1,2 — schema then name, so drivers precedes orders. If the
        # ordinals were read as the constants 1 and 2 every row would sort
        # equally and the listing would be silently unordered.
        check(r"\dt honours ORDER BY 1,2 (drivers before orders)",
              out.find("drivers") < out.find("orders"), out.strip()[:200])
        check(r"\dt does NOT leak internal bookkeeping",
              "__links__" not in out, out.strip()[:200])

        # ── everything else that now works for free ─────────────────────────
        section("the rest of psql's introspection")

        for cmd, why in [
            (r"\dv", "views (empty — NEDB has none)"),
            (r"\di", "indexes (empty — none are SQL-visible)"),
            (r"\dm", "materialized views (empty)"),
            (r"\dS", "system tables"),
            (r"\l", "databases"),
            (r"\du", "roles"),
            (r"\dg", "role groups"),
            (r"\df", "functions (empty)"),
            (r"\dx", "extensions (empty)"),
        ]:
            ok, out, err = run_psql(pg_port, cmd)
            check(f"psql {cmd} exits 0 — {why}", ok, err.strip()[:110])

        # ── the three that used to be refused ───────────────────────────────
        section(r"\dp, \dT and \d <table> — subqueries, ARRAY(), regex groups")

        ok, out, err = run_psql(pg_port, r"\d orders")
        check(r"psql \d orders exits 0 (regex ^(orders)$, three scalar subqueries)",
              ok, err.strip()[:150])
        check(r"\d orders lists the observed fields as columns",
              "status" in out and "total" in out, out.strip()[:200])
        check(r"\d orders types them the way the wire does (total is bigint)",
              "bigint" in out, out.strip()[:200])
        check(r"\d orders shows engine metadata columns too (_seq)",
              "_seq" in out, out.strip()[:200])
        # psql exits 1 for a relation it cannot find, on Postgres too — the
        # point is that the message is psql's own, not an engine ERROR.
        ok, out, err = run_psql(pg_port, r"\d nosuch")
        check(r"\d nosuch is psql's own 'Did not find', not an engine error",
              not ok and "Did not find any relation" in (out + err) and "ERROR" not in err,
              (out + err).strip()[:150])

        ok, out, err = run_psql(pg_port, r"\dp")
        check(r"psql \dp exits 0 (two ARRAY(SELECT ...) columns, = ANY)", ok, err.strip()[:150])
        check(r"\dp lists both tables with empty privileges",
              "orders" in out and "drivers" in out and "(2 rows)" in out, out.strip()[:200])

        ok, out, err = run_psql(pg_port, r"\dT")
        check(r"psql \dT exits 0 (correlated subquery + NOT EXISTS)", ok, err.strip()[:150])
        # Every type NEDB advertises lives in pg_catalog, which psql filters
        # out — so the honest listing is empty, not a fabricated user type.
        check(r"\dT lists no user-defined types", "(0 rows)" in out, out.strip()[:200])

        # ── the whole describe family ───────────────────────────────────────
        section("every psql 17 backslash-describe command exits 0")

        for cmd in [
            r"\d", r"\d+", r"\d+ orders", r"\dp orders", r"\dT+", r"\dd", r"\dD",
            r"\ds", r"\dE", r"\dc", r"\dC", r"\do", r"\dO", r"\dL", r"\dy", r"\dF",
            r"\dA", r"\db", r"\dl", r"\dP", r"\dP+", r"\dRp", r"\dRs", r"\drds",
            r"\dX", r"\dt+", r"\df+", r"\l+", r"\dn+", r"\du+", r"\dconfig", r"\z",
            r"\dS+", r"\dt public.*", r"\d *ord*", r"\dg+", r"\dD+", r"\des", r"\det",
            r"\deu", r"\dew", r"\dL+", r"\dm+", r"\di+", r"\ds+", r"\dv+", r"\dy+",
            r"\dX+", r"\dl+", r"\dA+", r"\dd+", r"\dFp", r"\dFd", r"\dFt",
            r"\dAc", r"\dAf", r"\dAo", r"\dAp",
        ]:
            ok, out, err = run_psql(pg_port, cmd)
            check(f"psql {cmd} exits 0", ok, err.strip()[:120])

        # psql itself exits 1 when a `+` listing finds nothing to describe —
        # identical against a fresh Postgres. Asserted as psql's message, so a
        # real error here could never hide behind "expected to fail".
        for cmd, msg in [(r"\dx+", "Did not find any extensions"),
                         (r"\dRp+", "Did not find any publications"),
                         (r"\dF+", "Did not find any text search configurations")]:
            ok, out, err = run_psql(pg_port, cmd)
            check(f"psql {cmd} is psql's own empty-listing exit, not an engine error",
                  not ok and msg in (out + err) and "ERROR" not in err,
                  (out + err).strip()[:150])

        # ── the SQL features themselves, through a driver ───────────────────
        try:
            import psycopg2
        except ImportError:                                          # pragma: no cover
            print("\n  …  psycopg2 not installed — skipping the feature checks")
            return

        section("the SQL features, asserted directly")

        conn = psycopg2.connect(host="127.0.0.1", port=pg_port, dbname="shop",
                                user="nedb", connect_timeout=10)
        conn.autocommit = True
        cur = conn.cursor()

        def q(sql):
            cur.execute(sql)
            return cur.fetchall()

        check("a LEFT JOIN across two catalogue relations",
              q("SELECT c.relname, n.nspname FROM pg_class c "
                "LEFT JOIN pg_namespace n ON n.oid = c.relnamespace "
                "ORDER BY 1")
              == [("drivers", "public"), ("orders", "public")])

        check("an unmatched LEFT JOIN row keeps NULLs rather than vanishing",
              q("SELECT c.relname, n.nspname FROM pg_class c "
                "LEFT JOIN pg_namespace n ON n.oid = 999999 ORDER BY 1")
              == [("drivers", None), ("orders", None)])

        check("an INNER JOIN drops what a LEFT JOIN keeps",
              q("SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = 999999") == [])

        check("a CASE expression, with an alias",
              q("SELECT CASE relkind WHEN 'r' THEN 'table' ELSE 'other' END "
                "AS \"Type\" FROM pg_class LIMIT 1") == [("table",)])

        check("a searched CASE",
              q("SELECT CASE WHEN relkind = 'r' THEN 1 ELSE 0 END FROM pg_class LIMIT 1")
              == [(1,)])

        check("scalar functions resolve",
              q("SELECT pg_get_userbyid(relowner), pg_table_is_visible(oid) "
                "FROM pg_class LIMIT 1") == [("nedb", True)])

        check("a table alias qualifies its own columns",
              q("SELECT c.relname FROM pg_class c WHERE c.relname = 'orders'")
              == [("orders",)])

        check("ORDER BY an ordinal sorts the projected column",
              q("SELECT relname FROM pg_class ORDER BY 1 DESC")
              == [("orders",), ("drivers",)])

        check("an IN list including the empty string",
              len(q("SELECT relname FROM pg_class WHERE relkind IN ('r','p','')")) == 2)

        check("DISTINCT dedupes",
              q("SELECT DISTINCT relkind FROM pg_class") == [("r",)])

        # The cross-over: a catalogue relation joined to a REAL collection.
        # This is what makes the SQL engine more than a catalogue special case
        # — the resolver reaches stored data as readily as synthesised rows.
        check("a catalogue relation can be JOINED to a user collection",
              q("SELECT c.relname, o.status FROM pg_class c "
                "CROSS JOIN orders o WHERE c.relname = 'orders' ORDER BY 2")
              == [("orders", "open"), ("orders", "paid")])

        # And the BOUNDARY, asserted rather than implied. A user table on its
        # own stays on the NQL path, which has the index pushdown, AS OF and
        # TRACE — and which has no table aliases. Routing every query through
        # the nested-loop engine to gain aliases would trade a real planner
        # for a cosmetic feature, so the limit is deliberate.
        check("a plain user-collection query still works (the NQL path)",
              sorted(q("SELECT status FROM orders")) == [("open",), ("paid",)])
        try:
            q("SELECT t.status FROM orders t")
            check("a user table ALIAS is refused, not silently mishandled",
                  False, "it answered — the NQL path gained aliases?")
        except Exception as e:                                      # noqa: BLE001
            check("a user table ALIAS is refused, not silently mishandled",
                  "unexpected" in str(e).lower(), str(e).strip()[:100])

        # The engine refuses rather than guessing — asserted through the wire,
        # because an error that never reaches the client is not a boundary.
        for sql, needle in [
            ("SELECT nosuchfn(1) FROM pg_class", "not implemented"),
            ("SELECT relname FROM pg_class ORDER BY 9", "out of range"),
            ("SELECT relname FROM pg_class WHERE relname ~ 'a{2}'", "interval"),
        ]:
            try:
                q(sql)
                check(f"refused: {sql[:44]}", False, "it answered")
            except Exception as e:                                  # noqa: BLE001
                check(f"refused: {sql[:44]}", needle in str(e), str(e).strip()[:110])

        # An unknown COLLECTION is empty, not an error — and that is correct
        # rather than sloppy. In a schemaless store you may `put` into a
        # collection without creating it, so "does not exist" and "is empty"
        # are the same observable state. Erroring would make a first write
        # into a new collection impossible to precede with a read.
        #
        # An unknown relation inside the SQL engine's own FROM list IS an
        # error, because psql never asks for a relation that does not exist —
        # there, it signals a bug. Two contexts, two right answers.
        check("an unknown user collection reads as EMPTY, not as an error",
              q("SELECT anything FROM nosuchcollection") == [],
              "schemaless: absent and empty are one state")

        cur.close()
        conn.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                           # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)
    print(r"psql \dn and \dt work against NEDB — real joins, real CASE,")
    print("real scalar functions, real ordinals. Not a pattern match.")


if __name__ == "__main__":
    main()
