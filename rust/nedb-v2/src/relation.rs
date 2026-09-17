// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! Reading one relation out of the store, without going through a query
//! language to do it.
//!
//! # Why this module exists
//!
//! The SQL evaluator used to obtain its rows by BUILDING AN NQL STRING and
//! parsing it back:
//!
//! ```text
//!     let mut q = format!("FROM {}", cname);
//!     if let Some(seq) = temporal.get(&key) { q.push_str(&format!(" AS OF {}", seq)); }
//!     ...
//!     crate::nql::query(db, &q)
//! ```
//!
//! That is a translation. It is the same translation the project spent months
//! removing, pointed the other way — and it carried the same class of defect,
//! because a clause that fails to make it into the string is a clause that
//! silently does not happen. The retry path in `pgwire` did exactly that: it
//! rebuilt a shorter string by hand and dropped `VALID AS OF` and `SEARCH`
//! while carefully preserving `AS OF`, because someone had been bitten by
//! `AS OF` specifically and fixed that one.
//!
//! A struct cannot forget a field. This module is the scan expressed as data,
//! executed by calling the store directly.
//!
//! # This is the fold, not a second implementation
//!
//! `matches_valid_as_of` and `node_contains_text` LIVE HERE NOW. They were
//! private to `nql`, and NQL's executor calls into this module for them rather
//! than keeping a copy. That ordering matters: two copies of "what does VALID
//! AS OF mean" is the exact failure mode being removed, and it would be absurd
//! to create one while removing one.
//!
//! What remains in `nql` is its parser and its predicate evaluator. When the
//! last caller of those is gone, so is the file.

use serde_json::Value;

use crate::db::Db;
use crate::store::Node;

/// One relation to read, and the qualifiers that shape it.
///
/// Every field is a question the store can answer directly. There is no
/// rendering step and nothing to escape — `SEARCH 'o''brien'` was a quoting
/// problem when this was a string, and is not one now.
#[derive(Debug, Clone, Default)]
pub struct Scan {
    /// The collection name.
    pub coll: String,
    /// `AS OF SYSTEM TIME <seq>` — system time, a sequence.
    pub as_of: Option<u64>,
    /// `VALID AS OF '<date>'` — application time, a date string.
    pub valid_as_of: Option<String>,
    /// `SEARCH '<text>'` — substring over the document's rendered fields.
    pub search: Option<String>,
    /// `TRACE <edge> [REVERSE]` — replace each row with its causal chain.
    pub trace: Option<String>,
    /// Walk effects rather than causes.
    pub trace_reverse: bool,
    /// `TRAVERSE <rel>` — replace each row with its one-hop neighbours.
    pub traverse: Option<String>,
    /// Chain length cap for `TRACE`.
    ///
    /// Explicit rather than defaulted at the call site. NQL took this from the
    /// query's `LIMIT` and fell back to 1000 — which silently conflated "how
    /// many rows do I want back" with "how deep may a causal chain go", two
    /// unrelated numbers. They are separate here, and a truncated chain is a
    /// thing the caller chose.
    pub trace_limit: usize,
}

impl Scan {
    pub fn new(coll: impl Into<String>) -> Self {
        Scan { coll: coll.into(), trace_limit: DEFAULT_TRACE_LIMIT, ..Default::default() }
    }

    /// True when this scan asks for anything beyond the live collection.
    pub fn is_plain(&self) -> bool {
        self.as_of.is_none()
            && self.valid_as_of.is_none()
            && self.search.is_none()
            && self.trace.is_none()
            && self.traverse.is_none()
    }
}

/// The cap NQL used, preserved so a migrated query answers identically.
pub const DEFAULT_TRACE_LIMIT: usize = 1000;

/// Is `node` valid at `date`?
///
/// Moved here from `nql`, unchanged, and now the only definition.
///
/// `valid_from` is inclusive and `valid_to` is EXCLUSIVE, which is what makes
/// two adjacent validity windows tile without overlapping — a row ending
/// `2026-01-01` and the next beginning `2026-01-01` yields exactly one answer
/// on that date, not two and not zero.
pub fn matches_valid_as_of(node: &Node, date: &str) -> bool {
    let from_ok = node.valid_from.as_deref().map(|f| f <= date).unwrap_or(true);
    let to_ok = node.valid_to.as_deref().map(|t| t > date).unwrap_or(true);
    from_ok && to_ok
}

