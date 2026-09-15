// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql query` — run **neSQL**.
//!
//! neSQL is PostgreSQL's SQL, inherited whole, PLUS what NEDB adds to it —
//! the temporal, causal and full-text clauses a permanent store can answer and
//! an overwriting one cannot. NQL's own FROM-first form is still accepted, so a
//! CLI that took only one of the two would be describing a smaller product than
//! the one that ships.
//!
//! # Routing is structural, so nothing is guessed
//!
//! The CLI's rule is that intent resolution must be total and must refuse
//! ambiguity. It survives here without a heuristic, because the two dialects
//! are disjoint at the first keyword:
//!
//! ```text
//! NQL  begins with FROM
//! SQL  begins with SELECT INSERT UPDATE DELETE EXPLAIN WITH SHOW SET VALUES TABLE BEGIN COMMIT ROLLBACK
//! ```
//!
//! Postgres has no statement form that begins with `FROM`, so the leading word
//! partitions the vocabularies rather than merely hinting at them. A first word
//! in neither is refused NAMING BOTH vocabularies — never routed to whichever
//! parser seems likelier, because "seems likelier" is exactly the guess the
//! rule forbids.
//!
//! `--nql` and `--sql` force a dialect. They exist so a user can demand a
//! dialect-specific refusal instead of a routing one, which matters when you
//! are debugging why the engine rejected something.
//!
//! # The engine owns the verdict
//!
//! This module does not pre-parse or pre-validate. A CLI-side check that
//! accepted something the engine rejects — or rejected something it accepts —
//! would make the CLI a second, quieter authority on the language, and the
//! first divergence would be found by a user. `nesql grammar` publishes the
//! surface; the engine rules on any given statement.

use std::sync::Arc;

use nedb_engine::{nql, pgwire, Db};
use serde_json::{json, Value};

use crate::out::{Exit, Report};

/// Which language a statement is written in.
///
/// Defined in `args`, because which dialect was named is a property of the
/// command line. Re-exported here so this module's callers and tests keep
/// referring to it as `query::Dialect`.
pub use crate::args::Dialect;

/// Routing is the ENGINE's decision, not this crate's.
///
/// `NQL_HEADS`, `SQL_HEADS` and `route` used to live here. They moved to
/// `nedb_engine::nesql` when the HTTP endpoint needed the same routing, so
/// that a statement means the same thing whether it arrives through `nesql
/// query` or through `POST /query`. Re-exported so this module's tests and
/// callers are unchanged.
pub use nedb_engine::nesql::{route, NQL_HEADS, SQL_HEADS};

pub fn run(db: &Arc<Db>, q: &str) -> Report {
    run_with(db, q, None)
}

/// `forced` comes from `--nql` / `--sql`.
pub fn run_with(db: &Arc<Db>, q: &str, forced: Option<Dialect>) -> Report {
    if q.trim().is_empty() {
        return Report::usage("query is empty");
    }
    let dialect = match forced {
        Some(d) => d,
        None => match route(q) {
            Ok(d) => d,
            Err(why) => return Report::usage(why),
        },
    };
    match dialect {
        Dialect::Nql => run_nql(db, q),
        Dialect::Sql => run_sql(db, q),
    }
}

fn refused(dialect: Dialect, q: &str, why: String) -> Report {
    // A rejected statement is a USAGE error, not a failure: nothing went wrong
    // with the database — the command line named something the language does
    // not accept. Exit 1 would suggest the query ran and something broke.
    Report::new(
        Exit::Usage,
        json!({ "error": why, "query": q, "dialect": dialect.name() }),
        format!(
            "usage: {}\n\ndialect: {}\nquery:   {}\nrun `nesql grammar` for the accepted forms",
            why, dialect.name(), q
        ),
    )
}

fn answer(dialect: Dialect, rows: Vec<Value>, scanned: Option<usize>, tag: Option<String>) -> Report {
    let human = if rows.is_empty() {
        match (&tag, scanned) {
            (Some(t), _) => t.clone(),
            (None, Some(n)) => format!("(0 rows, {} scanned)", n),
            (None, None) => "(0 rows)".to_string(),
        }
    } else {
        let mut s = String::new();
        for r in &rows {
            s.push_str(&serde_json::to_string(r).unwrap_or_default());
            s.push('\n');
        }
        match scanned {
            Some(n) => s.push_str(&format!("({} rows, {} scanned)", rows.len(), n)),
            None => s.push_str(&format!("({} rows)", rows.len())),
        }
        s
    };
    Report::ok(
        json!({
            "dialect": dialect.name(),
            "rows": rows,
            "count": rows.len(),
            "scanned": scanned,
            "tag": tag,
        }),
        human,
    )
}

fn run_nql(db: &Db, q: &str) -> Report {
    match nql::query(db, q) {
        Err(e) => refused(Dialect::Nql, q, e.to_string()),
        Ok((rows, scanned)) => answer(Dialect::Nql, rows, Some(scanned), None),
    }
}

fn run_sql(db: &Arc<Db>, q: &str) -> Report {
    // Writes are allowed. `nesql` is an interactive tool, and a query command
    // that silently refused INSERT would be a different tool than the one the
    // grammar describes. `execute_sql` takes the same read-only flag a pgwire
    // session carries, so the policy lives in one place rather than two.
    match pgwire::execute_sql(db, q, false) {
        Err(why) => refused(Dialect::Sql, q, why),
        Ok(done) => {
            let tag = if done.has_rows { None } else { Some(done.tag.clone()) };
            answer(Dialect::Sql, done.rows, None, tag)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_two_vocabularies_do_not_overlap() {
        // The whole no-guessing argument rests on this. If a word ever appears
        // in both lists, routing becomes a coin flip and the CLI starts lying
        // about being total.
        for n in NQL_HEADS {
            assert!(
                !SQL_HEADS.contains(n),
                "{:?} begins a statement in both dialects — routing is no longer structural",
                n
            );
        }
    }

    #[test]
    fn each_dialect_routes_to_itself() {
        assert_eq!(route("FROM orders").unwrap(), Dialect::Nql);
        assert_eq!(route("from orders WHERE x = 1").unwrap(), Dialect::Nql);
        assert_eq!(route("SELECT * FROM orders").unwrap(), Dialect::Sql);
        assert_eq!(route("  explain select 1").unwrap(), Dialect::Sql);
        assert_eq!(route("(SELECT 1) UNION (SELECT 2)").unwrap(), Dialect::Sql);
        assert_eq!(route("INSERT INTO o VALUES (1)").unwrap(), Dialect::Sql);
    }

    #[test]
    fn a_word_in_neither_vocabulary_is_refused_naming_both() {
        let e = route("GRANT ALL ON orders").unwrap_err();
        assert!(e.contains("GRANT"), "{}", e);
        assert!(e.contains("FROM"), "the refusal must name the NQL vocabulary: {}", e);
        assert!(e.contains("SELECT"), "and the SQL one: {}", e);
    }

    #[test]
    fn an_empty_statement_is_refused_rather_than_routed() {
        assert!(route("").is_err());
        assert!(route("   \n ").is_err());
    }
}
