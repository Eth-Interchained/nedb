// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! **neSQL** — the whole language: NQL *and* PostgreSQL SQL.
//!
//! Not a third dialect. neSQL is the name for the pair, and this module is the
//! one place that decides which half a statement is written in.
//!
//! # Why the router lives in the engine
//!
//! `nesql-cli` had this logic first, and it was correct there. Putting a
//! second copy in the HTTP server would have been the same mistake this whole
//! effort exists to undo: two implementations of one decision, drifting until
//! `POST /query` and `nesql query` disagree about what a statement means —
//! and disagreeing about the MEANING of a statement is worse than disagreeing
//! about its result, because nothing looks broken.
//!
//! So it moved down here, where both the daemon and the CLI can reach it, and
//! the CLI re-exports it rather than keeping its own.
//!
//! # Routing is structural, so nothing is guessed
//!
//! ```text
//! NQL  begins with FROM
//! SQL  begins with SELECT INSERT UPDATE DELETE EXPLAIN WITH SHOW SET
//!                  VALUES TABLE BEGIN COMMIT ROLLBACK
//! ```
//!
//! PostgreSQL has no statement form that begins with `FROM`, so the leading
//! keyword PARTITIONS the two vocabularies rather than hinting at them. That
//! is what makes this a decision rather than a heuristic, and it is why a
//! first word in neither is REFUSED naming both — never handed to whichever
//! parser seems likelier, because "seems likelier" is the guess the rule
//! forbids.

/// Which half of neSQL a statement is written in — inherited PostgreSQL, or NQL.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dialect {
    Nql,
    Sql,
}

impl Dialect {
    pub fn name(self) -> &'static str {
        match self {
            Dialect::Nql => "nql",
            Dialect::Sql => "sql",
        }
    }
}

/// Statement-initial keywords, per dialect. Disjoint by construction; the test
/// `the_two_vocabularies_do_not_overlap` holds them that way.
pub const NQL_HEADS: &[&str] = &["FROM"];
pub const SQL_HEADS: &[&str] = &[
    "SELECT", "INSERT", "UPDATE", "DELETE", "EXPLAIN", "WITH", "SHOW", "SET",
    "VALUES", "TABLE", "BEGIN", "COMMIT", "ROLLBACK",
];

fn first_word(s: &str) -> Option<String> {
    s.split_whitespace()
        .next()
        // A statement may open with a parenthesis — `(SELECT …) UNION …`.
        .map(|w| w.trim_start_matches('(').trim_end_matches(';').to_uppercase())
        .filter(|w| !w.is_empty())
}

/// Decide which half a statement is written in, or refuse.
pub fn route(q: &str) -> Result<Dialect, String> {
    let Some(head) = first_word(q) else {
        return Err("the statement is empty".to_string());
    };
    if NQL_HEADS.contains(&head.as_str()) {
        return Ok(Dialect::Nql);
    }
    if SQL_HEADS.contains(&head.as_str()) {
        return Ok(Dialect::Sql);
    }
    Err(format!(
        "{:?} does not begin a neSQL statement.\n\
         neSQL is PostgreSQL's SQL plus NEDB's own clauses, so a statement starts\n\
         in one of these two vocabularies:\n  \
         NQL form begins with: {}\n  \
         SQL form begins with: {}",
        head,
        NQL_HEADS.join(", "),
        SQL_HEADS.join(", "),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_two_vocabularies_do_not_overlap() {
        // The whole no-guessing argument rests on this. If a word ever appears
        // in both lists, routing becomes a coin flip and neSQL starts lying
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

    /// The reason this module is in the engine rather than in the CLI.
    #[test]
    fn a_sql_statement_is_never_handed_to_the_nql_parser() {
        // `POST /query {"nql": "SELECT ..."}` is the case that motivated
        // this: the field is called `nql` for compatibility, and its contents
        // are no longer required to be NQL.
        assert_eq!(route("SELECT who FROM orders").unwrap(), Dialect::Sql);
        assert_eq!(route("FROM orders").unwrap(), Dialect::Nql);
    }
}
