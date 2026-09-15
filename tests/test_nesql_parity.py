#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""The two neSQL routers must agree on what a statement IS.

NEDB ships two engines — the Rust core and the Python reference — and both now
have to decide whether a statement is NQL or SQL. That is two implementations
of one decision, which is the shape of defect this codebase has been bitten by
before: required-vs-optional GROUP BY, and an ordering comparison against a
missing field that was true in Rust and false in Python. Both were found late.

A disagreement about a RESULT shows up as a wrong answer. A disagreement about
which LANGUAGE a statement is written in shows up as nothing at all: one engine
routes `SHOW TABLES` to SQL and answers, the other calls it unparseable, and
whichever one you happened to be talking to decides whether your query exists.

So this reads the keyword lists out of the Rust source and compares them to the
Python ones, token for token. Adding a statement head to one side fails this
test instead of splitting the engines.

Run: python3 tests/test_nesql_parity.py
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from nedb import nesql  # noqa: E402

RUST = os.path.join(ROOT, "rust", "nedb-v2", "src", "nesql.rs")

_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fail
    if cond:
        print("  ok    %s%s" % (name, ("  — " + detail) if detail else ""))
    else:
        _fail += 1
        print("  FAIL  %s  — %s" % (name, detail))


def rust_heads(const: str) -> list[str]:
    """Pull `pub const <const>: &[&str] = &[ ... ];` out of the Rust source."""
    src = open(RUST, encoding="utf8").read()
    m = re.search(
        r"pub const %s:\s*&\[&str\]\s*=\s*&\[(.*?)\];" % re.escape(const),
        src,
        re.S,
    )
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


def main() -> int:
    print("\n── the two routers share one vocabulary ──")

    if not os.path.exists(RUST):
        check("the Rust router is where this test expects it", False, RUST)
        return 1

    for const, py in (("NQL_HEADS", nesql.NQL_HEADS), ("SQL_HEADS", nesql.SQL_HEADS)):
        rs = rust_heads(const)
        check("%s is readable from the Rust source" % const, bool(rs),
              "found %d" % len(rs))
        # Compared as ORDERED lists, not sets: the Python module documents that
        # it keeps Rust's order so a human diff of the two files is readable.
        # A reordering is harmless at runtime and still worth knowing about.
        check("%s matches, token for token" % const, list(py) == rs,
              "rust=%s python=%s" % (rs, list(py)))

    print("\n── the vocabularies stay disjoint ──")
    overlap = set(nesql.NQL_HEADS) & set(nesql.SQL_HEADS)
    check("no word begins a statement in both halves", not overlap,
          "overlap=%s" % sorted(overlap) if overlap else "structural routing holds")

    print("\n── routing answers the same strings the Rust side answers ──")
    for stmt, want in (
        ("FROM orders", "nql"),
        ("from orders WHERE x = 1", "nql"),
        ("SELECT * FROM orders", "sql"),
        ("  explain select 1", "sql"),
        ("(SELECT 1) UNION (SELECT 2)", "sql"),
        ("INSERT INTO o VALUES (1)", "sql"),
    ):
        try:
            got = nesql.route(stmt)
        except nesql.DialectError as e:
            got = "REFUSED: %s" % str(e).splitlines()[0]
        check("route(%r)" % stmt, got == want, "got %s want %s" % (got, want))

    print("\n── a word in neither half is refused NAMING BOTH ──")
    try:
        nesql.route("GRANT ALL ON orders")
        check("GRANT is refused", False, "it was accepted")
    except nesql.DialectError as e:
        msg = str(e)
        check("GRANT is refused", True)
        check("the refusal names the offending word", "GRANT" in msg)
        check("...and the NQL vocabulary", "FROM" in msg)
        check("...and the SQL vocabulary", "SELECT" in msg)

    for empty in ("", "   \n "):
        try:
            nesql.route(empty)
            check("empty statement refused (%r)" % empty, False, "accepted")
        except nesql.DialectError:
            check("empty statement refused (%r)" % empty, True)

    print("\n" + "=" * 62)
    print("neSQL router parity: %s" % ("ALL PASSED" if not _fail else "%d FAILED" % _fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
