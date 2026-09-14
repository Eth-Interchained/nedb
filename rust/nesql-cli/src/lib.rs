// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! neSQL — the grammar and the command-line surface.
//!
//! neSQL owns the grammar AND the CLI; NEDB is the engine and runtime beneath
//! it. Nothing in here reaches into engine internals: every fact reported comes
//! from the public API of the `nedb-engine` crate, so the CLI cannot drift into
//! reporting a private detail that the engine is free to change.
//!
//! Layout:
//!
//! ```text
//! main.rs      argv in, exit code out, and nothing else
//! args.rs      parsing and intent resolution (total, refusing)
//! grammar.rs   the grammar surface, written down, plus its digest
//! out.rs       Report -> text, and the exit-code table
//! cmd/*.rs     one module per command; each returns a Report
//! ```

pub mod args;
pub mod cmd;
pub mod grammar;
pub mod out;

use std::path::PathBuf;

/// Where the database lives, given `--db`.
///
/// `--db`, else `$NEDB_PATH`, else `./nedb-data`. Resolution is here rather
/// than in the parser so the parser stays a pure function of its arguments.
pub fn db_path(flag: Option<&PathBuf>) -> PathBuf {
    if let Some(p) = flag {
        return p.clone();
    }
    match std::env::var_os("NEDB_PATH") {
        Some(v) if !v.is_empty() => PathBuf::from(v),
        _ => PathBuf::from("./nedb-data"),
    }
}

/// The CLI's own version. One of the two dimensions `nesql version` reports.
pub const CLI_VERSION: &str = env!("CARGO_PKG_VERSION");

/// The engine version this binary was compiled against, or `unknown` when the
/// build could not determine it. See `build.rs` for why it is never guessed.
pub const ENGINE_VERSION: &str = env!("NESQL_ENGINE_VERSION");
