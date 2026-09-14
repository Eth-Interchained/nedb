// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql grammar` — the language surface this binary implements, and its digest.
//!
//! Printed rather than merely hashed. A digest a user cannot expand is a digest
//! they cannot act on when it mismatches, and "your grammar digest differs" is
//! only useful next to the thing that differs.

use serde_json::json;

use crate::out::Report;

pub fn run() -> Report {
    let digest = crate::grammar::digest();
    let body = json!({
        "version": crate::grammar::VERSION,
        "digest": digest,
        "spec": crate::grammar::SPEC,
    });
    let human = format!(
        "{}\n\nversion  {}\ndigest   {}",
        crate::grammar::SPEC,
        crate::grammar::VERSION,
        digest
    );
    Report::ok(body, human)
}

/// `nesql help`, and the bare invocation.
///
/// The same text as `grammar`, because there is exactly one description of the
/// command surface. Two would drift, and the one that drifted would be the one
/// the user read.
pub fn help() -> Report {
    run()
}
