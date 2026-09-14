// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql branch` — lines of work, and what they pin.
//!
//! A live branch pins the history its future merge needs, and compaction
//! refuses to prune past it. That is worth surfacing in `list`: the operator
//! deciding whether to reclaim space needs to see what is holding history
//! open, not discover it from a refusal.

use nedb_engine::branch as engine;
use nedb_engine::Db;
use serde_json::json;

use crate::args::BranchCmd;
use crate::out::{Exit, Report};

pub fn run(db: &Db, cmd: &BranchCmd) -> Report {
    match cmd {
        BranchCmd::Create { name, base } => create(db, name, *base),
        BranchCmd::Put { name, coll, id, json } => put(db, name, coll, id, json),
        BranchCmd::Rm { name, coll, id } => rm(db, name, coll, id),
        BranchCmd::Inspect { name } => inspect(db, name),
        BranchCmd::List { include_all } => list(db, *include_all),
        BranchCmd::Abandon { name } => abandon(db, name),
    }
}

fn create(db: &Db, name: &str, base: u64) -> Report {
    match engine::create_branch(db, name, base) {
        Err(e) => Report::new(
            Exit::Usage,
            json!({ "error": e.to_string(), "branch": name }),
            format!("usage: {}", e),
        ),
        Ok(b) => Report::ok(
            json!({ "branch": b }),
            format!(
                "branch {} forked at seq {}\n  generation  {}\n  this branch now pins history at {} — compaction will refuse to prune past it until the branch is merged or abandoned",
                b.name, b.base_seq, b.created_seq, b.base_seq
            ),
        ),
    }
}

fn put(db: &Db, name: &str, coll: &str, id: &str, json: &str) -> Report {
    let value = match serde_json::from_str::<serde_json::Value>(json) {
        Ok(v) => v,
        Err(e) => return Report::usage(format!("the document is not valid JSON: {}", e)),
    };
    match engine::branch_put(db, name, coll, id, value) {
        Err(e) => Report::new(Exit::Usage, json!({ "error": e.to_string() }), format!("usage: {}", e)),
        Ok(w) => Report::ok(
            json!({ "write": w }),
            format!(
                "{}/{} written on {}\n  the destination does not see it until merge",
                coll, id, name),
        ),
    }
}

fn rm(db: &Db, name: &str, coll: &str, id: &str) -> Report {
    match engine::branch_delete(db, name, coll, id) {
        Err(e) => Report::new(Exit::Usage, json!({ "error": e.to_string() }), format!("usage: {}", e)),
        Ok(_) => Report::ok(
            json!({ "deleted": { "branch": name, "coll": coll, "id": id } }),
            format!(
                "{}/{} deleted on {}\n  recorded as an explicit deletion, which is a \
                 different fact from the branch never touching it",
                coll, id, name),
        ),
    }
}

fn inspect(db: &Db, name: &str) -> Report {
    match engine::get_branch(db, name) {
        None => Report::new(
            Exit::NotFound,
            json!({ "error": "no such branch", "branch": name }),
            format!("not found: no branch named {:?}", name),
        ),
        Some(b) => {
            let writes = engine::branch_writes(db, name);
            let human = format!(
                "branch {}\nbase_seq {}\ngeneration  {}\nstatus {:?}\nwrites {}{}",
                b.name, b.base_seq, b.created_seq, b.status, writes.len(),
                if writes.is_empty() { String::new() } else {
                    format!("\n{}", writes.iter()
                        .map(|w| format!("  {} {}/{}",
                            if w.value.is_some() { "put" } else { "del" }, w.coll, w.id))
                        .collect::<Vec<_>>().join("\n"))
                },
            );
            Report::ok(json!({ "branch": b, "writes": writes }), human)
        }
    }
}

fn list(db: &Db, include_all: bool) -> Report {
    let branches = if include_all { engine::list_all_branches(db) } else { engine::list_branches(db) };
    let pinned = engine::minimum_pinned_seq(db);
    let human = if branches.is_empty() {
        "no branches".to_string()
    } else {
        let mut s = branches.iter()
            .map(|b| format!("{:>12}  {}  {:?}", b.base_seq, b.name, b.status))
            .collect::<Vec<_>>().join("\n");
        match pinned {
            Some(p) => s.push_str(&format!("\n\nhistory pinned at {} by the live branches above", p)),
            None => s.push_str("\n\nnothing pins history — compaction is free to proceed"),
        }
        s
    };
    Report::ok(json!({ "branches": branches, "count": branches.len(),
                       "minimum_pinned_seq": pinned }), human)
}

fn abandon(db: &Db, name: &str) -> Report {
    match engine::abandon_branch(db, name) {
        Err(e) => Report::new(Exit::Usage, json!({ "error": e.to_string() }), format!("usage: {}", e)),
        Ok(false) => Report::new(
            Exit::NotFound,
            json!({ "error": "no live branch by that name", "branch": name }),
            format!("not found: no live branch named {:?}", name),
        ),
        Ok(true) => Report::ok(
            json!({ "abandoned": name }),
            format!(
                "abandoned {}\n  its writes stay in history; it no longer pins anything",
                name),
        ),
    }
}
