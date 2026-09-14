// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! One module per command, plus the dispatch table.
//!
//! Every command has the same shape — `(&Db, args) -> Report` for the ones that
//! touch a database, `(args) -> Report` for the ones that answer from the
//! binary. That uniformity is what lets `diff`, `tag`, `branch` and `merge` slot
//! in later by filling in a match arm: [`dispatch`] already routes them, and
//! [`not_wired`] already produces the right refusal, so wiring one up means
//! replacing one call with one module.

pub mod constitution;
pub mod grammar;
pub mod inspect;
pub mod log;
pub mod query;
pub mod root;
pub mod status;
pub mod version;

use std::path::Path;
use std::sync::Arc;

use nedb_engine::Db;

use crate::args::{Command, NotWired, RootCmd};
use crate::out::{Exit, Report};

/// Open the database a command was pointed at.
///
/// `Db::open` calls `create_dir_all`, so opening a path that does not exist
/// would CREATE an empty database and then truthfully report that it has no
/// collections. That answer is indistinguishable from "your database is
/// empty", and it is reached by a command that was only ever asked to read. So
/// existence is checked first and a missing path is exit 4.
pub fn open_db(path: &Path) -> Result<Arc<Db>, Report> {
    if !path.exists() {
        return Err(Report::new(
            Exit::NotFound,
            serde_json::json!({
                "error": format!("no database at {}", path.display()),
                "path": path.display().to_string(),
            }),
            format!(
                "not found: no database at {}\n\
                 (nothing was created; pass --db <path> or set NEDB_PATH)",
                path.display()
            ),
        ));
    }
    if !path.is_dir() {
        return Err(Report::new(
            Exit::Failure,
            serde_json::json!({
                "error": format!("{} is not a directory", path.display()),
                "path": path.display().to_string(),
            }),
            format!("error: {} is not a directory", path.display()),
        ));
    }
    Db::open(path, None).map(Arc::new).map_err(|e| {
        Report::new(
            Exit::Failure,
            serde_json::json!({
                "error": e.to_string(),
                "path": path.display().to_string(),
            }),
            format!("error: could not open {}: {}", path.display(), e),
        )
    })
}

/// The refusal for a command that the grammar reserves but this build does not
/// implement.
///
/// Exit 2, not 1: the command line named something this build cannot do, which
/// is the same class of problem as naming a command that does not exist. Exit 0
/// would be a lie and exit 1 would suggest the operation was attempted.
pub fn not_wired(which: NotWired) -> Report {
    Report::new(
        Exit::Usage,
        serde_json::json!({
            "error": format!("{}: not yet wired", which.name()),
            "command": which.name(),
            "reserved": true,
        }),
        format!(
            "usage: {0}: not yet wired\n\
             `{0}` is reserved in grammar_v1 and will be implemented against the \
             engine API; this build does not implement it.",
            which.name()
        ),
    )
}

/// Route a resolved command.
///
/// `path` is passed alongside the open database because the database's own root
/// directory is not part of the engine's public API, and `status` has to be able
/// to say WHICH database it just described.
pub fn dispatch(command: &Command, db: &Arc<Db>, path: &Path) -> Report {
    match command {
        Command::Status => status::run(db, path),
        Command::Log { limit, since } => log::run(db, *limit, *since),
        Command::Inspect(thing) => inspect::run(db, thing),
        Command::Root(RootCmd::Create { at }) => root::create(db, *at),
        Command::Root(RootCmd::Inspect { at }) => root::inspect(db, *at),
        Command::Root(RootCmd::Verify { at }) => root::verify(db, *at),
        Command::Root(RootCmd::List) => root::list(db),
        Command::Constitution => constitution::run(db),
        Command::Query(q) => query::run(db, q),

        // Answered from the binary; routed here too so that `dispatch` is total
        // over `Command` and a new variant cannot be forgotten.
        Command::Grammar => grammar::run(),
        Command::Version => version::run(),
        Command::Help => grammar::help(),
        Command::NotWired(w) => not_wired(*w),
    }
}

/// The commands that answer without a database. Kept separate so `main` can
/// run them before deciding whether a database exists.
pub fn dispatch_dbless(command: &Command) -> Option<Report> {
    match command {
        Command::Grammar => Some(grammar::run()),
        Command::Version => Some(version::run()),
        Command::Help => Some(grammar::help()),
        Command::NotWired(w) => Some(not_wired(*w)),
        _ => None,
    }
}
