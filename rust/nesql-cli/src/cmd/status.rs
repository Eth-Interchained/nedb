// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql status` — what this database is, right now.

use std::path::Path;
use std::sync::atomic::Ordering;

use nedb_engine::Db;
use serde_json::json;

use crate::out::{Exit, Report};

/// The head sequence: the seq of the most recent write, or 0 for an empty
/// database.
///
/// `Db::seq` is the NEXT sequence to be handed out, so the head is one less.
/// Reported as the head rather than as the counter because every other command
/// takes a seq that names a write, and two different meanings of "seq" across
/// one CLI is a bug waiting to be scripted against.
pub fn head_seq(db: &Db) -> u64 {
    db.seq.load(Ordering::SeqCst).saturating_sub(1)
}

pub fn run(db: &Db, path: &Path) -> Report {
    let seq = head_seq(db);
    let collections = db.collections();
    let floor = db.history_floor();
    let head = db.head();

    // A state root that cannot be computed is reported as such, and the command
    // exits 3. It is NOT reported as absent: "this database has no state root"
    // and "this build could not compute one" are different facts, and a
    // monitoring script that cannot tell them apart will eventually alert on the
    // wrong one.
    let (root, root_error) = match db.state_root() {
        Ok(r) => (Some(r), None),
        Err(e) => (None, Some(e)),
    };

    let exit = if root_error.is_some() { Exit::Indeterminate } else { Exit::Ok };

    let body = json!({
        "path": path.display().to_string(),
        "seq": seq,
        "head": head,
        "history_floor": floor,
        "collection_count": collections.len(),
        "collections": collections,
        "state_root": root,
        "state_root_error": root_error,
        "persisted_root_count": db.list_roots().len(),
    });

    let mut human = String::new();
    human.push_str(&format!("path            {}\n", path.display()));
    human.push_str(&format!("seq             {}\n", seq));
    human.push_str(&format!("head            {}\n", if head.is_empty() { "—".into() } else { head }));
    human.push_str(&format!("history floor   {}\n", floor));
    human.push_str(&format!("collections     {}", collections.len()));
    if !collections.is_empty() {
        human.push_str(&format!("  ({})", collections.join(", ")));
    }
    human.push('\n');
    match (&root, &root_error) {
        (Some(r), _) => {
            human.push_str(&format!("state root      {}\n", r.state_root));
            human.push_str(&format!("  version       {}\n", r.version));
            human.push_str(&format!("  namespace     {}\n", r.namespace_root));
            human.push_str(&format!("  records       {}\n", r.records_root));
            human.push_str(&format!(
                "  counts        {} collections · {} records\n",
                r.collection_count, r.record_count
            ));
        }
        (None, Some(e)) => {
            human.push_str(&format!(
                "state root      could not be determined: {}\n\
                 (exit 3 — this is not a report that the root is wrong)\n",
                e
            ));
        }
        (None, None) => {}
    }
    human.push_str(&format!("persisted roots {}", db.list_roots().len()));

    Report::new(exit, body, human)
}