/// Full-text over the document's rendered JSON, case-insensitively.
///
/// Moved here from `nql`, unchanged, and now the only definition. It searches
/// the SERIALISED document, so it matches field names as well as values —
/// long-standing behaviour, preserved deliberately rather than quietly
/// improved, because changing what `SEARCH` matches is a semantic change and
/// this module's job is to not be one.
pub fn node_contains_text(node: &Node, text: &str) -> bool {
    node.data.to_string().to_lowercase().contains(&text.to_lowercase())
}

/// Read the relation.
///
/// The order is NQL's execution order, and it is load-bearing:
///
/// 1. **candidates** — every row, at a sequence or at the tip
/// 2. **filters** — `VALID AS OF`, then `SEARCH`
/// 3. **row-set transforms** — `TRACE`, then `TRAVERSE`
///
/// Filters before transforms is the part worth stating. Tracing first and
/// filtering after would apply `SEARCH` to the CHAIN rather than to the roots
/// the chain was grown from, which is a different question with a
/// plausible-looking answer.
pub fn read(db: &Db, scan: &Scan) -> Vec<Node> {
    // A sequence reaches through the graveyard: a row deleted after `seq` was
    // alive AT `seq`, and `list` only knows about the living. That is why this
    // goes id-by-id rather than filtering `list`.
    let candidates: Vec<Node> = match scan.as_of {
        Some(seq) => db
            .list_ids_including_deleted(&scan.coll)
            .into_iter()
            .filter_map(|id| db.get_as_of(&scan.coll, &id, seq))
            .collect(),
        None => db.list(&scan.coll),
    };

    let mut rows: Vec<Node> = candidates
        .into_iter()
        .filter(|n| {
            scan.valid_as_of
                .as_deref()
                .map(|d| matches_valid_as_of(n, d))
                .unwrap_or(true)
        })
        .filter(|n| {
            scan.search
                .as_deref()
                .map(|t| node_contains_text(n, t))
                .unwrap_or(true)
        })
        .collect();

    if scan.trace.is_some() {
        let limit = if scan.trace_limit == 0 { DEFAULT_TRACE_LIMIT } else { scan.trace_limit };
        let mut traced: Vec<Node> = Vec::new();
        for root in &rows {
            traced.extend(db.trace(&root.hash, scan.trace_reverse, limit));
        }
        rows = traced;
    }

    if let Some(rel) = &scan.traverse {
        let mut hopped: Vec<Node> = Vec::new();
        for root in &rows {
            hopped.extend(db.neighbors(&format!("{}:{}", root.coll, root.id), rel));
        }
        rows = hopped;
    }

    rows
}

/// Read the relation as query rows.
pub fn read_json(db: &Db, scan: &Scan) -> Vec<Value> {
    read(db, scan).iter().map(crate::nql::node_to_json).collect()
}

