// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! The grammar the CLI implements, written down, plus a digest of it.
//!
//! neSQL owns the grammar; NEDB is the engine underneath it. This module is the
//! grammar's only definition inside the CLI — `args.rs` implements exactly what
//! is written here, and `nesql grammar` prints it. A caller that wants to know
//! whether two `nesql` builds accept the same commands compares digests instead
//! of diffing help text.
//!
//! The digest covers the SPEC BYTES, nothing else. It is a change detector for
//! the command surface, not a signature: it says "this is a different grammar
//! than the one you saw", which is the only claim a hash of a string can make.

/// Domain separation, so this digest can never collide with a state root or an
/// object hash computed over the same bytes.
const TAG: &[u8] = b"nesql:grammar_v1:";

/// The grammar surface, version 1.
///
/// Every production here is enforced by `args.rs`. The two are kept in step by
/// `grammar_spec_matches_parser` in this module's tests, which parses one
/// example of each command and fails when a documented form stops working.
pub const SPEC: &str = r#"nesql — grammar_v1

  nesql [GLOBAL]... <command> [ARGS]...

GLOBAL FLAGS  (accepted before or after the command; `--` ends flag parsing)
  --db <path>        the database directory
                     default: $NEDB_PATH, else ./nedb-data
  --json             machine output: exact, stable, one JSON object on stdout
  --human            human output (the default; stated explicitly)
  -h, --help         this grammar
  --version          alias for `nesql version`

COMMANDS
  status                        database summary: path, seq, collections,
                                state root, history floor
  log [--limit N] [--since SEQ] writes in (SEQ, head], newest-first
  inspect <thing>               one thing, named unambiguously (see THINGS)
  root create [--at SEQ]        persist a state root (default: current seq)
  root inspect [SEQ]            a persisted root record
                                no SEQ = the most recent persisted record;
                                for the LIVE root, use `nesql status`
  root verify [SEQ]             recompute a persisted root and compare
                                no SEQ = the most recent persisted record
  root list                     every persisted root record
  grammar                       this grammar surface and its digest
  constitution                  the engine's constitution + compatibility check
  version                       CLI version AND engine version
  query <NQL>                   run an NQL query

  diff, tag, branch, merge      reserved; not yet wired (exit 2)

THINGS  (the argument to `inspect`)
  A thing is named by KIND, because the kinds are not distinguishable by shape.

  collection:<name>   a collection          (alias: coll:)
  document:<coll>/<id>  one document        (alias: doc:)
  root:<seq>          a persisted root record
  seq:<n>             the write at one sequence number

  Two bare forms are accepted, and only because exactly one kind can be meant:
  <coll>/<id>         a document — a collection name cannot contain '/'
  <name>              a collection — every other kind needs a marker

  A bare number is REFUSED. `42` could be seq:42 or root:42, and the CLI does
  not pick one.

EXIT CODES
  0  success — the thing was done, or the check ran and passed
  1  failure — the operation ran and did not succeed
  2  usage   — the command line was not understood, or was ambiguous
  3  could not determine — the check could not run (e.g. history pruned).
                           NOT a failure, and deliberately not folded into one
  4  not found — the named thing does not exist
  5  unsupported — a version or format this build does not know
"#;

/// BLAKE2b-256 of the tagged spec, hex.
pub fn digest() -> String {
    use blake2::digest::{Update, VariableOutput};
    let mut h = blake2::Blake2bVar::new(32).expect("32 is a valid blake2b output length");
    h.update(TAG);
    h.update(SPEC.as_bytes());
    let mut out = [0u8; 32];
    h.finalize_variable(&mut out)
        .expect("output buffer is exactly the configured length");
    hex::encode(out)
}

/// `grammar_v1`. Bumped when the spec changes shape, not when wording changes;
/// the digest is what detects wording changes.
pub const VERSION: &str = "grammar_v1";

#[cfg(test)]
mod tests {
    use super::*;
    use crate::args;

    #[test]
    fn digest_is_stable_across_calls() {
        assert_eq!(digest(), digest());
        assert_eq!(digest().len(), 64);
    }

    #[test]
    fn digest_changes_when_the_spec_changes() {
        // Same function, different input — the property under test is that the
        // digest is over the spec bytes and not a constant.
        use blake2::digest::{Update, VariableOutput};
        let mut h = blake2::Blake2bVar::new(32).unwrap();
        h.update(TAG);
        h.update(b"nesql - a different grammar");
        let mut out = [0u8; 32];
        h.finalize_variable(&mut out).unwrap();
        assert_ne!(hex::encode(out), digest());
    }

    /// Every command the spec documents must actually parse. This is the link
    /// that keeps `nesql grammar` from becoming a fiction.
    #[test]
    fn grammar_spec_matches_parser() {
        for line in [
            "status",
            "log",
            "log --limit 10",
            "log --since 4",
            "log --limit 10 --since 4",
            "inspect collection:users",
            "inspect coll:users",
            "inspect document:users/u1",
            "inspect doc:users/u1",
            "inspect users/u1",
            "inspect users",
            "inspect root:3",
            "inspect seq:3",
            "root create",
            "root create --at 7",
            "root inspect",
            "root inspect 7",
            "root verify",
            "root verify 7",
            "root list",
            "grammar",
            "constitution",
            "version",
            "query FROM users",
        ] {
            let argv: Vec<String> = line.split(' ').map(String::from).collect();
            assert!(
                args::parse(&argv).is_ok(),
                "the grammar documents {:?} but the parser refuses it",
                line
            );
        }
    }

    #[test]
    fn spec_names_every_exit_code() {
        for code in ["0", "1", "2", "3", "4", "5"] {
            assert!(
                SPEC.lines().any(|l| l.trim_start().starts_with(code)),
                "exit code {} is undocumented",
                code
            );
        }
    }
}
