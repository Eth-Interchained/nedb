// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql tag` — immutable names for states.
//!
//! A tag target never changes, deletion leaves an audited tombstone, and a
//! deleted name is never reusable. That last rule is the reason the delete
//! path here reads as blunt as it does: "which v1.0 did that build use?" must
//! have exactly one answer forever.

use nedb_engine::refs;
use nedb_engine::Db;
use serde_json::json;

use crate::args::TagCmd;
use crate::out::{Exit, Report};

pub fn run(db: &Db, cmd: &TagCmd) -> Report {
    match cmd {
        TagCmd::Create { name, at, message } => create(db, name, *at, message.as_deref()),
        TagCmd::Inspect { name } => inspect(db, name),
        TagCmd::List { include_deleted } => list(db, *include_deleted),
        TagCmd::Delete { name } => delete(db, name),
    }
}

fn create(db: &Db, name: &str, at: u64, message: Option<&str>) -> Report {
    match refs::create_tag(db, name, at, message) {
        // Every refusal here is the caller naming something they may not have
        // — a taken name, a retracted one, a sequence that has not happened.
        // Usage, not failure: nothing went wrong with the database.
        Err(e) => Report::new(
            Exit::Usage,
            json!({ "error": e.to_string(), "tag": name }),
            format!("usage: {}", e),
        ),
        Ok(t) => {
            let human = format!(
                "tagged {} at seq {}\n  state root  {}{}",
                t.name, t.at_seq,
                t.state_root.clone().unwrap_or_else(||
                    "— (no root persisted at that sequence; `nesql root create --at` takes one)".into()),
                t.message.as_ref().map(|m| format!("\n  message {}", m)).unwrap_or_default(),
            );
            Report::ok(json!({ "tag": t }), human)
        }
    }
}

fn inspect(db: &Db, name: &str) -> Report {
    // Deleted tags are reachable on purpose: the answer to "what did v1.0
    // point at" outlives the tag, and refusing to say is worse than saying
    // "it was retracted, and here is what it named".
    match refs::get_tag_including_deleted(db, name) {
        None => Report::new(
            Exit::NotFound,
            json!({ "error": "no such tag", "tag": name }),
            format!("not found: no tag named {:?}", name),
        ),
        Some(t) => {
            let human = format!(
                "tag {}\nat_seq {}\nstate root  {}\nstatus {}{}",
                t.name, t.at_seq,
                t.state_root.clone().unwrap_or_else(|| "—".into()),
                if t.deleted { "DELETED (the name cannot be reused)" } else { "live" },
                t.message.as_ref().map(|m| format!("\nmessage {}", m)).unwrap_or_default(),
            );
            Report::ok(json!({ "tag": t }), human)
        }
    }
}

fn list(db: &Db, include_deleted: bool) -> Report {
    let tags = if include_deleted {
        refs::list_tags_including_deleted(db)
    } else {
        refs::list_tags(db)
    };
    let human = if tags.is_empty() {
        "no tags".to_string()
    } else {
        tags.iter()
            .map(|t| format!(
                "{:>12}  {}{}",
                t.at_seq, t.name, if t.deleted { "  (deleted)" } else { "" }))
            .collect::<Vec<_>>().join("\n")
    };
    Report::ok(json!({ "tags": tags, "count": tags.len() }), human)
}

fn delete(db: &Db, name: &str) -> Report {
    match refs::delete_tag(db, name) {
        Err(e) => Report::new(
            Exit::Usage,
            json!({ "error": e.to_string(), "tag": name }),
            format!("usage: {}", e),
        ),
        Ok(false) => Report::new(
            Exit::NotFound,
            json!({ "error": "no live tag by that name", "tag": name }),
            format!("not found: no live tag named {:?}", name),
        ),
        Ok(true) => Report::ok(
            json!({ "deleted": name }),
            format!(
                "retracted {}\n  the tombstone records what it pointed at, and the name can never be reused", name),
        ),
    }
}