/// Resolve an `AS OF` marker to a real sequence number.
///
/// A bare integer passes through bit-for-bit; that is the backcompat
/// contract. A wall-clock moment arrives with `WALL_CLOCK_FLAG` set and is
/// resolved through [`Db::seq_at`] — the last sequence whose write-time is at
/// or before the moment.
///
/// # Why this is a function and not a closure in one executor
///
/// It WAS a closure inside the SQL executor, and NQL had no resolution at all.
/// That split is why `FROM orders AS OF "2026-01-01"` errored in Rust while
/// the Python reference engine accepted it: one dialect, two implementations,
/// and only one of them taught to read a timestamp.
///
/// Worse than the error was the fix that looked obvious — teaching the NQL
/// PARSER to accept a datetime without also teaching its executor to resolve
/// the flag. The marker would then reach `get_as_of` as a literal sequence
/// near 2^63 and the query would answer confidently from the wrong point in
/// history. An error is recoverable; a silently wrong answer about the past is
/// the failure this codebase exists to prevent.
///
/// Returns the message TEXT rather than a typed error because the two callers
/// surface it differently (a wire `ErrorResponse` and an `anyhow` bail), and
/// both would convert a shared enum straight back to a string.
pub fn resolve_as_of(db: &Db, marker: u64) -> Result<u64, String> {
    if (marker & crate::wallclock::WALL_CLOCK_FLAG) == 0 {
        return Ok(marker); // bare integer — a seq, untouched
    }
    let moment = crate::wallclock::WallClock::from_marker(marker)
        .ok_or_else(|| "invalid wall-clock marker".to_string())?;
    if !db.ts_index_ready() {
        return Err(
            "the write-time index is not ready on this boot (warm start defers it). \
             Run `nedb-cli repair` or a cold scan, or AS OF a bare sequence number"
                .to_string(),
        );
    }
    match db.seq_at(moment.epoch_secs()) {
        Some(seq) => Ok(seq),
        None => {
            let floor = db.history_floor();
            // "Before anything existed" and "pruned away" read completely
            // differently to an operator: one is routine, the other is the
            // compaction tradeoff answering back.
            if floor > 0 {
                Err(format!(
                    "history at or before that moment is no longer available — \
                     the store was compacted past it (history floor {}). \
                     AS OF a bare sequence at or after the floor instead",
                    floor
                ))
            } else {
                Err(format!(
                    "no writes at or before that moment in this database — \
                     nothing existed yet (the first write is at seq {}). \
                     A timestamp answers about the past; there is no past here yet",
                    db.seq.load(std::sync::atomic::Ordering::SeqCst)
                ))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn db() -> (tempfile::TempDir, Db) {
        let dir = tempfile::tempdir().unwrap();
        let db = Db::open(dir.path(), None).unwrap();
        (dir, db)
    }

    #[test]
    fn a_plain_scan_is_the_live_collection() {
        let (_d, db) = db();
        db.put("orders", "1", json!({"who": "acme"}), vec![], None, None).unwrap();
        db.put("orders", "2", json!({"who": "globex"}), vec![], None, None).unwrap();
        let rows = read(&db, &Scan::new("orders"));
        assert_eq!(rows.len(), 2);
    }

    #[test]
    fn as_of_reaches_a_row_that_was_deleted_later() {
        // The reason the candidate set is built from
        // `list_ids_including_deleted` rather than from `list`. A scan that
        // started from the living would answer "it was never there", which is
        // a different and wrong claim about the past.
        let (_d, db) = db();
        db.put("orders", "1", json!({"who": "acme"}), vec![], None, None).unwrap();
        let alive = db.seq.load(std::sync::atomic::Ordering::SeqCst) - 1;
        db.delete("orders", "1").unwrap();

        assert_eq!(read(&db, &Scan::new("orders")).len(), 0, "gone at the tip");
        let past = Scan { as_of: Some(alive), ..Scan::new("orders") };
        assert_eq!(read(&db, &past).len(), 1, "present at the sequence it was alive");
    }

    #[test]
    fn search_filters_before_trace_grows_the_row_set() {
        // Order matters: searching after the trace would test the CHAIN, not
        // the roots, and quietly answer a different question.
        let (_d, db) = db();
        db.put("orders", "1", json!({"who": "acme"}), vec![], None, None).unwrap();
        db.put("orders", "2", json!({"who": "globex"}), vec![], None, None).unwrap();

        let s = Scan { search: Some("acme".into()), ..Scan::new("orders") };
        let rows = read(&db, &s);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, "1");
    }

    #[test]
    fn valid_to_is_exclusive_so_windows_tile() {
        let (_d, db) = db();
        db.put("p", "1", json!({"v": 1}), vec![],
               Some("2026-01-01".into()), Some("2026-02-01".into())).unwrap();
        db.put("p", "2", json!({"v": 2}), vec![],
               Some("2026-02-01".into()), None).unwrap();

        let on = |d: &str| {
            let s = Scan { valid_as_of: Some(d.into()), ..Scan::new("p") };
            read(&db, &s).into_iter().map(|n| n.id).collect::<Vec<_>>()
        };
        assert_eq!(on("2026-01-15"), vec!["1"]);
        // The boundary: exactly one row, because `valid_to` is exclusive.
        assert_eq!(on("2026-02-01"), vec!["2"]);
    }

    #[test]
    fn a_scan_knows_whether_it_is_plain() {
        assert!(Scan::new("t").is_plain());
        assert!(!Scan { as_of: Some(1), ..Scan::new("t") }.is_plain());
        assert!(!Scan { trace: Some("caused_by".into()), ..Scan::new("t") }.is_plain());
    }
}
