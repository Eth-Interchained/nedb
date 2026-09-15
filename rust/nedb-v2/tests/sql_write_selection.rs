// SPDX-License-Identifier: BUSL-1.1
// SPDX-FileCopyrightText: © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `UPDATE` and `DELETE` choose their rows with the SQL evaluator.
//!
//! # What this file is guarding
//!
//! Writes used to select rows by rendering the SQL `WHERE` clause into NQL text
//! and running it through the NQL parser. The read path had already stopped
//! doing that; the write path had not. The visible cost was that a predicate
//! form the evaluator handles fine — a subquery — was unreachable from a write,
//! because the rendering produced NQL that the NQL parser cannot parse.
//!
//! So the risk this file exists to catch is not "does UPDATE work". It is:
//!
//! 1. The subquery case STAYS reachable. It is the reason the change was made,
//!    and a future simplification that quietly restores the NQL rendering would
//!    put it back out of reach without failing anything else.
//!
//! 2. A write against a collection that does not exist still FAILS. This is the
//!    sharp edge of the change and the one worth writing down: `nql::query`
//!    errors on an absent collection, while the evaluator's scan returns no
//!    rows — because a schemaless read of an absent collection is legitimately
//!    empty. Swapping the two without a guard turns `UPDATE nowhere SET …`
//!    from a loud `42P01` into a silent `UPDATE 0`: a write that reports
//!    success having done nothing. There is no worse outcome available, and
//!    nothing else in the suite would have noticed.
//!
//! 3. An `UPDATE` matches exactly what a `SELECT` with the same `WHERE`
//!    matches. That equivalence is the whole claim of having one predicate
//!    implementation instead of two, and it is only a claim until asserted.

use nedb_engine::db::Db;
use nedb_engine::pgwire::execute_sql;
use serde_json::json;
use std::sync::Arc;

fn fixture() -> (Arc<Db>, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let db = Arc::new(Db::open(dir.path(), None).unwrap());
    for (id, who, total, region) in [
        ("1", "acme", 100, "us"),
        ("2", "globex", 250, "us"),
        ("3", "initech", 300, "eu"),
        ("4", "umbrella", 50, "eu"),
    ] {
        db.put(
            "orders",
            id,
            json!({ "who": who, "total": total, "region": region }),
            vec![],
            None,
            None,
        )
        .unwrap();
    }
    (db, dir)
}

fn whos(db: &Arc<Db>, sql: &str) -> Vec<String> {
    let mut v: Vec<String> = execute_sql(db, sql, true)
        .expect(sql)
        .rows
        .iter()
        .filter_map(|r| r.get("who").and_then(|w| w.as_str()).map(String::from))
        .collect();
    v.sort();
    v
}

#[test]
fn update_can_select_rows_with_a_subquery() {
    let (db, _d) = fixture();

    // The case that was unreachable. `IN (SELECT …)` rendered into NQL text is
    // not parseable NQL, so this used to be a 42601 rather than a write.
    let done = execute_sql(
        &db,
        "UPDATE orders SET status = 'flagged' \
         WHERE _id IN (SELECT _id FROM orders WHERE total > 200)",
        false,
    )
    .expect("a subquery must be usable to select rows for an UPDATE");
    assert_eq!(done.tag, "UPDATE 2", "only the two orders over 200");

    assert_eq!(
        whos(&db, "SELECT who FROM orders WHERE status = 'flagged'"),
        vec!["globex", "initech"],
        "and it must be the RIGHT two -- a count alone would pass while \
         flagging the wrong rows"
    );
}

#[test]
fn delete_can_select_rows_with_a_subquery_and_still_return_them() {
    let (db, _d) = fixture();

    // RETURNING has to capture the row BEFORE the tombstone, so the selection
    // must yield whole rows and not just ids.
    let done = execute_sql(
        &db,
        "DELETE FROM orders WHERE _id IN (SELECT _id FROM orders WHERE region = 'eu') \
         RETURNING who",
        false,
    )
    .expect("a subquery must be usable to select rows for a DELETE");
    assert_eq!(done.tag, "DELETE 2");

    let mut returned: Vec<String> = done
        .rows
        .iter()
        .filter_map(|r| r.get("who").and_then(|w| w.as_str()).map(String::from))
        .collect();
    returned.sort();
    assert_eq!(
        returned,
        vec!["initech", "umbrella"],
        "RETURNING must carry the rows as they were, read before the delete"
    );

    assert_eq!(whos(&db, "SELECT who FROM orders"), vec!["acme", "globex"]);
}

#[test]
fn a_write_against_an_absent_collection_fails_rather_than_reporting_zero() {
    let (db, _d) = fixture();

    for sql in [
        "UPDATE nowhere SET x = 1 WHERE y = 2",
        "DELETE FROM nowhere WHERE y = 2",
    ] {
        // `Executed` is not Debug, so this is matched rather than unwrapped --
        // and the Ok arm reports the TAG, which is the whole diagnosis: a
        // regression here reads "UPDATE 0", not "it compiled".
        let e = match execute_sql(&db, sql, false) {
            Err(e) => e,
            Ok(done) => panic!(
                "{:?} must be refused, but it succeeded with {:?}. An absent \
                 collection is a legitimately empty scan for the evaluator, so \
                 without the guard a write reports success having written nothing.",
                sql, done.tag
            ),
        };
        assert!(
            e.contains("nowhere") && e.contains("does not exist"),
            "the refusal must name the relation: {} -> {}",
            sql,
            e
        );
    }
}

#[test]
fn an_update_matches_exactly_what_a_select_matches() {
    // The equivalence that justifies having one predicate implementation.
    // Each predicate is first measured with a SELECT, then used in an UPDATE,
    // and the counts must agree -- on a fresh fixture per case so the writes
    // do not interfere with each other.
    for pred in [
        "total > 200",
        "total BETWEEN 1 AND 120",
        "region = 'eu' AND total < 100",
        "who LIKE 'a%'",
        "who IN ('acme', 'globex')",
        "total > 200 OR region = 'us'",
        "_id IN (SELECT _id FROM orders WHERE total > 90)",
        // Matches nothing: zero must also agree, or the equivalence only holds
        // where it is convenient.
        "total > 100000",
    ] {
        let (db, _d) = fixture();

        let selected = execute_sql(
            &db,
            &format!("SELECT _id FROM orders WHERE {}", pred),
            true,
        )
        .unwrap_or_else(|e| panic!("SELECT with {:?} failed: {}", pred, e))
        .rows
        .len();

        let tag = execute_sql(
            &db,
            &format!("UPDATE orders SET touched = 1 WHERE {}", pred),
            false,
        )
        .unwrap_or_else(|e| panic!("UPDATE with {:?} failed: {}", pred, e))
        .tag;

        assert_eq!(
            tag,
            format!("UPDATE {}", selected),
            "SELECT and UPDATE disagree about which rows {:?} matches -- that \
             is two predicate implementations, which is what this change removed",
            pred
        );
    }
}
