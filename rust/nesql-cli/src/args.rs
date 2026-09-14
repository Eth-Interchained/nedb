// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! Argument parsing and intent resolution.
//!
//! # Why this is hand-rolled
//!
//! A derive-macro argument parser makes "guess what the user meant" the easy
//! path: a positional argument gets a type, the type gets a `FromStr`, and the
//! first interpretation that parses wins. That is exactly the behaviour this
//! CLI must not have. `nesql inspect 42` is not a question with one answer —
//! `42` is a well-formed sequence number AND a well-formed root sequence — and
//! a parser whose structure rewards picking one will pick one.
//!
//! So intent resolution is written out, by hand, with two rules:
//!
//!   * **Total.** Every argument vector maps to exactly one outcome. There is
//!     no fallthrough, no "ignored trailing argument", no flag that is silently
//!     dropped for a command that does not take it.
//!   * **Refusing.** When an argument admits more than one well-formed reading,
//!     parsing FAILS and the message names the readings it would have had to
//!     choose between. A user who is told "42 could be seq:42 or root:42" can
//!     fix it in one keystroke. A user who is given the wrong answer cannot
//!     even tell.
//!
//! Every failure here is a usage error, and usage errors are exit 2 — see
//! [`crate::out::Exit`].

use std::path::PathBuf;

// ── The resolved intent ───────────────────────────────────────────────────

/// A thing `inspect` can be pointed at.
///
/// The kinds are not distinguishable by shape — a collection name, a document
/// id and a sequence number can all be the string `42` — so the variant is
/// decided by an explicit marker in the argument, never by probing the database
/// to see which one happens to exist. Resolution that depends on current
/// database contents would make the same command line mean different things on
/// two machines, which is the ambiguity, moved rather than removed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Thing {
    Collection(String),
    Document { coll: String, id: String },
    Root(u64),
    Seq(u64),
}

/// The `root` subcommands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RootCmd {
    /// `--at SEQ`, or the current sequence when absent.
    Create { at: Option<u64> },
    /// `SEQ`, or the most recent persisted record when absent.
    Inspect { at: Option<u64> },
    Verify { at: Option<u64> },
    List,
}

/// A command that is reserved in the grammar but not yet implemented.
///
/// Present as a variant rather than as an "unknown command" so the dispatch
/// table is already the right shape when the engine API lands: the work is
/// filling in an arm, not restructuring the parser. Exits 2 — a command that
/// does not do anything yet must not be mistaken for one that did nothing
/// because there was nothing to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotWired {
    Diff,
    Tag,
    Branch,
    Merge,
}

impl NotWired {
    pub fn name(self) -> &'static str {
        match self {
            NotWired::Diff => "diff",
            NotWired::Tag => "tag",
            NotWired::Branch => "branch",
            NotWired::Merge => "merge",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Status,
    Log { limit: Option<usize>, since: Option<u64> },
    Inspect(Thing),
    Root(RootCmd),
    Grammar,
    Constitution,
    Version,
    Query(String),
    Help,
    NotWired(NotWired),
}

impl Command {
    /// The name reported in JSON output. Stable.
    pub fn name(&self) -> &'static str {
        match self {
            Command::Status => "status",
            Command::Log { .. } => "log",
            Command::Inspect(_) => "inspect",
            Command::Root(RootCmd::Create { .. }) => "root create",
            Command::Root(RootCmd::Inspect { .. }) => "root inspect",
            Command::Root(RootCmd::Verify { .. }) => "root verify",
            Command::Root(RootCmd::List) => "root list",
            Command::Grammar => "grammar",
            Command::Constitution => "constitution",
            Command::Version => "version",
            Command::Query(_) => "query",
            Command::Help => "help",
            Command::NotWired(w) => w.name(),
        }
    }

    /// Does this command need a database on disk? `grammar`, `version` and
    /// `help` answer from the binary itself, and must work with no database
    /// present — a CLI that cannot tell you its own version without a database
    /// is useless in exactly the situation you need it.
    pub fn needs_db(&self) -> bool {
        !matches!(self, Command::Grammar | Command::Version | Command::Help | Command::NotWired(_))
    }
}

