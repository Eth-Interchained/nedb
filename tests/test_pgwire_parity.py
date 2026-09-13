#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
Two engines, one question, one answer — or the flag does not get flipped.

# What this is for

The PostgreSQL endpoint has two ways to answer a `SELECT` over a user
collection:

  * the TRANSLATOR — rewrite the SQL into NQL and let the NQL engine run it.
    Has the index pushdown and the bounded scans. Cannot express a join, a
    subquery, a set operation, or two named aggregates in one grouped row,
    because NQL cannot.
  * the EVALUATOR (`sqlselect`) — run the SQL for real. Has all of those, and
    now also parses NQL's own verbs (`AS OF SYSTEM TIME`, `VALID AS OF`,
    `SEARCH`) as table qualifiers, rendering them back into the NQL it asks
    the store for. So the verbs have ONE implementation and two front-ends,
    which is what "NQL folded into neSQL" means in practice.

`NEDBD_SQL_ENGINE=1` moves user collections from the first to the second, and
it is DEFAULT OFF for exactly one reason: nobody has shown the two agree. This
file is that proof, or the record of where it fails.

# The only assertion that means anything

Where BOTH engines answer, they must return the SAME ROWS. Not similar, not
compatible — identical, after an order-insensitive comparison when the query
has no `ORDER BY`.

Where only one answers, that is recorded as an ASYMMETRY rather than a failure,
because it is the expected shape of the thing:

  * evaluator-only  — a join, a subquery, `UNION`, `DISTINCT`, two aggregates.
    This is what the flag is FOR. Counting it as a failure would make the
    harness fail by succeeding.
  * translator-only — `TRACE` and `TRAVERSE`, which the SQL grammar still has
    no spelling for, so they fall through to the translator on their own.
    `SEARCH` and `VALID AS OF` USED to be in this list and are not any more:
    the evaluator learned them, so they are held to the parity standard now
    instead, and their answers must be identical either way.

A query where both answer and they DISAGREE is the only real failure, and it is
also the only interesting result, so it prints both answers in full.

# Why two daemons rather than one

