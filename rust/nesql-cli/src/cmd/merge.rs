// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql merge` — plan, execute, resolve.
//!
//! `plan` and `execute` are separate commands because they are separate
//! decisions. Planning writes nothing and can be read; executing replays the
//! branch as new destination history. A single verb that did both would make
//! the review step something you opt out of rather than something you do.

use nedb_engine::conflict::{self, Resolution};
use nedb_engine::merge as engine;
use nedb_engine::{branch, Db};
use serde_json::json;

use crate::args::{MergeCmd, Side};
use crate::out::{Exit, Report};

pub fn run(db: &Db, cmd: &MergeCmd) -> Report {
    match cmd {
        MergeCmd::Plan { branch } => plan(db, branch),
        MergeCmd::Execute { branch } => execute(db, branch),
        MergeCmd::Resolve { branch, coll, id, side } => resolve(db, branch, coll, id, *side),
    }
}

fn render_plan(p: &engine::MergePlan) -> String {
    let mut s = format!("merge {} (forked at {}, into {})\n", p.branch, p.base_seq, p.into_seq);
    if p.is_empty() {
        s.push_str("  nothing to do");
        return s;
    }
    for c in &p.changes {
        s.push_str(&format!("  {:?} {}/{}\n", c.kind, c.coll, c.id));
    }
    for c in &p.conflicts {
        s.push_str(&format!("  CONFLICT {:?} {}/{}\n", c.kind, c.coll, c.id));
    }
    s.push_str(&format!("  {} change(s), {} conflict(s)", p.changes.len(), p.conflicts.len()));
    if !p.is_clean() {
        s.push_str(
            "\n\nexecute is refused while conflicts remain. settle each with\n nesql merge resolve <BRANCH> <COLL> <ID> --ours|--theirs");
    }
    s
}

fn plan(db: &Db, name: &str) -> Report {
    match engine::plan(db, name) {
        Err(e) => Report::new(
            Exit::Usage, json!({ "error": e.to_string(), "branch": name }),
            format!("usage: {}", e)),
        Ok(p) => {
            // Conflicts are not a failure of the plan — producing them IS the
            // plan working. But a caller scripting "can I merge this" needs to
            // tell the two apart without parsing prose, so an unclean plan
            // exits 1 while still returning the whole plan as its payload.
            let exit = if p.is_clean() { Exit::Ok } else { Exit::Failure };
            let human = render_plan(&p);
            Report::new(exit, json!({ "plan": p, "clean": p.is_clean() }), human)
        }
    }
}

fn execute(db: &Db, name: &str) -> Report {
    let p = match engine::plan(db, name) {
        Err(e) => return Report::new(
            Exit::Usage, json!({ "error": e.to_string(), "branch": name }),
            format!("usage: {}", e)),
        Ok(p) => p,
    };
    if !p.is_clean() {
        return Report::new(
            Exit::Failure,
            json!({ "error": "unresolved conflicts", "plan": p }),
            format!("{}\n\nrefusing to merge with unresolved conflicts", render_plan(&p)),
        );
    }
    match engine::execute(db, &p) {
        Err(e) => Report::err(Exit::Failure, e.to_string()),
        Ok(rec) => Report::ok(
            json!({ "merge": rec }),
            format!(
                "merged {} at seq {}\n  replayed    {} change(s) as NEW destination writes\n  state root  {}\n  the branch's own nodes are untouched; the pre-merge values remain readable with AS OF",
                rec.branch, rec.merged_at_seq, rec.replayed,
                rec.state_root.clone().unwrap_or_else(|| "—".into()),
            ),
        ),
    }
}

fn resolve(db: &Db, name: &str, coll: &str, id: &str, side: Side) -> Report {
    let p = match engine::plan(db, name) {
        Err(e) => return Report::new(
            Exit::Usage, json!({ "error": e.to_string() }), format!("usage: {}", e)),
        Ok(p) => p,
    };
    let Some(c) = p.conflicts.iter().find(|c| c.coll == coll && c.id == id) else {
        // Say which conflicts DO exist. "Not found" without the alternatives
        // is a dead end when the caller has simply mistyped an id.
        let open: Vec<String> = p.conflicts.iter()
            .map(|c| format!("{}/{}", c.coll, c.id)).collect();
        return Report::new(
            Exit::NotFound,
            json!({ "error": "no such open conflict", "branch": name,
                    "coll": coll, "id": id, "open_conflicts": open }),
            format!(
                "not found: {} has no open conflict on {}/{}\n{}",
                name, coll, id,
                if open.is_empty() { "  (it has no open conflicts at all)".to_string() }
                else { format!("  open: {}", open.join(", ")) },
            ),
        );
    };
    let choice = match side { Side::Ours => Resolution::TakeOurs, Side::Theirs => Resolution::TakeTheirs };
    match conflict::resolve(db, c, choice) {
        Err(e) => Report::err(Exit::Failure, e.to_string()),
        Ok(()) => {
            let gen = branch::get_branch(db, name).map(|b| b.created_seq).unwrap_or(0);
            Report::ok(
                json!({ "resolved": { "branch": name, "generation": gen,
                                      "coll": coll, "id": id,
                                      "side": match side { Side::Ours => "ours", Side::Theirs => "theirs" } } }),
                format!(
                    "resolved {}/{} in favour of {}\n  the decision is recorded against {} generation {} — it settles THIS branch and no other",
                    coll, id,
                    match side { Side::Ours => "the destination", Side::Theirs => "the branch" },
                    name, gen,
                ),
            )
        }
    }
}
