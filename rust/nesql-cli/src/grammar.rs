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
  query <neSQL> [--nql|--sql]   run neSQL: PostgreSQL SQL + NEDB's own
                                the dialect is chosen by the leading keyword
                                (NQL statements begin FROM; SQL begins SELECT,
                                INSERT, UPDATE, DELETE, EXPLAIN, WITH, SHOW,
                                SET, VALUES, TABLE, BEGIN, COMMIT, ROLLBACK)
                                --nql / --sql force one, to get that dialect's
                                own error instead of a routing error
  diff <FROM> <TO>              what changed between two sequences
  tag create <NAME> <SEQ> [--message M]
                                name a sequence, immutably
  tag inspect <NAME>            one tag
  tag list [--all]              every tag (--all includes deleted)
  tag delete <NAME>             remove a tag reference
  branch create <NAME> <SEQ>    a branch off a sequence
  branch put <NAME> <COLL> <ID> <JSON>
                                write into a branch's overlay
  branch rm <NAME> <COLL> <ID>  delete within a branch's overlay
  branch inspect <NAME>         one branch and its overlay
  branch list [--all]           every branch (--all includes abandoned)
  branch abandon <NAME>         stop a branch without merging it
  merge plan <NAME>             what merging a branch would do, and conflicts
  merge execute <NAME>          replay a branch's overlay onto the trunk
  merge resolve <NAME> <COLL> <ID> (--ours|--theirs)
                                settle one conflict; the side is REQUIRED,
                                because there is no safe default for which
                                edit wins

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
            "query SELECT 1",
            "query --nql FROM users",
            "query --sql SELECT 1",
            "diff 1 2",
            "tag create v1 7",
            "tag create v1 7 --message shipped",
            "tag inspect v1",
            "tag list",
            "tag list --all",
            "tag delete v1",
            "branch create work 7",
            "branch put work orders 1 {}",
            "branch rm work orders 1",
            "branch inspect work",
            "branch list",
            "branch list --all",
            "branch abandon work",
            "merge plan work",
            "merge execute work",
            "merge resolve work orders 1 --ours",
            "merge resolve work orders 1 --theirs",
        ] {
            let argv: Vec<String> = line.split(' ').map(String::from).collect();
            assert!(
                args::parse(&argv).is_ok(),
                "the grammar documents {:?} but the parser refuses it",
                line
            );
        }
    }

    /// The COMMANDS block, which is what a reader scans for "can I do X".
    fn commands_section() -> &'static str {
        let from = SPEC.find("COMMANDS").expect("the spec has a COMMANDS section");
        let rest = &SPEC[from..];
        let to = rest.find("\nTHINGS").unwrap_or(rest.len());
        &rest[..to]
    }

    /// The MISSING HALF of `grammar_spec_matches_parser`, and the reason this
    /// module shipped a fiction in v6.0.0.
    ///
    /// That test asks "does everything the spec documents still parse?" — it
    /// walks SPEC -> parser. Nothing walked parser -> SPEC, so when `diff`,
    /// `tag`, `branch` and `merge` were wired, the spec went on calling them
    /// "reserved; not yet wired" and no test could tell. The digest did not
    /// help either: it is over the SPEC BYTES, so a spec that stopped being
    /// true without being edited keeps its digest. v6.0.0's grammar digest was
    /// byte-identical to v5.0.1's for exactly that reason.
    ///
    /// This walks the other way: every command the PARSER accepts must appear
    /// in the documented command surface.
    #[test]
    fn every_command_the_parser_accepts_is_documented() {
        let cmds = commands_section();
        for line in [
            "status", "log", "inspect users", "root create", "root inspect",
            "root verify", "root list", "grammar", "constitution", "version",
            "query FROM users", "diff 1 2", "tag create v1 7", "tag inspect v1",
            "tag list", "tag delete v1", "branch create work 7",
            "branch put work orders 1 {}", "branch rm work orders 1",
            "branch inspect work", "branch list", "branch abandon work",
            "merge plan work", "merge execute work",
            "merge resolve work orders 1 --ours",
        ] {
            let argv: Vec<String> = line.split(' ').map(String::from).collect();
            let parsed = args::parse(&argv)
                .unwrap_or_else(|e| panic!("{:?} should parse: {}", line, e.0));
            let name = parsed.command.name();
            // A line must START with the command, not merely contain it.
            // Substring matching is not enough and this test learned it the
            // hard way: the v6.0.0 spec line
            //     diff, tag, branch, merge      reserved; not yet wired
            // CONTAINS all four verb names, so a `cmds.contains(name)` check
            // passes on precisely the text that was wrong. Requiring the name
            // in the leading position means the verb has to have its own entry.
            let documented = cmds.lines().any(|l| {
                let t = l.trim_start();
                t.strip_prefix(name)
                    .is_some_and(|rest| rest.is_empty() || rest.starts_with(' '))
            });
            assert!(
                documented,
                "the parser accepts {:?} (command {:?}) but no line in the spec's \
                 COMMANDS section begins with {:?} -- `nesql grammar` is \
                 understating the binary",
                line,
                name,
                name
            );
        }
    }

    /// A command that works must not be advertised as one that does not.
    ///
    /// Stated as its own test because it is the specific claim that was false:
    /// the spec described four working verbs as unwired, which is worse than
    /// omitting them. Someone reading `nesql grammar` would not have tried
    /// them.
    #[test]
    fn the_spec_never_calls_a_working_command_unwired() {
        for (verb, example) in [
            ("diff", "diff 1 2"),
            ("tag", "tag list"),
            ("branch", "branch list"),
            ("merge", "merge plan work"),
        ] {
            let argv: Vec<String> = example.split(' ').map(String::from).collect();
            if args::parse(&argv).is_err() {
                continue; // genuinely not wired; the spec may say so
            }
            for claim in ["not yet wired", "reserved"] {
                let offending = SPEC
                    .lines()
                    .find(|l| l.contains(claim) && l.contains(verb));
                assert!(
                    offending.is_none(),
                    "{:?} parses, so it is wired, but the spec says: {:?}",
                    example,
                    offending.unwrap().trim()
                );
            }
        }
    }

    /// `--nql` and `--sql` were listed in `is_known_flag` and explained in
    /// `cmd::query`, while the `query` arm called `no_flags` and refused them.
    /// A documented flag that exits 2 is a worse defect than an undocumented
    /// one, because the user believes the fault is theirs.
    #[test]
    fn the_dialect_flags_the_spec_documents_are_accepted() {
        use crate::args::{Command, Dialect};
        let want = [("--nql", Dialect::Nql), ("--sql", Dialect::Sql)];
        for (flag, expect) in want {
            assert!(
                SPEC.contains(flag),
                "{} is accepted but undocumented",
                flag
            );
            let argv: Vec<String> = vec!["query".into(), flag.into(), "FROM users".into()];
            match args::parse(&argv).expect("the flag must be accepted").command {
                Command::Query { dialect, .. } => assert_eq!(
                    dialect,
                    Some(expect),
                    "{} must force {:?}",
                    flag,
                    expect
                ),
                other => panic!("{} parsed as {:?}", flag, other.name()),
            }
        }
        // Neither flag: routing decides, which must be recorded as "no dialect
        // was named" rather than defaulted to one.
        let argv: Vec<String> = vec!["query".into(), "FROM users".into()];
        match args::parse(&argv).unwrap().command {
            Command::Query { dialect, .. } => assert_eq!(dialect, None),
            other => panic!("parsed as {:?}", other.name()),
        }
    }

    /// The spec must name neSQL, not NQL alone. `query` has answered Postgres
    /// SQL since the pgwire merge; describing it as an NQL-only command sends
    /// the reader to the wrong syntax.
    #[test]
    fn the_spec_describes_query_as_neql() {
        let q = commands_section()
            .lines()
            .find(|l| l.trim_start().starts_with("query "))
            .expect("the spec documents query");
        assert!(
            q.contains("neSQL"),
            "the query line still describes only one half of the language: {:?}",
            q.trim()
        );
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