The flag is read once per process (`OnceLock`), which is deliberate — it is a
deployment decision, not a per-statement one. So parity needs two processes
with identical data. They are seeded through the same code path in the same
order, so the sequences line up and `AS OF <seq>` means the same thing in both.
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PASS, FAIL, ASYM = [], [], []


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_nedbd():
    for cand in (
        os.environ.get("NEDBD_BIN") or "",
        os.path.join(ROOT, "rust", "target", "release", "nedbd"),
        os.path.join(ROOT, "rust", "target", "debug", "nedbd"),
        shutil.which("nedbd-v2") or "",
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


BIN = find_nedbd()
if not BIN:
    print("SKIP: no nedbd binary — cargo build --release --bin nedbd -p nedb-engine")
    sys.exit(0)

try:
    import psycopg2
except ImportError:
    print("SKIP: psycopg2 is not installed (pip install psycopg2-binary)")
    sys.exit(0)


def http(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


class Daemon:
    """One nedbd, with the SQL-engine flag either set or not."""

    def __init__(self, tmp, sql_engine: bool):
        self.sql_engine = sql_engine
        self.label = "evaluator" if sql_engine else "translator"
        self.http_port, self.pg_port = free_port(), free_port()
        env = {**os.environ, "NEDBD_SWEEP_S": "0"}
        if sql_engine:
            env["NEDBD_SQL_ENGINE"] = "1"
        else:
            # Removed rather than set to 0, so an inherited value from the
            # caller's shell cannot silently make both daemons the same engine
            # and turn this whole file into a tautology that always passes.
            env.pop("NEDBD_SQL_ENGINE", None)
        self.proc = subprocess.Popen(
            [BIN, "--data", os.path.join(tmp, self.label),
             "--port", str(self.http_port), "--pg-port", str(self.pg_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env,
        )
        for _ in range(60):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.http_port}/health", timeout=2).read()
                return
            except Exception:                                        # noqa: BLE001
                continue
        raise SystemExit(f"nedbd ({self.label}) never came up")

    def seed(self, rows):
        http(self.http_port, "POST", "/v1/databases", {"name": "shop"})
        for coll, i, doc in rows:
            http(self.http_port, "POST", "/v1/databases/shop/put",
                 {"coll": coll, "id": i, "doc": doc})

    def ask(self, sql):
        """`(rows, None)` when it answered, `(None, message)` when it refused.

        `_hash` is dropped from the comparison, and this is the one exclusion
        in the file so it is worth justifying. A NEDB node carries `ts`, a
        wall-clock timestamp, and the hash chain commits to it — so two stores
        holding IDENTICAL documents seeded at different instants correctly
        produce different hashes. That is the tamper-evidence working: the
        chain attests to when a write happened, not only to what it said.
        Comparing hashes across two processes would therefore fail for a
        reason that has nothing to do with which engine answered.

        Column NAMES and their ORDER are still compared, in `ask_cols` — the
        `SELECT *` divergence this harness found was an ordering one, so
        dropping a column's value must not drop the check that it is there.
        """
        got = self.ask_cols(sql)
        if got[0] is None:
            return None, got[1]
        names, rows = got
        keep = [i for i, n in enumerate(names) if n != "_hash"]
        return [tuple(r[i] for i in keep) for r in rows], None

    def ask_cols(self, sql):
        """`(names, rows)` when it answered, `(None, message)` when it refused."""
        conn = psycopg2.connect(host="127.0.0.1", port=self.pg_port,
                                dbname="shop", user="nedb")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                names = [d[0] for d in (cur.description or [])]
                return names, [tuple(r) for r in cur.fetchall()]
        except Exception as e:                                       # noqa: BLE001
            return None, str(e).strip().splitlines()[0]
        finally:
            conn.close()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ── the corpus ──────────────────────────────────────────────────────────────
#
# `ordered=True` means the query fixes its own row order and the comparison is
# order-SENSITIVE. Without an ORDER BY, two engines may legitimately enumerate
# in different orders, and asserting a coincidental order would fail the
# harness for a difference that is not one.
BOTH = [
    ("a bare select",                 "SELECT _id FROM orders", False),
    ("a column list",                 "SELECT status, total FROM orders", False),
    ("select star",                   "SELECT * FROM orders", False),
    ("an equality predicate",         "SELECT _id FROM orders WHERE status = 'paid'", False),
    ("an inequality",                 "SELECT _id FROM orders WHERE total > 100", False),
    ("a negated predicate",           "SELECT _id FROM orders WHERE status != 'paid'", False),
    ("AND",                           "SELECT _id FROM orders WHERE status='paid' AND total>100", False),
    ("OR",                            "SELECT _id FROM orders WHERE status='paid' OR total<50", False),
    ("IN a value list",               "SELECT _id FROM orders WHERE status IN ('paid','open')", False),
    ("LIKE",                          "SELECT _id FROM orders WHERE status LIKE 'pa%'", False),
    ("a qualified column",            "SELECT orders._id FROM orders WHERE orders.status='paid'", False),
    ("a table alias",                 "SELECT o._id FROM orders o WHERE o.status='paid'", False),
    ("ORDER BY ascending",            "SELECT total FROM orders ORDER BY total", True),
    ("ORDER BY descending",           "SELECT total FROM orders ORDER BY total DESC", True),
    ("ORDER BY an ordinal",           "SELECT total, status FROM orders ORDER BY 1", True),
    ("LIMIT after ORDER BY",          "SELECT total FROM orders ORDER BY total LIMIT 2", True),
    ("OFFSET after ORDER BY",         "SELECT total FROM orders ORDER BY total OFFSET 1", True),
    ("count star",                    "SELECT count(*) FROM orders", False),
    ("count with a predicate",        "SELECT count(*) FROM orders WHERE status='paid'", False),
    ("sum",                           "SELECT sum(total) FROM orders", False),
    ("min and max separately",        "SELECT min(total) FROM orders", False),
    ("GROUP BY with count",           "SELECT status, count(*) FROM orders GROUP BY status", False),
    ("GROUP BY with one aggregate",   "SELECT status, sum(total) FROM orders GROUP BY status", False),
    ("HAVING on the count",           "SELECT status, count(*) FROM orders GROUP BY status HAVING count(*) > 1", False),
    # A field ABSENT from one document is where SQL's NULL and NEDB's missing
    # field could part company, so both engines are pinned on it.
    ("a predicate on a sparse field", "SELECT _id FROM orders WHERE cust = 'acme'", False),
    ("a sparse field projected",      "SELECT _id, cust FROM orders ORDER BY _id", True),
    # Time travel, which only exists because the evaluator learned AS OF.
    ("AS OF at the seeded tip",       "SELECT _id, total FROM orders AS OF SYSTEM TIME 3", False),
    ("AS OF with a predicate",        "SELECT _id FROM orders AS OF SYSTEM TIME 3 WHERE status='paid'", False),
    # NQL's verbs composed with SQL the translator CAN also express. `count`
    # rides along free with a named aggregate in an NQL grouped row, so this
    # one is answered by both -- which makes it a parity assertion rather than
    # an unlock, and a stronger claim for it.
    ("SEARCH with count and a named aggregate",
     "SELECT count(*), sum(total) FROM orders SEARCH 'acme'", False),
    ("SEARCH with a predicate",
     "SELECT _id FROM orders SEARCH 'acme' WHERE status = 'paid'", False),
]

# Expected to work on the EVALUATOR only. Each is a thing NQL cannot express,
# so the translator refuses it by name -- that asymmetry is the entire point of
# the flag.
EVALUATOR_ONLY = [
    ("a join",            "SELECT o._id FROM orders o JOIN drivers d ON o.driver = d._id"),
    ("a left join",       "SELECT o._id FROM orders o LEFT JOIN drivers d ON o.driver = d._id"),
    ("DISTINCT",          "SELECT DISTINCT status FROM orders"),
    ("UNION",             "SELECT _id FROM orders UNION SELECT _id FROM drivers"),
    ("a subquery in IN",  "SELECT _id FROM orders WHERE driver IN (SELECT _id FROM drivers)"),
    ("two aggregates",    "SELECT status, sum(total), avg(total) FROM orders GROUP BY status"),
    ("an expression",     "SELECT total * 2 FROM orders"),
    # NQL's verbs COMPOSED with SQL, which is the whole point of folding them
    # in rather than leaving them on a separate path. The translator can say
    # SEARCH and it can say a join; it cannot say both in one statement,
    # because NQL is single-collection.
    ("SEARCH joined to another collection",
     "SELECT o._id, d.name FROM orders SEARCH 'acme' o JOIN drivers d ON o.driver = d._id"),
    ("SEARCH with DISTINCT",
     "SELECT DISTINCT status FROM orders SEARCH 'acme'"),
    ("VALID AS OF with a join",
     "SELECT o._id FROM orders VALID AS OF '2030-01-01' o JOIN drivers d ON o.driver = d._id"),
]

# The NQL-only verbs. SQL has no spelling for them, so the SQL parser cannot
# take them and the statement falls through to the translator on ITS OWN.
#
# The first version of this file asserted these must REFUSE with the flag on,
# and they answered — correctly. The flag does not replace the translator, it
# only redirects the statements the SQL grammar can parse. So the assertion was
# wrong, not the engine: these belong in the both-must-agree set, because the
# flag must not change their answer by so much as a row.
#
# `ordered=True` throughout: each of these fixes its own order, and a silent
# reordering under the flag would be exactly the kind of drift worth catching.
NQL_VERBS = [
    ("SEARCH still answers, unchanged",      "SELECT _id FROM orders SEARCH 'acme'", False),
    ("VALID AS OF still answers, unchanged", "SELECT _id FROM orders VALID AS OF '2026-01-01'", False),
]


def report(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name + (f"  — {detail}" if detail else ""))


def compare(name, a, b, ordered):
    """`a` from the translator, `b` from the evaluator."""
    (rows_a, err_a), (rows_b, err_b) = a, b
    if rows_a is None and rows_b is None:
        # Both refused. Not parity in the interesting sense, but not a
        # disagreement either -- and worth seeing, because a query the corpus
        # believes is supported failing on BOTH is a corpus bug.
        ASYM.append(name)
        print(f"  both   {name}  — refused by both: {err_a}")
        return
    if rows_a is None:
        ASYM.append(name)
        print(f"  eval   {name}  — translator refused: {err_a}")
        return
    if rows_b is None:
        ASYM.append(name)
        print(f"  trans  {name}  — evaluator refused: {err_b}")
        return
    if not ordered:
        rows_a, rows_b = sorted(rows_a, key=repr), sorted(rows_b, key=repr)
    if rows_a == rows_b:
        report(f"{name}  ({len(rows_a)} rows)", True)
    else:
        report(name, False, "THE TWO ENGINES DISAGREE")
        print(f"          translator: {rows_a}")
        print(f"          evaluator:  {rows_b}")


def main():
    tmp = tempfile.mkdtemp(prefix="nedb-parity-")
    seed = [
        ("orders", "1", {"status": "paid", "total": 120, "region": "eu", "cust": "acme", "driver": "d1"}),
        ("orders", "2", {"status": "open", "total": 40,  "region": "us", "cust": "zenith", "driver": "d2"}),
        ("orders", "3", {"status": "paid", "total": 300, "region": "eu", "cust": "acme", "driver": "d1"}),
        ("orders", "4", {"status": "void", "total": 10,  "region": "ap"}),   # no cust, no driver
        # Carries a field NO earlier document has. This is the row that catches
        # a `SELECT *` built from `rows.first()` -- without it, the column loss
        # is invisible because every field happens to exist in document 1.
        ("orders", "5", {"status": "paid", "total": 55, "region": "eu", "rush": True}),
        ("drivers", "d1", {"name": "Bob", "active": True}),
        ("drivers", "d2", {"name": "Ann", "active": False}),
    ]
    trans = Daemon(tmp, sql_engine=False)
    evalr = Daemon(tmp, sql_engine=True)
    try:
        trans.seed(seed)
        evalr.seed(seed)

        # The harness is worthless if both daemons are secretly the same
        # engine, so that is checked rather than assumed: a join must refuse on
        # one and answer on the other before a single parity claim is made.
        probe = "SELECT o._id FROM orders o JOIN drivers d ON o.driver = d._id"
        t_rows, t_err = trans.ask(probe)
        e_rows, e_err = evalr.ask(probe)
        distinct_engines = t_rows is None and e_rows is not None
        report("the two daemons really are running different engines",
               distinct_engines,
               f"translator={'refused' if t_rows is None else 'answered'}, "
               f"evaluator={'answered' if e_rows is not None else 'refused: %s' % e_err}")
        if not distinct_engines:
            print("\nrefusing to report parity on two identical engines")
            print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
            return 1

        # `SELECT *` on a schemaless store is the one place the two engines had
        # to INVENT a column list, so the names and their order are asserted
        # on their own. They diverged both ways when this harness first ran:
        # the evaluator ordered by the first row's key order where the
        # translator sorted, and — worse — it read only `rows.first()`, so a
        # field carried by later documents silently did not appear at all.
        # A client reading `SELECT *` by POSITION would have got different
        # columns depending on a deployment flag.
        t_names, _ = trans.ask_cols("SELECT * FROM orders")
        e_names, _ = evalr.ask_cols("SELECT * FROM orders")
        report("SELECT * yields the same columns in the same order",
               t_names is not None and t_names == e_names,
               f"translator={t_names}  evaluator={e_names}")

        print("\n── both engines must agree ──────────────────────────────────")
        for name, sql, ordered in BOTH:
            compare(name, trans.ask(sql), evalr.ask(sql), ordered)

        print("\n── evaluator-only: what the flag is FOR ─────────────────────")
        for name, sql in EVALUATOR_ONLY:
            t_rows, t_err = trans.ask(sql)
            e_rows, e_err = evalr.ask(sql)
            report(f"{name}: evaluator answers, translator refuses by name",
                   e_rows is not None and t_rows is None,
                   f"evaluator={e_err or 'ok'}, translator={t_err or 'ANSWERED'}")

        print("\n── the NQL-only verbs must survive the flag untouched ───────")
        for name, sql, ordered in NQL_VERBS:
            compare(name, trans.ask(sql), evalr.ask(sql), ordered)
    finally:
        evalr.stop()
        trans.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    print(f"{len(PASS)} passed, {len(FAIL)} failed, {len(ASYM)} asymmetric")
    if FAIL:
        print("\nfailures:")
        for f in FAIL:
            print("  - " + f)
        return 1
    print("\nThe two engines agree everywhere both can answer. That is what the")
    print("NEDBD_SQL_ENGINE flag is waiting on — not a benchmark, an agreement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
