// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! Every command, against a real engine database.
//!
//! These call the command functions rather than spawning the binary. Spawning
//! tests the renderer and the shell; calling tests the DECISION — which exit
//! code, which payload — and the decision is the part that a caller scripts
//! against.

use std::path::Path;
use std::sync::Arc;

use nedb_engine::Db;
use nesql::args::{self, Command, RootCmd, Thing};
use nesql::cmd;
use nesql::out::{Exit, Format};

fn seed(dir: &Path) -> Arc<Db> {
    let db = Db::open(dir, None).expect("open");
    db.put("orders", "1", serde_json::json!({"total": 100, "who": "acme"}), vec![], None, None).unwrap();
    db.put("orders", "2", serde_json::json!({"total": 250, "who": "globex"}), vec![], None, None).unwrap();
    db.put("users", "u1", serde_json::json!({"name": "mark"}), vec![], None, None).unwrap();
    db.flush_all();
    Arc::new(db)
}

fn parse(line: &[&str]) -> args::Invocation {
    let argv: Vec<String> = line.iter().map(|s| s.to_string()).collect();
    args::parse(&argv).unwrap_or_else(|e| panic!("{:?} refused: {}", line, e))
}

#[test]
fn status_reports_the_database_it_was_pointed_at() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let r = cmd::status::run(&db, dir.path());
    assert_eq!(r.exit, Exit::Ok);
    assert_eq!(r.body["collection_count"], 2);
    assert_eq!(r.body["state_root"]["record_count"], 3);
    assert!(r.body["state_root"]["state_root"].as_str().unwrap().len() == 64);
    // The path is in the output because a status report that does not say
    // WHICH database it described is unusable in a script with two of them.
    assert_eq!(r.body["path"], dir.path().display().to_string());
}

#[test]
fn a_missing_database_is_not_found_and_is_not_created() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("nope");
    let err = cmd::open_db(&missing).err().expect("must refuse");
    assert_eq!(err.exit, Exit::NotFound);
    assert!(!missing.exists(), "a read command must not create a database");
}

#[test]
fn root_create_inspect_and_verify_agree() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());

    let created = cmd::root::create(&db, None);
    assert_eq!(created.exit, Exit::Ok);
    assert_eq!(created.body["backfilled"], false);
    let at = created.body["root"]["at_seq"].as_u64().unwrap();
    let digest = created.body["root"]["state_root"].as_str().unwrap().to_string();

    let seen = cmd::root::inspect(&db, Some(at));
    assert_eq!(seen.exit, Exit::Ok);
    assert_eq!(seen.body["root"]["state_root"], digest);

    let v = cmd::root::verify(&db, Some(at));
    assert_eq!(v.exit, Exit::Ok, "a fresh root must verify");
    assert_eq!(v.body["verified"], true);
    assert_eq!(v.body["mismatch"], false);
}

#[test]
fn root_verify_distinguishes_checked_and_good_from_could_not_check() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let at = cmd::root::create(&db, None).body["root"]["at_seq"].as_u64().unwrap();

    assert_eq!(cmd::root::verify(&db, Some(at)).exit.code(), 0);

    // Put the database into the pruned state. The floor is what verification
    // consults; how it got raised is compaction's business.
    db.set_history_floor(at + 1).unwrap();

    let v = cmd::root::verify(&db, Some(at));
    assert_eq!(v.exit.code(), 3, "unavailable has its own code, not 0 and not 1");
    assert_eq!(v.body["verified"], false, "unavailable is not verified");
    assert_eq!(v.body["mismatch"], false, "and it is not a mismatch");
    assert_eq!(v.body["reason"], "HISTORY_PRUNED");
}

#[test]
fn verifying_a_root_that_was_never_taken_is_not_found() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let v = cmd::root::verify(&db, Some(1));
    assert_eq!(v.exit.code(), 4);
    assert_eq!(v.body["verified"], false);
}

#[test]
fn root_create_refuses_a_sequence_the_database_has_never_reached() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let r = cmd::root::create(&db, Some(9_999));
    assert_eq!(r.exit, Exit::NotFound);
    assert!(r.human.contains("past the head"));
}

#[test]
fn backfilling_a_root_is_reported_as_backfilling() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let head = cmd::status::head_seq(&db);
    let r = cmd::root::create(&db, Some(head - 1));
    assert_eq!(r.exit, Exit::Ok);
    assert_eq!(r.body["backfilled"], true, "a past root costs more and says so");
}

