// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql version` — BOTH dimensions.
//!
//! neSQL and NEDB version independently: the language and its CLI are one
//! artifact, the engine is another, and a machine can have any pair installed.
//! Reporting a single number would force the reader to guess which one it was,
//! and they would guess the engine's, because that is the one that used to be
//! the only number.

use serde_json::json;

use crate::out::Report;

pub fn run() -> Report {
    let body = json!({
        "nesql": crate::CLI_VERSION,
        "engine": crate::ENGINE_VERSION,
        "grammar": crate::grammar::VERSION,
        "grammar_digest": crate::grammar::digest(),
    });
    let human = format!(
        "nesql    {}\nengine   {}\ngrammar  {} ({})",
        crate::CLI_VERSION,
        crate::ENGINE_VERSION,
        crate::grammar::VERSION,
        &crate::grammar::digest()[..16],
    );
    Report::ok(body, human)
}
