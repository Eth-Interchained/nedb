// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! Records the ENGINE version this CLI was compiled against.
//!
//! `nesql version` reports two dimensions, and the second one has to come from
//! somewhere. Cargo hands a crate its OWN version (`CARGO_PKG_VERSION`) and
//! nothing about its dependencies, and the engine exports no version constant,
//! so the engine manifest is read here at build time.
//!
//! When that read fails — the manifest moved, or switched to
//! `version.workspace = true` — the value emitted is the literal `unknown`.
//! It is NOT filled in from this crate's own version: two crates that happen to
//! share a version number today are still two version numbers, and a CLI that
//! prints its own version under the engine's name would answer "what engine is
//! this?" confidently and wrongly. `unknown` is the honest answer, and
//! `nesql version` reports it as such.

use std::path::Path;

fn main() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("nedb-v2")
        .join("Cargo.toml");

    println!("cargo:rerun-if-changed={}", manifest.display());

    let version = std::fs::read_to_string(&manifest)
        .ok()
        .and_then(|text| engine_version(&text))
        .unwrap_or_else(|| "unknown".to_string());

    println!("cargo:rustc-env=NESQL_ENGINE_VERSION={}", version);
}

/// Pull `version = "x.y.z"` out of the `[package]` table, and only that table.
/// A `version` key under `[dependencies]` is a different fact.
fn engine_version(text: &str) -> Option<String> {
    let mut in_package = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            in_package = line == "[package]";
            continue;
        }
        if !in_package {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else { continue };
        if key.trim() != "version" {
            continue;
        }
        let value = value.trim().trim_end_matches(',');
        // `version.workspace = true` lands here as key "version.workspace" and
        // is skipped above; a bare `true` would mean the same thing.
        let quoted = value.strip_prefix('"')?.strip_suffix('"')?;
        if quoted.is_empty() {
            return None;
        }
        return Some(quoted.to_string());
    }
    None
}
