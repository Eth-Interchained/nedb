#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""`AS OF` must mean the same thing on all four surfaces.

# The drift this caught

NEDB has two engines and each speaks two halves of neSQL, so one temporal
clause has FOUR implementations. Measured before this test existed:

    surface       AS OF <seq>  AS OF <dt>  AS OF SYSTEM TIME <seq>  <dt>   projection
    Rust SQL      yes          yes         yes                      yes    correct
    Python SQL    yes          NO          NO                       NO     LOST
    Rust NQL      yes          NO          NO                       NO     n/a
    Python NQL    yes          yes         NO                       NO     n/a

Every disagreement was silent. A query written against nedbd raised a
SyntaxError against the reference engine, and `SELECT who` answered with every
field on one engine and one field on the other -- same dialect, same store,
different answers, and nothing anywhere said which was right.

# What is asserted, and what is deliberately NOT

The two SQL halves must agree exactly, and the two NQL halves must agree
exactly. `AS OF SYSTEM TIME` is SQL-only: both NQL halves reject it, on
purpose, because it is the SQL spelling of the clause. That rejection is
asserted too -- a silent acceptance on one side would be the same class of
drift as a silent rejection.

Projection is asserted on the SQL halves only. NQL has no select list; it
returns documents, which is why the SQL layer is the one that projects on both
engines.

Run: python3 tests/test_asof_parity.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

_fail = 0


def check(name: str, cond: bool, detail: str = "", why: str = "") -> None:
    global _fail
    if cond:
        print("  ok    %s%s" % (name, ("  — " + detail) if detail else ""))
    else:
        _fail += 1
        print("  FAIL  %s  — %s" % (name, why or detail))


# ── the python side, in process ───────────────────────────────────────────────

def python_results():
    from nedb import NEDB
    from nedb.sql import sql_exec

    db = NEDB()
    db.put("orders", "1", {"who": "acme", "total": 100})
    db.put("orders", "2", {"who": "globex", "total": 200})
    at = db.seq
    db.put("orders", "1", {"who": "changed", "total": 999})

    out = {}
    for spelling in ("AS OF", "AS OF SYSTEM TIME"):
        for target, kind in ((str(at), "seq"), ('"2099-01-01"', "datetime")):
            sql = "SELECT who FROM orders %s %s" % (spelling, target)
            out[("sql", spelling, kind)] = _run(lambda: sql_exec(db, sql))
            nql = "FROM orders %s %s" % (spelling, target)
            out[("nql", spelling, kind)] = _run(lambda: db.query(nql))
    return out


def _run(fn):
    try:
        rows = fn()
        if not isinstance(rows, list):
            rows = [rows]
        cols = sorted({k for r in rows if isinstance(r, dict) for k in r
                       if not k.startswith("_")})
        return ("ok", len(rows), cols)
    except Exception:
        return ("err", 0, [])


# ── the rust side, through a generated integration test ──────────────────────

RUST_PROBE = r'''
use nedb_engine::db::Db;
use nedb_engine::pgwire::execute_sql;
use serde_json::json;
use std::sync::atomic::Ordering;
use std::sync::Arc;
fn cols(rows: &[serde_json::Value]) -> Vec<String> {
    let mut c: Vec<String> = rows.first().and_then(|r| r.as_object())
        .map(|o| o.keys().filter(|k| !k.starts_with('_')).cloned().collect())
        .unwrap_or_default();
    c.sort(); c
}
#[test]
fn probe() {
    let dir = tempfile::tempdir().unwrap();
    let db = Arc::new(Db::open(dir.path(), None).unwrap());
    db.put("orders","1",json!({"who":"acme","total":100}),vec![],None,None).unwrap();
    db.put("orders","2",json!({"who":"globex","total":200}),vec![],None,None).unwrap();
    let at = db.seq.load(Ordering::SeqCst).saturating_sub(1);
    std::thread::sleep(std::time::Duration::from_millis(50));
    db.put("orders","1",json!({"who":"changed","total":999}),vec![],None,None).unwrap();
    for sp in ["AS OF", "AS OF SYSTEM TIME"] {
        for (t, k) in [(at.to_string(), "seq"), ("'2099-01-01'".to_string(), "datetime")] {
            let q = format!("SELECT who FROM orders {} {}", sp, t);
            match execute_sql(&db, &q, true) {
                Ok(x) => println!("PARITY sql|{}|{}|ok|{}|{}", sp, k, x.rows.len(), cols(&x.rows).join(",")),
                Err(_) => println!("PARITY sql|{}|{}|err|0|", sp, k),
            }
        }
        for (t, k) in [(at.to_string(), "seq"), ("\"2099-01-01\"".to_string(), "datetime")] {
            let q = format!("FROM orders {} {}", sp, t);
            match nedb_engine::nql::query(&db, &q) {
                Ok((r, _)) => println!("PARITY nql|{}|{}|ok|{}|{}", sp, k, r.len(), cols(&r).join(",")),
                Err(_) => println!("PARITY nql|{}|{}|err|0|", sp, k),
            }
        }
    }
}
'''


