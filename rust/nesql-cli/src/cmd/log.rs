// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql log` — the writes, newest first.

use nedb_engine::Db;
use serde_json::json;

use crate::out::{Exit, Report};

/// How many entries `log` shows when `--limit` is not given.
pub const DEFAULT_LIMIT: usize = 50;

pub fn run(db: &Db, limit: Option<usize>, since: Option<u64>) -> Report {
    let limit = limit.unwrap_or(DEFAULT_LIMIT);
    let floor = db.history_floor();
    let from = since.unwrap_or(0);

    // `--since` below the floor is REPORTED, not silently clamped. The page
    // returned starts wherever history actually begins, and a caller that asked
    // for sequence 5 of a database pruned to 900 must be told that the gap
    // exists rather than handed a page that looks complete.
    let below_floor = since.map(|s| s < floor).unwrap_or(false);

    let batch = db.since(from, limit);

    let entries: Vec<_> = batch
        .nodes
        .iter()
        .rev() // newest first: `log` is read top-down
        .map(|n| {
            json!({
                "seq": n.seq,
                "coll": n.coll,
                "id": n.id,
                "hash": n.hash,
                "prev": n.prev,
                "ts": n.ts,
                "caused_by": n.caused_by,
                "valid_from": n.valid_from,
                "valid_to": n.valid_to,
            })
        })
        .collect();

    let body = json!({
        "from_seq": batch.from_seq,
        "to_seq": batch.to_seq,
        "head_seq": batch.head_seq,
        "has_more": batch.has_more,
        "history_floor": floor,
        "since_below_floor": below_floor,
        "limit": limit,
        "count": entries.len(),
        "entries": entries,
    });

    let mut human = String::new();
    if below_floor {
        human.push_str(&format!(
            "note: --since {} is below the history floor {}; entries before the floor \
             were pruned and are not in this page\n\n",
            from, floor
        ));
    }
    if entries.is_empty() {
        human.push_str(&format!(
            "no writes in ({}, {}]",
            batch.from_seq, batch.head_seq
        ));
    } else {
        for e in &entries {
            human.push_str(&format!(
                "{:>8}  {}/{}  {}\n",
                e["seq"],
                e["coll"].as_str().unwrap_or("?"),
                e["id"].as_str().unwrap_or("?"),
                short_hash(e["hash"].as_str().unwrap_or("")),
            ));
        }
        human.push_str(&format!(
            "\n{} of head {}{}",
            entries.len(),
            batch.head_seq,
            if batch.has_more {
                format!(" · more available: --since {}", batch.to_seq)
            } else {
                String::new()
            }
        ));
    }

    // An empty page is not an error: a database with no writes has no writes.
    Report::new(Exit::Ok, body, human)
}

fn short_hash(h: &str) -> String {
    if h.len() > 12 {
        h[..12].to_string()
    } else {
        h.to_string()
    }
}