#[test]
fn root_list_is_ordered_and_reports_the_floor() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    for _ in 0..3 {
        db.put("orders", "1", serde_json::json!({"total": 1}), vec![], None, None).unwrap();
        cmd::root::create(&db, None);
    }
    let r = cmd::root::list(&db);
    assert_eq!(r.exit, Exit::Ok);
    let seqs: Vec<u64> = r.body["roots"].as_array().unwrap().iter()
        .map(|x| x["at_seq"].as_u64().unwrap()).collect();
    let mut sorted = seqs.clone();
    sorted.sort();
    assert_eq!(seqs, sorted);
    assert_eq!(r.body["history_floor"], 0);
}

#[test]
fn inspect_reaches_a_collection_a_document_and_a_sequence() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());

    let c = cmd::inspect::run(&db, &Thing::Collection("orders".into()));
    assert_eq!(c.exit, Exit::Ok);
    assert_eq!(c.body["live_rows"], 2);

    let d = cmd::inspect::run(&db, &Thing::Document { coll: "orders".into(), id: "1".into() });
    assert_eq!(d.exit, Exit::Ok);
    assert_eq!(d.body["node"]["data"]["total"], 100);

    let s = cmd::inspect::run(&db, &Thing::Seq(cmd::status::head_seq(&db)));
    assert_eq!(s.exit, Exit::Ok);
    assert_eq!(s.body["state_root"]["record_count"], 3);
}

#[test]
fn inspect_says_deleted_rather_than_merely_absent() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    db.delete("orders", "1").unwrap();
    let d = cmd::inspect::run(&db, &Thing::Document { coll: "orders".into(), id: "1".into() });
    assert_eq!(d.exit, Exit::NotFound);
    assert_eq!(d.body["deleted"], true, "a tombstone is not an absence");

    let never = cmd::inspect::run(&db, &Thing::Document { coll: "orders".into(), id: "zzz".into() });
    assert_eq!(never.body["deleted"], false);
}

#[test]
fn inspecting_a_pruned_sequence_is_indeterminate_not_not_found() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    db.set_history_floor(cmd::status::head_seq(&db)).unwrap();
    let s = cmd::inspect::run(&db, &Thing::Seq(0));
    assert_eq!(s.exit.code(), 3);
    assert_eq!(s.body["error"], "HISTORY_PRUNED");
}

#[test]
fn query_runs_and_a_bad_query_is_a_usage_error() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());

    let ok = cmd::query::run(&db, "FROM orders");
    assert_eq!(ok.exit, Exit::Ok);
    assert_eq!(ok.body["count"], 2);

    let bad = cmd::query::run(&db, "SELECT * FROM nowhere WITH FEELING");
    assert_eq!(bad.exit, Exit::Usage, "the language rejected it; the database is fine");
    assert!(bad.body["error"].is_string());

    let empty = cmd::query::run(&db, "   ");
    assert_eq!(empty.exit, Exit::Usage);
}

#[test]
fn a_query_never_returns_the_engines_own_bookkeeping() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    cmd::root::create(&db, None);
    let r = cmd::query::run(&db, "FROM orders");
    for row in r.body["rows"].as_array().unwrap() {
        assert_eq!(row["_coll"], "orders");
    }
    let status = cmd::status::run(&db, dir.path());
    let colls = status.body["collections"].as_array().unwrap();
    assert!(
        colls.iter().all(|c| !c.as_str().unwrap().starts_with("_nedb")),
        "reserved collections are not part of the user's namespace: {:?}", colls
    );
}

#[test]
fn grammar_and_version_answer_without_a_database() {
    for c in [Command::Grammar, Command::Version, Command::Help] {
        let r = cmd::dispatch_dbless(&c).expect("must not need a database");
        assert_eq!(r.exit, Exit::Ok);
    }
    let g = cmd::grammar::run();
    assert_eq!(g.body["digest"].as_str().unwrap().len(), 64);
    let v = cmd::version::run();
    assert_eq!(v.body["nesql"], env!("CARGO_PKG_VERSION"));
    assert!(v.body["engine"].is_string(), "both dimensions, always");
}

