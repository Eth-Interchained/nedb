// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql diff <FROM> <TO>` — what changed in logical state between two
//! sequences.
//!
//! Not a log and not a text diff: a set of changes to documents and
//! collections. The engine refuses below the history floor rather than
//! returning a quietly incomplete answer, and this reports that refusal as
//! exit 3 — could-not-determine — because a pruned range is not a failure and
//! is certainly not an empty diff.

use nedb_engine::diff as engine;
use nedb_engine::Db;
use serde_json::json;

use crate::out::{Exit, Report};

pub fn run(db: &Db, from: u64, to: u64) -> Report {
    match engine::diff(db, from, to) {
        Err(why) => {
            // HISTORY_PRUNED is indeterminate; a reversed range is the caller
            // asking for something that does not exist, which is usage.
            let exit = if why.starts_with(engine::HISTORY_PRUNED) {
                Exit::Indeterminate
            } else {
                Exit::Usage
            };
            Report::new(
                exit,
                json!({ "error": why, "from_seq": from, "to_seq": to }),
                format!("{}: {}", if exit == Exit::Indeterminate { "indeterminate" } else { "usage" }, why),
            )
        }
        Ok(d) => {
            let mut human = format!("diff {} → {}\n", d.from_seq, d.to_seq);
            if d.is_empty() {
                human.push_str("  (no change)");
            } else {
                for c in &d.collections {
                    human.push_str(&format!("  {:?} collection {}\n", c.kind, c.name));
                }
                for c in &d.documents {
                    human.push_str(&format!("  {:?} {}/{}", c.kind, c.coll, c.id));
                    if let Some(f) = &c.fields {
                        let mut parts = Vec::new();
                        if !f.added.is_empty() { parts.push(format!("+{}", f.added.join(","))); }
                        if !f.removed.is_empty() { parts.push(format!("-{}", f.removed.join(","))); }
                        if !f.changed.is_empty() { parts.push(format!("~{}", f.changed.join(","))); }
                        if !parts.is_empty() { human.push_str(&format!("  [{}]", parts.join(" "))); }
                    }
                    if c.temporal.is_some() { human.push_str("  [validity]"); }
                    human.push('\n');
                }
                human.push_str(&format!(
                    "  {} collection(s), {} document(s)",
                    d.collections.len(), d.documents.len()
                ));
            }
            Report::ok(json!({ "diff": d }), human.trim_end().to_string())
        }
    }
}
