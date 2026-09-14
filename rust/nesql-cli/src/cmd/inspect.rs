// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql inspect` — look at one thing closely.
//!
//! The parser has already decided WHAT kind of thing was named, and refused if
//! the argument could have been two kinds. That separation is the point: intent
//! resolution is total and happens once, in `args.rs`, so nothing down here
//! guesses.

use std::sync::atomic::Ordering;

use nedb_engine::Db;
use serde_json::json;

use crate::args::Thing;
use crate::cmd::status::head_seq;
use crate::out::{Exit, Report};

pub fn run(db: &Db, thing: &Thing) -> Report {
    match thing {
        Thing::Collection(name) => collection(db, name),
        Thing::Document { coll, id } => document(db, coll, id),
        Thing::Root(seq) => crate::cmd::root::inspect(db, Some(*seq)),
        Thing::Seq(seq) => sequence(db, *seq),
    }
}

fn collection(db: &Db, name: &str) -> Report {
    let live = db.collections();
    if !live.iter().any(|c| c == name) {
        // "Never existed" and "dropped" are different answers, and the engine
        // can tell them apart, so the CLI must not flatten them into one
        // not-found. A dropped collection is history, not an absence.
        let ever = db.collections_as_of(head_seq(db)).iter().any(|c| c == name)
            || db.list_ids_including_deleted(name).len() > 0;
        let detail = if ever {
            "it is not currently live — it was dropped, or never registered under this exact name"
        } else {
            "no collection by that name has existed in this database"
        };
        return Report::new(
            Exit::NotFound,
            json!({ "error": "collection not live", "collection": name, "detail": detail }),
            format!("not found: {}\n{}", name, detail),
        );
    }

    let ids = db.list_ids_including_deleted(name);
    let live_rows = db.list(name);
    let human = format!(
        "collection    {}\nlive rows     {}\nids seen      {}  (live + graveyard)",
        name,
        live_rows.len(),
        ids.len()
    );
    Report::ok(
        json!({
            "collection": name,
            "live_rows": live_rows.len(),
            "ids_including_deleted": ids.len(),
        }),
        human,
    )
}

fn document(db: &Db, coll: &str, id: &str) -> Report {
    match db.get(coll, id) {
        Some(n) => {
            let human = format!(
                "collection    {}\nid            {}\nseq           {}\nhash          {}\nprev          {}\ncaused_by     {}\nvalid         {} .. {}\n\n{}",
                n.coll, n.id, n.seq, n.hash,
                n.prev.clone().unwrap_or_else(|| "—".into()),
                if n.caused_by.is_empty() { "—".to_string() } else { n.caused_by.join(", ") },
                n.valid_from.clone().unwrap_or_else(|| "—".into()),
                n.valid_to.clone().unwrap_or_else(|| "—".into()),
                serde_json::to_string_pretty(&n.data).unwrap_or_default(),
            );
            Report::ok(json!({ "node": n }), human)
        }
        None => {
            // A deleted document is not a missing one. The graveyard keeps its
            // history addressable, so say so and point at the way to read it.
            let deleted = db.list_ids_including_deleted(coll).iter().any(|x| x == id);
            let detail = if deleted {
                "it was deleted; its history is still reachable with `nesql inspect seq <n>` \
                 at a sequence before the delete"
            } else {
                "no document with that id has existed in this collection"
            };
            Report::new(
                Exit::NotFound,
                json!({ "error": "document not live", "collection": coll, "id": id,
                        "deleted": deleted, "detail": detail }),
                format!("not found: {}/{}\n{}", coll, id, detail),
            )
        }
    }
}

fn sequence(db: &Db, seq: u64) -> Report {
    let head = head_seq(db);
    if seq > head {
        return Report::new(
            Exit::NotFound,
            json!({ "error": "sequence is past the head", "seq": seq, "head": head }),
            format!("not found: sequence {} is past the head ({})", seq, head),
        );
    }
    let floor = db.history_floor();
    if seq < floor {
        return Report::new(
            Exit::Indeterminate,
            json!({ "error": "HISTORY_PRUNED", "seq": seq, "history_floor": floor }),
            format!(
                "indeterminate: history below {} was discarded by a compaction, so the \
                 state at {} cannot be reconstructed\n(exit 3 — the material is gone, \
                 nothing is wrong)",
                floor, seq
            ),
        );
    }

    let colls = db.collections_as_of(seq);
    let root = db.state_root_as_of(seq);
    let (root_val, root_err) = match root {
        Ok(r) => (Some(r), None),
        Err(e) => (None, Some(e)),
    };
    let mut human = format!(
        "seq           {}\ncollections   {}",
        seq,
        if colls.is_empty() { "—".to_string() } else { colls.join(", ") }
    );
    match (&root_val, &root_err) {
        (Some(r), _) => {
            human.push_str(&format!(
                "\nstate root    {}\ncounts        {} collections · {} records",
                r.state_root, r.collection_count, r.record_count
            ));
        }
        (None, Some(e)) => {
            human.push_str(&format!("\nstate root    could not be determined: {}", e));
        }
        (None, None) => {}
    }
    let persisted = db.get_root(seq);
    if persisted.is_some() {
        human.push_str("\npersisted     yes (a root record exists at this sequence)");
    }

    Report::new(
        if root_err.is_some() { Exit::Indeterminate } else { Exit::Ok },
        json!({
            "seq": seq,
            "collections": colls,
            "state_root": root_val,
            "state_root_error": root_err,
            "persisted_root": persisted,
            "next_seq": db.seq.load(Ordering::SeqCst),
        }),
        human,
    )
}
