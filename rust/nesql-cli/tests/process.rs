//! Process-level output contracts, including failures before dispatch.
use std::process::Command;

#[test]
fn malformed_json_commands_emit_one_envelope_without_opening_a_database() {
    for args in [
        vec!["--json", "inspect", "42"],
        vec!["inspect", "42", "--json"],
        vec!["--json", "unknown-command"],
        vec!["--unknown-flag", "--json"],
        vec!["--json", "--limit"],
        vec!["--json", "--json", "status"],
    ] {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("must-not-exist");
        let output = Command::new(env!("CARGO_BIN_EXE_nesql"))
            .arg("--db").arg(&db).args(&args).output().unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        let body: serde_json::Value = serde_json::from_slice(&output.stdout)
            .unwrap_or_else(|e| panic!("{args:?}: {e}; {output:?}"));
        assert_eq!(body["ok"], false);
        assert_eq!(body["exit"], 2);
        assert_eq!(body["status"], "usage");
        assert_eq!(body["command"], "parse");
        assert!(!body["error"].as_str().unwrap().is_empty());
        assert!(output.stderr.is_empty(), "{output:?}");
        assert!(!db.exists());
    }
}

#[test]
fn human_errors_and_literal_json_operands_stay_on_stderr() {
    for args in [
        vec!["inspect", "42"],
        vec!["--human", "inspect", "42"],
        vec!["--json", "--human", "inspect", "42"],
        vec!["inspect", "42", "--", "--json"],
        vec!["--db", "--json", "inspect", "42"],
        vec!["--db=--json", "inspect", "42"],
        vec!["tag", "create", "t", "bad-seq", "--message", "--json"],
    ] {
        let dir = tempfile::tempdir().unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_nesql"))
            .current_dir(dir.path()).args(&args).output().unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        assert!(output.stdout.is_empty(), "{args:?}: {output:?}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("usage:"));
        assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
    }
}