def rust_results():
    """Run the probe as a real integration test, or return None if cargo can't."""
    path = os.path.join(ROOT, "rust", "nedb-v2", "tests", "_asof_parity_probe.rs")
    with open(path, "w", encoding="utf8") as fh:
        fh.write(RUST_PROBE)
    # rustup installs to ~/.cargo/bin and does not touch the PATH a
    # non-login subprocess inherits. The first version of this looked for a
    # bare `cargo`, failed to find the one that was definitely installed, and
    # reported "cargo is not installed" -- so it measured one engine and said
    # the parity check passed.
    cargo = os.environ.get("CARGO") or "cargo"
    env = dict(os.environ, CARGO_INCREMENTAL="0")
    env["PATH"] = os.pathsep.join([
        os.path.expanduser("~/.cargo/bin"),
        env.get("PATH", ""),
    ])
    try:
        p = subprocess.run(
            [cargo, "test", "-q", "-p", "nedb-engine",
             "--test", "_asof_parity_probe", "--", "--nocapture"],
            cwd=os.path.join(ROOT, "rust"), capture_output=True, text=True,
            env=env, timeout=3600,
        )
    except FileNotFoundError:
        print("  SKIP  cargo not found on PATH or via $CARGO; "
              "the Rust half was NOT measured")
        return None
    finally:
        os.unlink(path)

    out = {}
    for line in p.stdout.splitlines():
        if not line.startswith("PARITY "):
            continue
        half, spelling, kind, status, n, cols = line[len("PARITY "):].split("|")
        out[(half, spelling, kind)] = (
            status, int(n), sorted(c for c in cols.split(",") if c)
        )
    if not out:
        print("  SKIP  the Rust probe produced no output; it was NOT measured")
        print("        stderr: %s" % p.stderr.strip().splitlines()[-1:] or "")
        return None
    return out


def main() -> int:
    print("\n── measuring both engines ──")
    py = python_results()
    rs = rust_results()
    print("  python surfaces measured: %d" % len(py))
    if rs is not None:
        print("  rust surfaces measured  : %d" % len(rs))

    print("\n── the two SQL halves must agree, form for form ──")
    for spelling in ("AS OF", "AS OF SYSTEM TIME"):
        for kind in ("seq", "datetime"):
            k = ("sql", spelling, kind)
            got = py[k]
            check("python SQL accepts %s <%s>" % (spelling, kind), got[0] == "ok",
                  why="the Rust SQL evaluator accepts it; a query that runs "
                      "against nedbd must not raise against the reference engine")
            if got[0] == "ok":
                check("python SQL projects %s <%s>" % (spelling, kind),
                      got[2] == ["who"],
                      detail="cols=%s" % got[2],
                      why="SELECT who returned %s — the select list was "
                          "discarded, so the answer carries fields the query "
                          "did not ask for" % got[2])
            if rs is not None:
                r = rs[k]
                check("engines agree on SQL %s <%s>" % (spelling, kind),
                      (r[0], r[1], r[2]) == (got[0], got[1], got[2]),
                      detail="both %s rows=%d cols=%s" % (got[0], got[1], got[2]),
                      why="rust=%s python=%s" % (r, got))

    print("\n── the two NQL halves must agree, form for form ──")
    for spelling in ("AS OF", "AS OF SYSTEM TIME"):
        for kind in ("seq", "datetime"):
            k = ("nql", spelling, kind)
            got = py[k]
            # SYSTEM TIME is the SQL spelling. Both NQL halves reject it, and
            # that is asserted rather than assumed: a one-sided acceptance is
            # the same drift as a one-sided rejection.
            want_ok = spelling == "AS OF"
            check("python NQL %s %s <%s>" % (
                      "accepts" if want_ok else "rejects", spelling, kind),
                  (got[0] == "ok") == want_ok,
                  why="got %s" % got[0])
            if rs is not None:
                r = rs[k]
                check("engines agree on NQL %s <%s>" % (spelling, kind),
                      (r[0], r[1]) == (got[0], got[1]),
                      detail="both %s rows=%d" % (got[0], got[1]),
                      why="rust=%s python=%s — one dialect, two "
                          "implementations, disagreeing" % (r, got))

    print("\n" + "=" * 66)
    if rs is None:
        # Exit NON-ZERO. A parity test that measured one of two engines has
        # not checked parity, and reporting success for it is the same lie the
        # drift itself told. An engine-less environment opts out explicitly
        # with NEDB_PARITY_ALLOW_PYTHON_ONLY=1 rather than by accident.
        allowed = os.environ.get("NEDB_PARITY_ALLOW_PYTHON_ONLY") == "1"
        print("AS OF parity: RUST NOT MEASURED — %s"
              % ("python side passed, opt-out honoured" if allowed and not _fail
                 else "%d python failures" % _fail if _fail
                 else "refusing to report parity on one engine"))
        if allowed and not _fail:
            return 0
        return 1
    print("AS OF parity: %s" % ("ALL PASSED" if not _fail else "%d FAILED" % _fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
