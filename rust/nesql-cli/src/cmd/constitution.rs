// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! `nesql constitution` — what the engine guarantees, and whether this CLI and
//! that engine can actually work together.
//!
//! The check is CLI-against-ENGINE, deliberately. A command that hashed this
//! binary's own bundled grammar and printed "verified" would have proved only
//! that it can read its own disk — which is the exact failure this is designed
//! to make impossible. So the claim is built from what THIS build implements
//! and handed to the ENGINE to judge.

use nedb_engine::constitution as engine;
use nedb_engine::Db;
use serde_json::json;

use crate::out::{Exit, Report};

/// What this CLI requires of an engine.
///
/// The capability list is what the implemented commands actually touch — not
/// everything the engine can do. Requiring more than you use turns a harmless
/// version skew into a refusal to start.
fn claim() -> engine::ClientClaim {
    engine::ClientClaim {
        client_name: "nesql".to_string(),
        client_version: crate::CLI_VERSION.to_string(),
        grammar_digest: crate::grammar::digest(),
        required_capabilities: vec![
            "state_root.compute".to_string(),
            "state_root.as_of".to_string(),
            "root.persist".to_string(),
            "root.verify.three_state".to_string(),
            "history.as_of".to_string(),
            "history.floor".to_string(),
            "collections.registry".to_string(),
            "replication.since".to_string(),
            "nql.from".to_string(),
        ],
        required_formats: vec![
            engine::FormatVersion { name: "state_root", version: 1 },
            engine::FormatVersion { name: "root_record", version: 1 },
            engine::FormatVersion { name: "collection_registry", version: 1 },
        ],
    }
}

pub fn run(_db: &Db) -> Report {
    let c = engine::constitution();
    let compat = engine::check_compatibility(&claim());

    let (exit, verdict, detail) = match &compat {
        engine::Compatibility::Compatible => {
            (Exit::Ok, "compatible".to_string(), Vec::new())
        }
        engine::Compatibility::CompatibleWithGaps { client_missing, engine_missing } => {
            // A gap is NOT an error. Additive change must not break an old
            // client, and a subset client is a legitimate client.
            let mut d = Vec::new();
            for m in engine_missing {
                d.push(format!("engine does not advertise: {}", m));
            }
            for m in client_missing {
                d.push(format!("this build did not ask for: {}", m));
            }
            (Exit::Ok, "compatible with gaps".to_string(), d)
        }
        engine::Compatibility::Incompatible { reasons } => {
            (Exit::Unsupported, "INCOMPATIBLE".to_string(), reasons.clone())
        }
    };

    let mut human = format!(
        "engine         {}\nnesql          {}\nverdict        {}\n",
        c.engine_version, crate::CLI_VERSION, verdict
    );
    human.push_str(&format!("constitution   {}\n", engine::digest()));
    human.push_str(&format!(
        "grammar        engine {}\n               nesql  {}\n",
        &c.grammar_digest, crate::grammar::digest()
    ));
    if c.grammar_digest != crate::grammar::digest() {
        human.push_str(
            "               (digests differ — a subset client is legitimate, so this \
             alone is a gap, not a refusal)\n",
        );
    }
    if !detail.is_empty() {
        human.push('\n');
        for d in &detail {
            human.push_str(&format!("  · {}\n", d));
        }
    }
    human.push_str(&format!("\nformats        {}\n", c.formats.len()));
    for f in &c.formats {
        human.push_str(&format!("  {} v{}\n", f.name, f.version));
    }
    human.push_str(&format!("\ninvariants     {}\n", c.invariants.len()));
    for i in &c.invariants {
        human.push_str(&format!("  {}\n    {}\n", i.id, i.statement));
    }
    human.push_str(&format!("\ncapabilities   {}\n  {}", c.capabilities.len(),
        c.capabilities.join("\n  ")));

    Report::new(
        exit,
        json!({
            "verdict": verdict,
            "detail": detail,
            "compatibility": compat,
            "constitution": c,
            "constitution_digest": engine::digest(),
            "client": claim(),
        }),
        human,
    )
}
