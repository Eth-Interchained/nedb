# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""neSQL dialect routing for the Python reference engine.

NEDB speaks NQL **and** SQL. This module decides which one a statement is
written in, so every Python-side door can accept either — the HTTP `/query`
endpoint, `/subscribe`, and any caller holding a `NEDB` instance.

# This is a MIRROR, and mirrors drift

The authoritative router is `rust/nedb-v2/src/nesql.rs`. This file exists
because the Python reference engine cannot call it, not because a second
opinion is wanted — and a second copy of "which language is this" is exactly
the kind of duplication that has silently diverged in this codebase before
(required-vs-optional GROUP BY, the ordering-comparison-against-missing-field
split).

So the vocabularies are asserted identical by
`tests/test_nesql_parity.py`, which reads the Rust source and compares the
keyword lists token for token. If someone adds a statement head to one side,
that test fails rather than the two engines quietly disagreeing about what a
statement MEANS — which is worse than disagreeing about a result, because
nothing looks broken when it happens.

# Routing is structural, not a guess

    NQL  begins with FROM
    SQL  begins with SELECT INSERT UPDATE DELETE EXPLAIN WITH SHOW SET
                     VALUES TABLE BEGIN COMMIT ROLLBACK

PostgreSQL has no statement form that begins with `FROM`, so the leading
keyword PARTITIONS the two vocabularies rather than hinting at them. A first
word in neither is refused NAMING BOTH — never handed to whichever parser
seems likelier, because "seems likelier" is the guess this rule forbids.
"""

from __future__ import annotations

#: Kept in the same order as the Rust lists so a diff between the two is
#: readable by eye as well as by the parity test.
NQL_HEADS = ("FROM",)
SQL_HEADS = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "EXPLAIN", "WITH", "SHOW", "SET",
    "VALUES", "TABLE", "BEGIN", "COMMIT", "ROLLBACK",
)


class DialectError(ValueError):
    """A statement that begins in neither half of neSQL."""


def first_word(statement: str) -> str | None:
    """The leading keyword, uppercased, or None for an empty statement."""
    parts = statement.split()
    if not parts:
        return None
    # A statement may open with a parenthesis — `(SELECT …) UNION …` — and may
    # carry a trailing semicolon on the first token when it is a bare verb.
    word = parts[0].lstrip("(").rstrip(";").upper()
    return word or None


def route(statement: str) -> str:
    """``"nql"`` or ``"sql"``, or raise :class:`DialectError`.

    Returns the same strings `Dialect::name()` returns on the Rust side, so a
    response body built here and one built there are indistinguishable to a
    client.
    """
    head = first_word(statement)
    if head is None:
        raise DialectError("the statement is empty")
    if head in NQL_HEADS:
        return "nql"
    if head in SQL_HEADS:
        return "sql"
    raise DialectError(
        '"%s" does not begin a neSQL statement.\n'
        "neSQL is PostgreSQL's SQL plus NEDB's own clauses, so a statement "
        "starts in one of these two vocabularies:\n"
        "  NQL form begins with: %s\n"
        "  SQL form begins with: %s"
        % (head, ", ".join(NQL_HEADS), ", ".join(SQL_HEADS))
    )


def execute(db, statement: str):
    """Run a neSQL statement against a Python ``NEDB``, routing on the head.

    The SQL half goes through :mod:`nedb.sql`, which is a TRANSLATOR and has a
    smaller surface than the Rust evaluator — no joins, no subqueries, no
    multiple aggregates. That difference is real and is not hidden: a statement
    the translator cannot express raises rather than returning a partial
    answer, so a caller never receives rows from a different query than the one
    they wrote.
    """
    dialect = route(statement)
    if dialect == "nql":
        return db.query(statement)
    from . import sql as _sql
    return _sql.sql_exec(db, statement)