#[test]
fn constitution_checks_this_cli_against_this_engine() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let r = cmd::constitution::run(&db);
    // The check must actually run and reach a verdict. What matters is that it
    // is not silently OK: an incompatible verdict must carry reasons.
    assert!(matches!(r.exit, Exit::Ok | Exit::Unsupported));
    if r.exit == Exit::Unsupported {
        let detail = r.body["detail"].as_array().unwrap();
        assert!(!detail.is_empty(), "an incompatible verdict with no reason is unactionable");
    }
    assert!(!r.body["constitution"]["invariants"].as_array().unwrap().is_empty());
    assert_eq!(r.body["constitution_digest"].as_str().unwrap().len(), 64);
}

#[test]
fn reserved_but_unimplemented_commands_refuse_rather_than_succeed() {
    for word in ["diff", "tag", "branch", "merge"] {
        let inv = parse(&[word]);
        let r = cmd::dispatch_dbless(&inv.command).expect("answered from the binary");
        assert_eq!(r.exit, Exit::Usage, "{} must not look like it did something", word);
        assert_eq!(r.body["reserved"], true);
    }
}

#[test]
fn every_command_renders_as_json_with_the_same_envelope() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let cases = vec![
        Command::Status,
        Command::Log { limit: Some(5), since: None },
        Command::Inspect(Thing::Collection("orders".into())),
        Command::Root(RootCmd::List),
        Command::Grammar,
        Command::Version,
        Command::Query("FROM orders".into()),
    ];
    for c in cases {
        let r = cmd::dispatch(&c, &db, dir.path());
        let text = r.render(c.name(), Format::Json);
        let v: serde_json::Value = serde_json::from_str(&text)
            .unwrap_or_else(|e| panic!("{} did not emit valid JSON: {}\n{}", c.name(), e, text));
        assert!(v["ok"].is_boolean(), "{} is missing the envelope", c.name());
        assert_eq!(v["command"], c.name());
    }
}

#[test]
fn the_exit_code_table_is_exactly_what_is_documented() {
    assert_eq!(Exit::Ok.code(), 0);
    assert_eq!(Exit::Failure.code(), 1);
    assert_eq!(Exit::Usage.code(), 2);
    assert_eq!(Exit::Indeterminate.code(), 3);
    assert_eq!(Exit::NotFound.code(), 4);
    assert_eq!(Exit::Unsupported.code(), 5);
    // An engine code this build cannot name must not be passed through as if
    // it were understood.
    assert_eq!(Exit::from_code(97), Exit::Failure);
}

// ── neQL: both halves of the language ─────────────────────────────────────

#[test]
fn neql_runs_nql_and_sql_through_one_command() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());

    let n = cmd::query::run(&db, "FROM orders");
    assert_eq!(n.exit, Exit::Ok, "{}", n.human);
    assert_eq!(n.body["dialect"], "nql");
    assert_eq!(n.body["count"], 2);

    let s = cmd::query::run(&db, "SELECT * FROM orders");
    assert_eq!(s.exit, Exit::Ok, "{}", s.human);
    assert_eq!(s.body["dialect"], "sql");
    assert_eq!(s.body["count"], 2);
}

#[test]
fn neql_writes_because_it_is_an_interactive_tool() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());

    let w = cmd::query::run(&db, "INSERT INTO orders (_id, total) VALUES ('3', 7)");
    assert_eq!(w.exit, Exit::Ok, "{}", w.human);

    let back = cmd::query::run(&db, "FROM orders");
    assert_eq!(back.body["count"], 3, "the write is visible to the other dialect");
}

#[test]
fn a_statement_in_neither_dialect_is_refused_naming_both() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    let r = cmd::query::run(&db, "GRANT ALL ON orders TO nobody");
    assert_eq!(r.exit, Exit::Usage);
    assert!(r.human.contains("FROM") && r.human.contains("SELECT"), "{}", r.human);
}

#[test]
fn a_forced_dialect_produces_that_dialects_own_error() {
    let dir = tempfile::tempdir().unwrap();
    let db = seed(dir.path());
    // Valid SQL, forced through the NQL parser: the refusal must come from NQL,
    // not from routing.
    let r = cmd::query::run_with(&db, "SELECT * FROM orders", Some(cmd::query::Dialect::Nql));
    assert_eq!(r.exit, Exit::Usage);
    assert_eq!(r.body["dialect"], "nql");
}