/// A complete command line, resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Invocation {
    /// `--db`, when given. Resolution against `$NEDB_PATH` and the default
    /// happens in [`crate::db_path`], not here, so the parser stays a pure
    /// function of its arguments and the tests need no environment.
    pub db: Option<PathBuf>,
    pub json: bool,
    pub command: Command,
}

/// Why a command line was refused. Always exit 2.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageError(pub String);

impl std::fmt::Display for UsageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

fn refuse<T>(msg: impl Into<String>) -> Result<T, UsageError> {
    Err(UsageError(msg.into()))
}

// ── Lexing ───────────────────────────────────────────────────────────────

/// Flags that take a value. Listed rather than inferred: a flag whose
/// value-ness depends on what follows it is a flag that changes meaning based
/// on its neighbour.
fn takes_value(flag: &str) -> bool {
    matches!(flag, "--db" | "--limit" | "--since" | "--at")
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Tok {
    Flag { name: String, value: Option<String> },
    Word(String),
}

/// Split the argument vector into flags and words.
///
/// `--name=value` and `--name value` are both accepted. `--` ends flag parsing:
/// everything after it is a word, which is how an NQL query containing a `--`
/// is passed through.
fn lex(argv: &[String]) -> Result<Vec<Tok>, UsageError> {
    let mut out = Vec::new();
    let mut i = 0;
    let mut flags_done = false;

    while i < argv.len() {
        let arg = &argv[i];
        i += 1;

        if flags_done {
            out.push(Tok::Word(arg.clone()));
            continue;
        }
        if arg == "--" {
            flags_done = true;
            continue;
        }
        if arg == "-h" {
            out.push(Tok::Flag { name: "--help".into(), value: None });
            continue;
        }
        if arg == "-" || !arg.starts_with('-') {
            out.push(Tok::Word(arg.clone()));
            continue;
        }
        if !arg.starts_with("--") {
            return refuse(format!(
                "unknown flag {:?}; neSQL has no short flags other than -h",
                arg
            ));
        }

        let (name, inline) = match arg.split_once('=') {
            Some((n, v)) => (n.to_string(), Some(v.to_string())),
            None => (arg.clone(), None),
        };

        if !is_known_flag(&name) {
            return refuse(format!("unknown flag {:?}", name));
        }

        if takes_value(&name) {
            let value = match inline {
                Some(v) => v,
                None => match argv.get(i) {
                    Some(v) => {
                        i += 1;
                        v.clone()
                    }
                    None => return refuse(format!("{} needs a value", name)),
                },
            };
            out.push(Tok::Flag { name, value: Some(value) });
        } else {
            if let Some(v) = inline {
                return refuse(format!("{} takes no value, but was given {:?}", name, v));
            }
            out.push(Tok::Flag { name, value: None });
        }
    }

    Ok(out)
}

fn is_known_flag(name: &str) -> bool {
    matches!(
        name,
        "--db" | "--json" | "--human" | "--help" | "--version" | "--limit" | "--since" | "--at"
    )
}

// ── Parsing ──────────────────────────────────────────────────────────────

/// The whole command line, or a usage error.
pub fn parse(argv: &[String]) -> Result<Invocation, UsageError> {
    let toks = lex(argv)?;

    // `query` takes free text, and free text can contain anything, so the
    // command word is found FIRST and `query` then claims every remaining
    // token verbatim. Doing it the other way round — lexing flags out of the
    // whole line and hoping none of them belonged to the query — is how a
    // query silently loses a clause.
    let mut db: Option<String> = None;
    let mut json: Option<bool> = None;
    let mut human = false;
    let mut help = false;
    let mut version_flag = false;
    let mut command_word: Option<String> = None;
    let mut words: Vec<String> = Vec::new();
    let mut flags: Vec<(String, String)> = Vec::new();

    for tok in toks {
        match tok {
            Tok::Word(w) => {
                if command_word.is_none() {
                    command_word = Some(w);
                } else {
                    words.push(w);
                }
            }
            Tok::Flag { name, value } => match (name.as_str(), value) {
                ("--json", _) => {
                    if json == Some(true) {
                        return refuse("--json given twice");
                    }
                    json = Some(true);
                }
                ("--human", _) => {
                    if human {
                        return refuse("--human given twice");
                    }
                    human = true;
                }
                ("--help", _) => help = true,
                ("--version", _) => version_flag = true,
                ("--db", Some(v)) => {
                    if v.is_empty() {
                        return refuse("--db was given an empty path");
                    }
                    if let Some(prev) = &db {
                        if prev != &v {
                            return refuse(format!(
                                "--db given twice, as {:?} and {:?}; the CLI will not \
                                 pick one database over another",
                                prev, v
                            ));
                        }
                        return refuse("--db given twice");
                    }
                    db = Some(v);
                }
                (other, Some(v)) => flags.push((other.to_string(), v)),
                (other, None) => {
                    return refuse(format!("{} needs a value", other));
                }
            },
        }
    }

    if json == Some(true) && human {
        return refuse(
            "--json and --human both given; they select different output formats and \
             the CLI will not choose between them",
        );
    }
    let json = json.unwrap_or(false);
    let db = db.map(PathBuf::from);

    // `--help` wins over everything: someone who asks what the commands are
    // should not have their malformed command line diagnosed instead.
    if help {
        return Ok(Invocation { db, json, command: Command::Help });
    }

    let command = match command_word.as_deref() {
        None => {
            if version_flag {
                Command::Version
            } else {
                // No command at all is a usage error, not an implicit `status`.
                // Defaulting here would make a typo'd flag run a command.
                return refuse("no command given");
            }
        }
        Some(word) => {
            if version_flag && word != "version" {
                return refuse(format!(
                    "--version given with the command {:?}; --version is an alias for \
                     `nesql version` and cannot be combined with another command",
                    word
                ));
            }
            resolve(word, &words, &flags)?
        }
    };

    Ok(Invocation { db, json, command })
}

/// Map a command word plus its operands and command flags onto an intent.
fn resolve(
    word: &str,
    words: &[String],
    flags: &[(String, String)],
) -> Result<Command, UsageError> {
    match word {
        "status" => {
            no_words("status", words)?;
            no_flags("status", flags)?;
            Ok(Command::Status)
        }

        "log" => {
            if !words.is_empty() {
                // `log 20` is refused rather than read as a limit: it is just as
                // plausibly a starting sequence, and the two produce completely
                // different pages of history.
                return refuse(format!(
                    "log takes no positional argument, but got {:?}; a bare number \
                     could be --limit {} or --since {} — say which",
                    words[0], words[0], words[0]
                ));
            }
            let limit = match take_flag(flags, "--limit")? {
                Some(v) => Some(parse_usize("--limit", &v)?),
                None => None,
            };
            let since = match take_flag(flags, "--since")? {
                Some(v) => Some(parse_u64("--since", &v)?),
                None => None,
            };
            reject_unused(flags, &["--limit", "--since"], "log")?;
            if limit == Some(0) {
                return refuse("--limit 0 would ask for no entries; omit it to use the default");
            }
            Ok(Command::Log { limit, since })
        }

        "inspect" => {
            no_flags("inspect", flags)?;
            match words.len() {
                0 => refuse(
                    "inspect needs a thing: collection:<name>, document:<coll>/<id>, \
                     root:<seq>, or seq:<n>",
                ),
                1 => Ok(Command::Inspect(resolve_thing(&words[0])?)),
                _ => refuse(format!(
                    "inspect takes exactly one thing, but got {}: {}",
                    words.len(),
                    words.join(", ")
                )),
            }
        }

        "root" => {
            let sub = match words.first() {
                Some(s) => s.as_str(),
                None => {
                    return refuse(
                        "root needs a subcommand: create, inspect, verify, or list",
                    )
                }
            };
            let rest = &words[1..];
            match sub {
                "create" => {
                    no_words("root create", rest)?;
                    let at = match take_flag(flags, "--at")? {
                        Some(v) => Some(parse_u64("--at", &v)?),
                        None => None,
                    };
                    reject_unused(flags, &["--at"], "root create")?;
                    Ok(Command::Root(RootCmd::Create { at }))
                }
                "inspect" | "verify" => {
                    no_flags(&format!("root {}", sub), flags)?;
                    let at = match rest.len() {
                        0 => None,
                        1 => Some(parse_u64(&format!("root {} <SEQ>", sub), &rest[0])?),
                        _ => {
                            return refuse(format!(
                                "root {} takes at most one sequence, but got {}",
                                sub,
                                rest.len()
                            ))
                        }
                    };
                    Ok(Command::Root(if sub == "inspect" {
                        RootCmd::Inspect { at }
                    } else {
                        RootCmd::Verify { at }
                    }))
                }
                "list" => {
                    no_words("root list", rest)?;
                    no_flags("root list", flags)?;
                    Ok(Command::Root(RootCmd::List))
                }
                other => {
                    // A bare number here is the classic ambiguity: `root 7`
                    // reads equally well as inspect-7 and verify-7, and one of
                    // those recomputes hashes over history while the other just
                    // reads a record.
                    if other.chars().all(|c| c.is_ascii_digit()) {
                        return refuse(format!(
                            "root {0} is ambiguous: it could mean `root inspect {0}` or \
                             `root verify {0}`, and those answer different questions — \
                             say which",
                            other
                        ));
                    }
                    refuse(format!(
                        "root has no subcommand {:?}; it has create, inspect, verify, list",
                        other
                    ))
                }
            }
        }

        "grammar" => {
            no_words("grammar", words)?;
            no_flags("grammar", flags)?;
            Ok(Command::Grammar)
        }
        "constitution" => {
            no_words("constitution", words)?;
            no_flags("constitution", flags)?;
            Ok(Command::Constitution)
        }
        "version" => {
            no_words("version", words)?;
            no_flags("version", flags)?;
            Ok(Command::Version)
        }
        "help" => {
            no_flags("help", flags)?;
            Ok(Command::Help)
        }

        "query" => {
            no_flags("query", flags)?;
            if words.is_empty() {
                return refuse("query needs an NQL statement");
            }
            // Multiple words are joined with single spaces, so both
            // `query "FROM users LIMIT 1"` and `query FROM users LIMIT 1`
            // work. NQL is whitespace-insensitive between tokens, so the join
            // cannot change the statement's meaning — but a quoted string
            // literal containing runs of spaces would be reflowed, so the
            // one-argument form is the documented way to pass one.
            Ok(Command::Query(words.join(" ")))
        }

        "diff" => Ok(Command::NotWired(NotWired::Diff)),
        "tag" => Ok(Command::NotWired(NotWired::Tag)),
        "branch" => Ok(Command::NotWired(NotWired::Branch)),
        "merge" => Ok(Command::NotWired(NotWired::Merge)),

        other => refuse(format!(
            "no such command: {:?}; run `nesql grammar` for the command surface",
            other
        )),
    }
}

/// Resolve the argument to `inspect`.
///
/// Marked forms first, then the two bare forms that admit exactly one reading.
/// Anything else is refused with the readings named.
fn resolve_thing(arg: &str) -> Result<Thing, UsageError> {
    if let Some((kind, rest)) = split_marker(arg) {
        return match kind {
            "collection" | "coll" => {
                if rest.is_empty() {
                    return refuse(format!("{}: needs a collection name", kind));
                }
                if rest.contains('/') {
                    return refuse(format!(
                        "{0}:{1} contains '/', and a collection name cannot — did you \
                         mean document:{1}?",
                        kind, rest
                    ));
                }
                Ok(Thing::Collection(rest.to_string()))
            }
            "document" | "doc" => {
                let Some((coll, id)) = rest.split_once('/') else {
                    return refuse(format!(
                        "{}:{} is not a document; a document is named <coll>/<id>",
                        kind, rest
                    ));
                };
                if coll.is_empty() || id.is_empty() {
                    return refuse(format!(
                        "{}:{} has an empty collection or id",
                        kind, rest
                    ));
                }
                if id.contains('/') {
                    return refuse(format!(
                        "{}:{} has more than one '/'; a document id cannot contain one",
                        kind, rest
                    ));
                }
                Ok(Thing::Document { coll: coll.to_string(), id: id.to_string() })
            }
            "root" => Ok(Thing::Root(parse_u64("root:<seq>", rest)?)),
            "seq" => Ok(Thing::Seq(parse_u64("seq:<n>", rest)?)),
            other => refuse(format!(
                "unknown kind {:?}; the kinds are collection, document, root, seq",
                other
            )),
        };
    }

    if arg.is_empty() {
        return refuse("inspect was given an empty thing");
    }

    // Bare number: genuinely ambiguous, and the whole reason this function is
    // written out by hand.
    if arg.chars().all(|c| c.is_ascii_digit()) {
        return refuse(format!(
            "{0} is ambiguous: it could be seq:{0} (the write at sequence {0}) or \
             root:{0} (the state root persisted at sequence {0}), and it could be a \
             collection named {0} — the CLI will not pick. Write seq:{0}, root:{0}, \
             or collection:{0}",
            arg
        ));
    }

    // Bare `a/b`: a document. Structurally unambiguous — a collection name
    // cannot contain '/' (the engine refuses one, because on disk the name IS
    // a directory name), so no other kind can be spelled this way.
    if let Some((coll, id)) = arg.split_once('/') {
        if coll.is_empty() || id.is_empty() || id.contains('/') {
            return refuse(format!(
                "{:?} is not a document; a document is named <coll>/<id>",
                arg
            ));
        }
        return Ok(Thing::Document { coll: coll.to_string(), id: id.to_string() });
    }

    // Bare name: a collection. Also exactly one reading — root and seq are
    // numeric, and a document needs a collection to live in, so a lone name
    // cannot denote one.
    Ok(Thing::Collection(arg.to_string()))
}

/// Split `kind:rest` on the FIRST colon, when the part before it looks like a
/// kind marker. A Windows path or a URL-ish argument must not be mistaken for
/// one, so the marker has to be a bare lowercase word.
fn split_marker(arg: &str) -> Option<(&str, &str)> {
    let (kind, rest) = arg.split_once(':')?;
    if kind.is_empty() || !kind.chars().all(|c| c.is_ascii_lowercase()) {
        return None;
    }
    Some((kind, rest))
}

// ── Small refusing helpers ───────────────────────────────────────────────

fn no_words(cmd: &str, words: &[String]) -> Result<(), UsageError> {
    if words.is_empty() {
        return Ok(());
    }
    refuse(format!(
        "{} takes no arguments, but got {:?}",
        cmd,
        words.join(" ")
    ))
}

fn no_flags(cmd: &str, flags: &[(String, String)]) -> Result<(), UsageError> {
    match flags.first() {
        None => Ok(()),
        Some((name, _)) => refuse(format!("{} does not take {}", cmd, name)),
    }
}

/// The value of a flag, refusing a repeat. A repeated flag is never "last one
/// wins": that silently discards something the user typed on purpose.
fn take_flag(flags: &[(String, String)], name: &str) -> Result<Option<String>, UsageError> {
    let mut found: Option<&String> = None;
    for (n, v) in flags {
        if n != name {
            continue;
        }
        if let Some(prev) = found {
            return refuse(format!(
                "{} given twice, as {:?} and {:?}",
                name, prev, v
            ));
        }
        found = Some(v);
    }
    Ok(found.cloned())
}

fn reject_unused(
    flags: &[(String, String)],
    allowed: &[&str],
    cmd: &str,
) -> Result<(), UsageError> {
    for (name, _) in flags {
        if !allowed.contains(&name.as_str()) {
            return refuse(format!("{} does not take {}", cmd, name));
        }
    }
    Ok(())
}

fn parse_u64(what: &str, raw: &str) -> Result<u64, UsageError> {
    raw.parse::<u64>()
        .map_err(|_| UsageError(format!("{} needs a non-negative integer, got {:?}", what, raw)))
}

fn parse_usize(what: &str, raw: &str) -> Result<usize, UsageError> {
    raw.parse::<usize>()
        .map_err(|_| UsageError(format!("{} needs a non-negative integer, got {:?}", what, raw)))
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn p(line: &str) -> Result<Invocation, UsageError> {
        let argv: Vec<String> = if line.is_empty() {
            Vec::new()
        } else {
            line.split(' ').map(String::from).collect()
        };
        parse(&argv)
    }

    fn cmd(line: &str) -> Command {
        p(line).expect("expected this line to parse").command
    }

    fn err(line: &str) -> String {
        p(line).expect_err("expected this line to be refused").0
    }

    // ── every command parses ──────────────────────────────────────────────

    #[test]
    fn status_parses() {
        assert_eq!(cmd("status"), Command::Status);
    }

    #[test]
    fn log_parses_bare_and_with_both_flags() {
        assert_eq!(cmd("log"), Command::Log { limit: None, since: None });
        assert_eq!(cmd("log --limit 5"), Command::Log { limit: Some(5), since: None });
        assert_eq!(cmd("log --since 9"), Command::Log { limit: None, since: Some(9) });
        assert_eq!(
            cmd("log --limit 5 --since 9"),
            Command::Log { limit: Some(5), since: Some(9) }
        );
        assert_eq!(cmd("log --limit=5"), Command::Log { limit: Some(5), since: None });
    }

    #[test]
    fn root_subcommands_parse() {
        assert_eq!(cmd("root create"), Command::Root(RootCmd::Create { at: None }));
        assert_eq!(cmd("root create --at 3"), Command::Root(RootCmd::Create { at: Some(3) }));
        assert_eq!(cmd("root inspect"), Command::Root(RootCmd::Inspect { at: None }));
        assert_eq!(cmd("root inspect 3"), Command::Root(RootCmd::Inspect { at: Some(3) }));
        assert_eq!(cmd("root verify"), Command::Root(RootCmd::Verify { at: None }));
        assert_eq!(cmd("root verify 3"), Command::Root(RootCmd::Verify { at: Some(3) }));
        assert_eq!(cmd("root list"), Command::Root(RootCmd::List));
    }

    #[test]
    fn self_describing_commands_parse() {
        assert_eq!(cmd("grammar"), Command::Grammar);
        assert_eq!(cmd("constitution"), Command::Constitution);
        assert_eq!(cmd("version"), Command::Version);
        assert_eq!(cmd("help"), Command::Help);
    }

    #[test]
    fn query_takes_one_argument_or_many_words() {
        assert_eq!(cmd("query FROM users"), Command::Query("FROM users".into()));
        let argv = vec!["query".to_string(), "FROM users LIMIT 1".to_string()];
        assert_eq!(
            parse(&argv).unwrap().command,
            Command::Query("FROM users LIMIT 1".into())
        );
    }

    #[test]
    fn double_dash_passes_the_rest_through_to_the_query() {
        let argv: Vec<String> = ["query", "--", "FROM", "users", "--json"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let inv = parse(&argv).unwrap();
        assert_eq!(inv.command, Command::Query("FROM users --json".into()));
        // The `--json` after `--` belonged to the query, not to the CLI.
        assert!(!inv.json);
    }

    #[test]
    fn unwired_commands_parse_to_their_own_variant() {
        for (word, want) in [
            ("diff", NotWired::Diff),
            ("tag", NotWired::Tag),
            ("branch", NotWired::Branch),
            ("merge", NotWired::Merge),
        ] {
            assert_eq!(cmd(word), Command::NotWired(want));
        }
    }

    // ── unknown things are usage errors ──────────────────────────────────

    #[test]
    fn unknown_command_is_refused_by_name() {
        let m = err("frobnicate");
        assert!(m.contains("no such command"), "{}", m);
        assert!(m.contains("frobnicate"), "{}", m);
    }

    #[test]
    fn no_command_does_not_default_to_anything() {
        assert!(err("").contains("no command given"));
        assert!(err("--json").contains("no command given"));
    }

    #[test]
    fn unknown_flag_is_refused() {
        assert!(err("status --verbose").contains("unknown flag"));
        assert!(err("status -v").contains("unknown flag"));
    }

    #[test]
    fn flag_on_a_command_that_does_not_take_it_is_refused() {
        assert!(err("status --limit 5").contains("does not take --limit"));
        assert!(err("root list --at 1").contains("does not take --at"));
        assert!(err("grammar --since 1").contains("does not take --since"));
    }

    #[test]
    fn missing_flag_value_is_refused() {
        assert!(err("log --limit").contains("needs a value"));
        assert!(err("--db").contains("needs a value"));
    }

    #[test]
    fn non_numeric_where_a_number_is_required_is_refused() {
        assert!(err("log --limit abc").contains("non-negative integer"));
        assert!(err("root verify abc").contains("non-negative integer"));
        assert!(err("inspect root:abc").contains("non-negative integer"));
        assert!(err("inspect seq:-1").contains("non-negative integer"));
    }

    #[test]
    fn boolean_flag_given_a_value_is_refused() {
        assert!(err("status --json=yes").contains("takes no value"));
    }

    // ── ambiguity is REFUSED, and the message names the readings ─────────

    #[test]
    fn bare_number_to_inspect_is_refused_naming_both_readings() {
        let m = err("inspect 42");
        assert!(m.contains("ambiguous"), "{}", m);
        assert!(m.contains("seq:42"), "{}", m);
        assert!(m.contains("root:42"), "{}", m);
        assert!(m.contains("collection:42"), "{}", m);
    }

    #[test]
    fn bare_number_after_root_is_refused_naming_both_subcommands() {
        let m = err("root 7");
        assert!(m.contains("ambiguous"), "{}", m);
        assert!(m.contains("root inspect 7"), "{}", m);
        assert!(m.contains("root verify 7"), "{}", m);
    }

    #[test]
    fn bare_number_to_log_is_refused_naming_limit_and_since() {
        let m = err("log 20");
        assert!(m.contains("--limit 20"), "{}", m);
        assert!(m.contains("--since 20"), "{}", m);
    }

    #[test]
    fn marked_numeric_things_are_accepted_because_they_are_unambiguous() {
        assert_eq!(cmd("inspect seq:42"), Command::Inspect(Thing::Seq(42)));
        assert_eq!(cmd("inspect root:42"), Command::Inspect(Thing::Root(42)));
        assert_eq!(
            cmd("inspect collection:42"),
            Command::Inspect(Thing::Collection("42".into()))
        );
    }

    #[test]
    fn root_without_a_subcommand_names_the_subcommands() {
        let m = err("root");
        assert!(m.contains("create"), "{}", m);
        assert!(m.contains("inspect"), "{}", m);
        assert!(m.contains("verify"), "{}", m);
        assert!(m.contains("list"), "{}", m);
    }

    #[test]
    fn root_with_an_unknown_subcommand_is_refused() {
        assert!(err("root frobnicate").contains("has no subcommand"));
    }

    // ── inspect thing resolution ─────────────────────────────────────────

    #[test]
    fn thing_markers_and_aliases_resolve() {
        assert_eq!(
            cmd("inspect collection:users"),
            Command::Inspect(Thing::Collection("users".into()))
        );
        assert_eq!(
            cmd("inspect coll:users"),
            Command::Inspect(Thing::Collection("users".into()))
        );
        assert_eq!(
            cmd("inspect document:users/u1"),
            Command::Inspect(Thing::Document { coll: "users".into(), id: "u1".into() })
        );
        assert_eq!(
            cmd("inspect doc:users/u1"),
            Command::Inspect(Thing::Document { coll: "users".into(), id: "u1".into() })
        );
    }

    #[test]
    fn bare_slash_form_is_a_document_and_bare_name_is_a_collection() {
        assert_eq!(
            cmd("inspect users/u1"),
            Command::Inspect(Thing::Document { coll: "users".into(), id: "u1".into() })
        );
        assert_eq!(
            cmd("inspect users"),
            Command::Inspect(Thing::Collection("users".into()))
        );
    }

    #[test]
    fn malformed_things_are_refused() {
        assert!(err("inspect doc:users").contains("<coll>/<id>"));
        assert!(err("inspect doc:a/b/c").contains("more than one"));
        assert!(err("inspect users/u1/x").contains("<coll>/<id>"));
        assert!(err("inspect coll:a/b").contains("document:a/b"));
        assert!(err("inspect wat:1").contains("unknown kind"));
        assert!(err("inspect").contains("needs a thing"));
    }

    #[test]
    fn inspect_takes_exactly_one_thing() {
        let m = err("inspect seq:1 root:2");
        assert!(m.contains("exactly one thing"), "{}", m);
    }

    // ── --json everywhere ────────────────────────────────────────────────

    #[test]
    fn json_is_accepted_on_every_command_before_or_after_it() {
        for line in [
            "status", "log", "inspect users", "root create", "root inspect", "root verify",
            "root list", "grammar", "constitution", "version", "query FROM users", "diff",
            "tag", "branch", "merge", "help",
        ] {
            let after = format!("{} --json", line);
            assert!(p(&after).expect("--json after the command").json, "{}", after);
            let before = format!("--json {}", line);
            assert!(p(&before).expect("--json before the command").json, "{}", before);
        }
    }

    #[test]
    fn human_is_the_default_and_can_be_stated() {
        assert!(!p("status").unwrap().json);
        assert!(!p("status --human").unwrap().json);
    }

    // ── conflicting flags ────────────────────────────────────────────────

    #[test]
    fn json_and_human_together_are_refused() {
        let m = err("status --json --human");
        assert!(m.contains("--json and --human"), "{}", m);
        assert!(m.contains("will not choose"), "{}", m);
    }

    #[test]
    fn repeated_flags_are_refused_rather_than_last_one_wins() {
        assert!(err("status --json --json").contains("twice"));
        assert!(err("status --human --human").contains("twice"));
        assert!(err("log --limit 1 --limit 2").contains("twice"));
        assert!(err("root create --at 1 --at 2").contains("twice"));
    }

    #[test]
    fn two_different_databases_are_refused() {
        let m = err("status --db /a --db /b");
        assert!(m.contains("twice"), "{}", m);
        assert!(m.contains("will not"), "{}", m);
    }

    #[test]
    fn version_flag_with_another_command_is_refused() {
        let m = err("status --version");
        assert!(m.contains("--version"), "{}", m);
        assert_eq!(cmd("--version"), Command::Version);
        assert_eq!(cmd("version --version"), Command::Version);
    }

    #[test]
    fn help_flag_wins_over_a_bad_command_line() {
        assert_eq!(cmd("--help"), Command::Help);
        assert_eq!(cmd("-h"), Command::Help);
        assert_eq!(cmd("inspect 42 --help"), Command::Help);
    }

    #[test]
    fn limit_zero_is_refused_rather_than_silently_meaning_default() {
        assert!(err("log --limit 0").contains("no entries"));
    }

    // ── db selection and needs_db ────────────────────────────────────────

    #[test]
    fn db_flag_is_captured_but_not_resolved_here() {
        let inv = p("status --db /tmp/x").unwrap();
        assert_eq!(inv.db, Some(PathBuf::from("/tmp/x")));
        assert_eq!(p("status").unwrap().db, None);
    }

    #[test]
    fn self_describing_commands_do_not_need_a_database() {
        assert!(!Command::Grammar.needs_db());
        assert!(!Command::Version.needs_db());
        assert!(!Command::Help.needs_db());
        assert!(!Command::NotWired(NotWired::Diff).needs_db());
        assert!(Command::Status.needs_db());
        assert!(Command::Constitution.needs_db());
        assert!(Command::Root(RootCmd::List).needs_db());
    }

    #[test]
    fn command_names_are_stable_and_distinct_per_root_subcommand() {
        assert_eq!(Command::Root(RootCmd::Create { at: None }).name(), "root create");
        assert_eq!(Command::Root(RootCmd::Verify { at: None }).name(), "root verify");
        assert_eq!(Command::Status.name(), "status");
        assert_eq!(Command::NotWired(NotWired::Merge).name(), "merge");
    }

    /// Totality: no argument vector may panic, and every one must land on
    /// either an intent or a refusal.
    #[test]
    fn parsing_is_total_over_a_pile_of_junk() {
        for line in [
            "", "--", "-", "- -", "status --", "--db", "--db=", "inspect :", "inspect a:",
            "inspect :a", "inspect //", "inspect /", "root :", "query", "query --",
            "--json --human --help", "log --since= --limit=", "inspect CAPS:1",
        ] {
            let argv: Vec<String> = if line.is_empty() {
                Vec::new()
            } else {
                line.split(' ').map(String::from).collect()
            };
            // The assertion is that this returns at all.
            let _ = parse(&argv);
        }
    }
}
