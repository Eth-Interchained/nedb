// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! What a command produces, and what the process does with it.
//!
//! Every command returns a [`Report`] — an exit code, a JSON body, and human
//! text — rather than printing. Three consequences, all of them the point:
//!
//!   * the exit code is chosen where the decision is made, not inferred from a
//!     string at the edge;
//!   * the tests call the command and assert on the code, so "checked and good"
//!     versus "could not check" is a test assertion rather than a convention;
//!   * human and JSON output are built from the same values, so they cannot
//!     drift into disagreeing about what happened.

use serde_json::{json, Value};

/// The process exit codes, and their meanings, in one place.
///
/// The distinction that matters most is 0 / 3: a caller scripting against this
/// must be able to tell a check that ran and passed from a check that could not
/// run. Folding "could not determine" into either success or failure destroys
/// exactly the fact an operator needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Exit {
    /// The thing was done, or the check ran and passed.
    Ok,
    /// The operation ran and did not succeed.
    Failure,
    /// The command line was not understood, or was ambiguous.
    Usage,
    /// The check could not be performed. Not a failure.
    Indeterminate,
    /// The named thing does not exist.
    NotFound,
    /// A version or format this build does not know.
    Unsupported,
}

impl Exit {
    pub fn code(self) -> i32 {
        match self {
            Exit::Ok => 0,
            Exit::Failure => 1,
            Exit::Usage => 2,
            Exit::Indeterminate => 3,
            Exit::NotFound => 4,
            Exit::Unsupported => 5,
        }
    }

    /// The name used in JSON output. Stable — callers may match on it.
    pub fn name(self) -> &'static str {
        match self {
            Exit::Ok => "ok",
            Exit::Failure => "failure",
            Exit::Usage => "usage",
            Exit::Indeterminate => "indeterminate",
            Exit::NotFound => "not_found",
            Exit::Unsupported => "unsupported",
        }
    }

    /// Adopt a code produced by the engine (`RootVerification::exit_code`).
    ///
    /// An unrecognised code becomes `Failure` rather than being passed through:
    /// a code this build cannot name is a code it cannot document, and an
    /// undocumented exit code is worse than a generic failure.
    pub fn from_code(code: i32) -> Exit {
        match code {
            0 => Exit::Ok,
            2 => Exit::Usage,
            3 => Exit::Indeterminate,
            4 => Exit::NotFound,
            5 => Exit::Unsupported,
            _ => Exit::Failure,
        }
    }

    pub fn is_ok(self) -> bool {
        matches!(self, Exit::Ok)
    }
}

/// Human or machine. Human is the default; JSON is exact.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Human,
    Json,
}

/// One command's complete outcome.
pub struct Report {
    pub exit: Exit,
    /// The payload, WITHOUT the envelope. [`Report::render`] adds the envelope
    /// so every command's JSON has the same outer shape whether it succeeded or
    /// not.
    pub body: Value,
    pub human: String,
}

impl Report {
    pub fn ok(body: Value, human: impl Into<String>) -> Report {
        Report { exit: Exit::Ok, body, human: human.into() }
    }

    pub fn new(exit: Exit, body: Value, human: impl Into<String>) -> Report {
        Report { exit, body, human: human.into() }
    }

    /// An outcome with no payload beyond its reason.
    pub fn err(exit: Exit, reason: impl Into<String>) -> Report {
        let reason = reason.into();
        Report {
            exit,
            body: json!({ "error": reason }),
            human: format!("error: {}", reason),
        }
    }

    /// A usage error. Always exit 2 — there is no other code a malformed or
    /// ambiguous command line can honestly produce.
    pub fn usage(reason: impl Into<String>) -> Report {
        let reason = reason.into();
        Report {
            exit: Exit::Usage,
            body: json!({ "error": reason }),
            human: format!("usage: {}\n\nrun `nesql grammar` for the command surface", reason),
        }
    }

    /// The text to print, given a format. The JSON envelope is added here and
    /// only here.
    pub fn render(&self, command: &str, format: Format) -> String {
        match format {
            Format::Human => self.human.clone(),
            Format::Json => {
                let mut envelope = json!({
                    "ok": self.exit.is_ok(),
                    "command": command,
                    "exit": self.exit.code(),
                    "status": self.exit.name(),
                });
                // Payload keys sit beside the envelope keys rather than under a
                // "data" key: one level, so `jq .seq` works and does not have
                // to know whether the call succeeded to find the field.
                if let (Some(dst), Some(src)) = (envelope.as_object_mut(), self.body.as_object()) {
                    for (k, v) in src {
                        dst.insert(k.clone(), v.clone());
                    }
                }
                serde_json::to_string_pretty(&envelope)
                    .unwrap_or_else(|e| format!("{{\"ok\":false,\"exit\":1,\"error\":\"{}\"}}", e))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_are_the_documented_ones() {
        assert_eq!(Exit::Ok.code(), 0);
        assert_eq!(Exit::Failure.code(), 1);
        assert_eq!(Exit::Usage.code(), 2);
        assert_eq!(Exit::Indeterminate.code(), 3);
        assert_eq!(Exit::NotFound.code(), 4);
        assert_eq!(Exit::Unsupported.code(), 5);
    }

    #[test]
    fn from_code_round_trips_every_named_code() {
        for e in [Exit::Ok, Exit::Failure, Exit::Usage, Exit::Indeterminate, Exit::NotFound, Exit::Unsupported] {
            assert_eq!(Exit::from_code(e.code()), e);
        }
    }

    #[test]
    fn unknown_engine_code_becomes_generic_failure_not_a_silent_success() {
        assert_eq!(Exit::from_code(99), Exit::Failure);
        assert_eq!(Exit::from_code(-1), Exit::Failure);
    }

    #[test]
    fn indeterminate_is_not_ok_and_not_failure() {
        assert!(!Exit::Indeterminate.is_ok());
        assert_ne!(Exit::Indeterminate, Exit::Failure);
        assert_eq!(Exit::Indeterminate.name(), "indeterminate");
    }

    #[test]
    fn json_envelope_carries_status_and_payload_side_by_side() {
        let r = Report::new(Exit::Indeterminate, json!({ "seq": 4 }), "hm");
        let text = r.render("root verify", Format::Json);
        let v: Value = serde_json::from_str(&text).expect("render emits valid json");
        assert_eq!(v["ok"], json!(false));
        assert_eq!(v["exit"], json!(3));
        assert_eq!(v["status"], json!("indeterminate"));
        assert_eq!(v["command"], json!("root verify"));
        assert_eq!(v["seq"], json!(4));
    }

    #[test]
    fn human_render_is_the_human_text_verbatim() {
        let r = Report::ok(json!({}), "seq 4");
        assert_eq!(r.render("status", Format::Human), "seq 4");
    }

    #[test]
    fn usage_report_is_always_exit_two() {
        let r = Report::usage("no such command: frobnicate");
        assert_eq!(r.exit, Exit::Usage);
        assert!(r.human.contains("frobnicate"));
    }
}
