// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql` — argv in, exit code out.
//!
//! Command tests exercise the library's reports; process tests cover argument
//! failures, stdout, stderr, and exit codes at this boundary.

use std::process::ExitCode;

use nesql::args;
use nesql::cmd;
use nesql::out::{Exit, Format, Report};

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();

    // Parse before anything else, and in particular before touching the
    // filesystem. A malformed command line must not be able to create,
    // open, or lock a database on its way to being refused.
    let inv = match args::parse(&argv) {
        Ok(i) => i,
        Err(e) => {
            let report = Report::usage(e.0);
            if args::usage_is_json(&argv) {
                // No command was resolved. Use a stable parse-stage name,
                // and the same envelope as errors after command dispatch.
                println!("{}", report.render("parse", Format::Json));
            } else {
                eprintln!("{}", report.human);
            }
            return ExitCode::from(Exit::Usage.code() as u8);
        }
    };

    let format = if inv.json { Format::Json } else { Format::Human };
    let name = inv.command.name();

    // Commands that answer from the binary run without a database, so
    // `nesql version` works in a directory that has none.
    let report = match cmd::dispatch_dbless(&inv.command) {
        Some(r) => r,
        None => {
            let path = nesql::db_path(inv.db.as_ref());
            match cmd::open_db(&path) {
                Err(r) => r,
                Ok(db) => cmd::dispatch(&inv.command, &db, &path),
            }
        }
    };

    let rendered = report.render(name, format);
    // Results go to stdout even when the outcome is bad, because the outcome
    // IS the result: `root verify` reporting a mismatch has answered the
    // question it was asked. The exit code carries the verdict; stderr is
    // reserved for the CLI failing to do its job at all.
    println!("{}", rendered);
    ExitCode::from(report.exit.code() as u8)
}
