// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql root` — create, inspect, verify, list.
//!
//! `root` earns a command family rather than a flag because the four verbs have
//! materially different cost and meaning. `create` at the tip walks live state;
//! `create --at` in the past also walks a version chain per document, which is
//! why backfill is spelled out instead of inferred. `verify` may or may not be
//! able to do its job at all, and says which.

use nedb_engine::root::{Recomputation, RecordStatus, UnavailableReason};
use nedb_engine::Db;
use serde_json::json;

use crate::cmd::status::head_seq;
use crate::out::{Exit, Report};

/// `nesql root create [--at SEQ]`
pub fn create(db: &Db, at: Option<u64>) -> Report {
    let head = head_seq(db);
    let target = at.unwrap_or(head);

    // A root for a sequence that has not happened is not a root. Refused here
    // rather than deeper, so the message can name both numbers.
    if target > head {
        return Report::new(
            Exit::NotFound,
            json!({ "error": "sequence is past the head", "at": target, "head": head }),
            format!(
                "not found: sequence {} is past the head ({})\n\
                 a root names a state the database has actually been in",
                target, head
            ),
        );
    }
    if target < db.history_floor() {
        return Report::new(
            Exit::Indeterminate,
            json!({
                "error": "HISTORY_PRUNED", "at": target, "history_floor": db.history_floor(),
            }),
            format!(
                "indeterminate: cannot compute a root at {} — history below {} was \
                 discarded by a compaction\n(exit 3 — nothing is wrong, the material \
                 is simply gone)",
                target, db.history_floor()
            ),
        );
    }

    let backfilling = target < head;
    match db.create_root_at(target) {
        Err(e) => Report::err(Exit::Failure, e.to_string()),
        Ok(rec) => {
            let human = format!(
                "{} root at seq {}\n  state root    {}\n  namespace     {}\n  records       {}\n  counts        {} collections · {} records",
                if backfilling { "backfilled" } else { "created" },
                rec.at_seq,
                rec.root.state_root,
                rec.root.namespace_root,
                rec.root.records_root,
                rec.root.collection_count,
                rec.root.record_count,
            );
            Report::ok(json!({ "backfilled": backfilling, "root": rec }), human)
        }
    }
}

/// `nesql root inspect [SEQ]` — the most recent persisted root when SEQ is absent.
pub fn inspect(db: &Db, at: Option<u64>) -> Report {
    let roots = db.list_roots();
    let rec = match at {
        Some(seq) => db.get_root(seq),
        None => roots.last().cloned(),
    };
    match rec {
        None => {
            let what = match at {
                Some(seq) => format!("no root record at sequence {}", seq),
                None => "no root has been persisted in this database".to_string(),
            };
            Report::new(
                Exit::NotFound,
                json!({ "error": what, "persisted_root_count": roots.len() }),
                format!("not found: {}\n(run `nesql root create`)", what),
            )
        }
        Some(rec) => {
            let human = format!(
                "seq           {}\nversion       {}\nstate root    {}\n  namespace   {}\n  records     {}\ncounts        {} collections · {} records",
                rec.at_seq, rec.root.version, rec.root.state_root,
                rec.root.namespace_root, rec.root.records_root,
                rec.root.collection_count, rec.root.record_count,
            );
            Report::ok(json!({ "root": rec }), human)
        }
    }
}

/// `nesql root verify [SEQ]`
///
/// Reports TWO INDEPENDENT FACTS and never collapses them: whether the stored
/// record is intact, and whether the history needed to recompute it is still
/// here. A pruned database is not a corrupt one, and an operator who cannot
/// tell those apart will either ignore a real alarm or panic at a routine one.
pub fn verify(db: &Db, at: Option<u64>) -> Report {
    let target = match at {
        Some(seq) => seq,
        None => match db.list_roots().last() {
            Some(r) => r.at_seq,
            None => {
                return Report::new(
                    Exit::NotFound,
                    json!({ "error": "no root has been persisted in this database" }),
                    "not found: no root has been persisted in this database\n\
                     (run `nesql root create`)",
                )
            }
        },
    };

    let v = db.verify_root(target);
    // The engine already decided what this outcome is worth. Adopting its code
    // rather than re-deriving one keeps the CLI from inventing a second opinion.
    let exit = Exit::from_code(v.exit_code());

    let record_line = match &v.record {
        RecordStatus::Valid => "valid".to_string(),
        RecordStatus::Missing => "missing".to_string(),
        RecordStatus::UnknownVersion(ver) => format!("unknown format version {:?}", ver),
    };
    let (recomp_line, reason_line) = match &v.recomputation {
        Recomputation::Matches => ("matches".to_string(), None),
        Recomputation::Differs => ("DIFFERS".to_string(), None),
        Recomputation::NotAttempted => ("not attempted".to_string(), None),
        Recomputation::Unavailable(UnavailableReason::HistoryPruned) => (
            "unavailable".to_string(),
            Some("HISTORY_PRUNED".to_string()),
        ),
        Recomputation::Unavailable(UnavailableReason::Other(e)) => {
            ("unavailable".to_string(), Some(e.clone()))
        }
    };

    let mut human = format!(
        "at_seq         {}\nroot_record    {}\nrecomputation  {}",
        target, record_line, recomp_line
    );
    if let Some(r) = &reason_line {
        human.push_str(&format!("\nreason         {}", r));
    }
    human.push_str(&format!("\nexit           {}", exit.code()));
    if v.is_mismatch() {
        human.push_str(
            "\n\nthe recomputed root does not match the stored one. \
             this is the outcome that means something is wrong.",
        );
    } else if matches!(v.recomputation, Recomputation::Unavailable(_)) {
        human.push_str(
            "\n\nthe record is intact; the history needed to re-derive it is not. \
             this is neither a pass nor a failure.",
        );
    }

    Report::new(
        exit,
        json!({
            "at_seq": target,
            "verified": v.is_verified(),
            "mismatch": v.is_mismatch(),
            "root_record": record_line,
            "recomputation": recomp_line,
            "reason": reason_line,
            "result": v,
        }),
        human,
    )
}

/// `nesql root list`
pub fn list(db: &Db) -> Report {
    let roots = db.list_roots();
    let floor = db.history_floor();
    let human = if roots.is_empty() {
        "no roots persisted".to_string()
    } else {
        let mut s = format!("{} root(s), history floor {}\n", roots.len(), floor);
        for r in &roots {
            // Truncated in HUMAN output only. `--json` carries the full digest,
            // because a script that compares a prefix is comparing a prefix.
            s.push_str(&format!("  {:>12}  {}\n", r.at_seq, &r.root.state_root[..16]));
        }
        s.trim_end().to_string()
    };
    Report::ok(json!({ "history_floor": floor, "count": roots.len(), "roots": roots }), human)
}
