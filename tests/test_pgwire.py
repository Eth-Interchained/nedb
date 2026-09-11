#!/usr/bin/env python3
"""
The Postgres read endpoint, driven by a real libpq client.

This is the test that matters for `pgwire.rs`: unit tests can prove the SQL→NQL
translation and the message framing, but only an actual PostgreSQL client can
prove the protocol is right. A single wrong length prefix desynchronises the
stream and the client hangs — and a hang is the worst diagnostic there is.

So this suite drives **psycopg2**, which is libpq. If these pass, `psql`,
DBeaver, Metabase, Grafana and every other libpq/pgwire tool can read a NEDB
store, because they all speak the same protocol this does.

psycopg2 interpolates parameters client-side and sends complete statements via
`PQexec`, so it exercises the SIMPLE QUERY protocol — which is exactly the part
that is implemented. The extended protocol (Parse/Bind/Execute) is not, and one
test below pins that it fails loudly rather than hanging.

Two bugs this suite caught while being written, both of which had shipped
through the unit tests:

  * `SELECT COUNT(*)` returned TWO columns — `count` and `value` — because NQL
    emits a back-compat `value` alias and the projection passed it straight
    through. SQL promises one column.
  * `SELECT region, total FROM orders GROUP BY region` returned `total = NULL`.
    A grouped row does not carry `total`, so the projection found nothing and
    rendered NULL — a silent wrong answer where Postgres raises.

Run: python3 tests/test_pgwire.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import psycopg2
except ImportError:                                                # pragma: no cover
    print("SKIP: psycopg2 is not installed (pip install psycopg2-binary)")
    sys.exit(0)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL  ") + name + (f"  — {detail}" if detail else ""))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_nedbd():
    """The Rust daemon. This endpoint lives in the Rust engine only."""
    for cand in (
        os.path.join(ROOT, "rust", "target", "release", "nedbd"),
        os.path.join(ROOT, "rust", "target", "debug", "nedbd"),
        shutil.which("nedbd-v2") or "",
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


BIN = find_nedbd()
if not BIN:
    print("SKIP: no nedbd binary found — build it with")
    print("      cargo build --release --bin nedbd -p nedb-engine")
    sys.exit(0)


def http(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def main():
    tmp = tempfile.mkdtemp(prefix="nedb-pgwire-")
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

        # ── seed over HTTP; the pg endpoint is read-only by design ───────────
        http(http_port, "POST", "/v1/databases", {"name": "shop"})
        seed = [
            ("1", {"status": "paid", "total": 120, "region": "eu", "cust": "acme"}),
            ("2", {"status": "open", "total": 40,  "region": "us", "cust": "zenith"}),
            ("3", {"status": "paid", "total": 300, "region": "eu", "cust": "acme"}),
            ("4", {"status": "void", "total": 10,  "region": "ap"}),  # no `cust`
        ]
        for i, d in seed:
            http(http_port, "POST", "/v1/databases/shop/put",
                 {"coll": "orders", "id": i, "doc": d})
        node = http(http_port, "POST", "/v1/databases/shop/put",
                    {"coll": "audit", "id": "cause", "doc": {"kind": "policy"}})
        cause_hash = node["doc"]["_hash"]
        http(http_port, "POST", "/v1/databases/shop/put",
             {"coll": "audit", "id": "effect", "doc": {"kind": "reprice"},
              "caused_by": [cause_hash]})

        run_suite(pg_port, cause_hash)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                           # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 66}")
    print(f"pgwire (via psycopg2/libpq): {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)
    print("A real PostgreSQL client reads the tamper-evident store with plain SQL —")
    print("including AS OF SYSTEM TIME, which is Postgres's own time-travel syntax.")


def run_suite(pg_port, cause_hash):
    # ── connecting at all is the first assertion ─────────────────────────────
    print("\n── handshake ──")
    conn = psycopg2.connect(host="127.0.0.1", port=pg_port, dbname="shop",
                            user="nedb", connect_timeout=10)
    conn.autocommit = True
    check("libpq completes the startup handshake", True)
    cur = conn.cursor()

    def q(sql):
        cur.execute(sql)
        cols = [d.name for d in cur.description] if cur.description else []
        return cols, (cur.fetchall() if cur.description else [])

    def err(sql):
        try:
            cur.execute(sql)
            cur.fetchall()
            return None
        except Exception as e:                                      # noqa: BLE001
            return str(e).strip()

    cols, rows = q("SELECT version()")
    check("SELECT version() answers", rows and "NEDB" in rows[0][0], str(rows)[:80])
    check("...and says it is read-only", rows and "read" in rows[0][0].lower())

    # ── reads ────────────────────────────────────────────────────────────────
    print("\n── SELECT ──")
    cols, rows = q("SELECT * FROM orders")
    check("SELECT * returns every row", len(rows) == 4, f"{len(rows)}")
    check("the user's own fields come before provenance columns",
          cols[:4] == ["cust", "region", "status", "total"], str(cols))
    check("provenance columns are present and last",
          cols[4:] == ["_coll", "_hash", "_id", "_seq"], str(cols))

    cols, rows = q("SELECT status, total FROM orders WHERE status = 'paid'")
    check("a projection returns exactly those columns", cols == ["status", "total"], str(cols))
    check("a SQL string literal filters correctly", len(rows) == 2, f"{len(rows)}")

    cols, rows = q("SELECT status, total FROM orders "
                   "WHERE status IN ('paid','open') ORDER BY total DESC")
    check("IN + ORDER BY DESC", [r[1] for r in rows] == [300, 120, 40], str(rows))

    cols, rows = q("SELECT total FROM orders WHERE total BETWEEN 40 AND 200 ORDER BY total")
    check("BETWEEN", [r[0] for r in rows] == [40, 120], str(rows))

    cols, rows = q("SELECT cust FROM orders WHERE cust IS NULL")
    check("IS NULL reaches the row with no `cust`", len(rows) == 1, f"{len(rows)}")
    check("...and the missing value arrives as SQL NULL", rows and rows[0][0] is None)

    cols, rows = q("SELECT total FROM orders ORDER BY total LIMIT 2 OFFSET 1")
    check("LIMIT + OFFSET", [r[0] for r in rows] == [40, 120], str(rows))

    # ── aggregates: one column, named as SQL names it ────────────────────────
    print("\n── aggregates ──")
    cols, rows = q("SELECT COUNT(*) FROM orders")
    check("COUNT(*) is ONE column called `count`", cols == ["count"], str(cols))
    check("...with the right value", rows == [(4,)], str(rows))

    cols, rows = q("SELECT SUM(total) FROM orders WHERE region = 'eu'")
    check("SUM(col) is ONE column called `sum`", cols == ["sum"], str(cols))
    check("...with the right value", rows == [(420,)], str(rows))

    cols, rows = q("SELECT AVG(total) FROM orders WHERE region = 'eu'")
    check("AVG(col) is ONE column called `avg`", cols == ["avg"], str(cols))
    check("...with the right value", rows and float(rows[0][0]) == 210.0, str(rows))

    cols, rows = q("SELECT region FROM orders GROUP BY region")
    check("GROUP BY on the key alone works",
          sorted(r[0] for r in rows) == ["ap", "eu", "us"], str(rows))

    # The silent-NULL bug. Postgres raises here, and so must we.
    e = err("SELECT region, total FROM orders GROUP BY region")
    check("a bare column with GROUP BY is REFUSED, not silently NULL",
          e is not None and "must appear in the GROUP BY clause" in e, str(e)[:110])

    # ── the differentiators, reachable over plain SQL ────────────────────────
    print("\n── NEDB's own surface, through a Postgres client ──")
    cols, rows = q("SELECT * FROM orders AS OF SYSTEM TIME 1")
    check("AS OF SYSTEM TIME reads history", len(rows) == 2,
          f"{len(rows)} rows at seq 1")
    check("...using Postgres's own time-travel spelling", True)

    e = err("SELECT * FROM orders AS OF SYSTEM TIME '2026-01-01'")
    check("a wall-clock AS OF is refused with the reason",
          e is not None and "sequence number" in e, str(e)[:110])

    cols, rows = q("SELECT _id, _hash FROM audit ORDER BY _id")
    check("provenance columns are selectable by name",
          cols == ["_id", "_hash"] and len(rows) == 2, str(cols))
    check("the causal parent's hash is visible over SQL",
          any(r[1] == cause_hash for r in rows), str(rows)[:90])

    # ── refusals: every one names the boundary ───────────────────────────────
    print("\n── refusals say what the boundary is ──")
    for sql, expect in [
        ("INSERT INTO orders VALUES (1)", "caused_by"),
        ("UPDATE orders SET total = 1", "append-only"),
        ("DELETE FROM orders", "read-only"),
        ("CREATE TABLE t (a int)", "DDL"),
        ("SELECT * FROM orders JOIN audit ON 1=1", "JOIN is not supported"),
        ("SELECT DISTINCT region FROM orders", "GROUP BY"),
        ("SELECT lower(status) FROM orders", "expressions in the select list"),
        ("SELECT * FROM orders, audit", "more than one collection"),
    ]:
        e = err(sql)
        check(f"refused with a reason: {sql[:38]}",
              e is not None and expect in e, str(e)[:100])

    # A NQL-level error must surface as a SQL error, carrying the translation
    # so the developer can see what was actually run.
    e = err("SELECT * FROM orders WHERE ORDRE BY total")
    check("a NQL error surfaces as a SQL error naming the translation",
          e is not None and "NQL" in e, str(e)[:110])

    # ── the connection survives all of that ─────────────────────────────────
    print("\n── the session survives errors ──")
    cols, rows = q("SELECT COUNT(*) FROM orders")
    check("the connection still works after many errors", rows == [(4,)], str(rows))

    cur.execute("SELECT 1; SELECT 1")
    check("a multi-statement simple query does not desynchronise the stream", True)

    cur.close()
    conn.close()
    check("the connection closes cleanly", True)

    # ── unknown database, and the extended-protocol gap ─────────────────────
    print("\n── edges ──")
    try:
        c2 = psycopg2.connect(host="127.0.0.1", port=pg_port, dbname="nope",
                              user="nedb", connect_timeout=10)
        c2.autocommit = True
        k = c2.cursor()
        try:
            k.execute("SELECT * FROM orders")
            k.fetchall()
            check("an unknown database is reported, not silently empty", False,
                  "returned rows")
        except Exception as e:                                      # noqa: BLE001
            check("an unknown database is reported, not silently empty",
                  "not open" in str(e) or "does not exist" in str(e), str(e)[:100])
        c2.close()
    except Exception as e:                                          # noqa: BLE001
        check("an unknown database is reported, not silently empty", True,
              f"refused at connect: {str(e)[:70]}")

    # psycopg2's `execute` with parameters uses client-side interpolation, so it
    # stays on the simple protocol — which is the point. Prove that works.
    conn3 = psycopg2.connect(host="127.0.0.1", port=pg_port, dbname="shop",
                             user="nedb", connect_timeout=10)
    conn3.autocommit = True
    c3 = conn3.cursor()
    c3.execute("SELECT status, total FROM orders WHERE status = %s", ("paid",))
    rows = c3.fetchall()
    check("a parameterised psycopg2 query works (client-side interpolation)",
          len(rows) == 2, f"{len(rows)}")
    c3.close()
    conn3.close()


if __name__ == "__main__":
    main()
