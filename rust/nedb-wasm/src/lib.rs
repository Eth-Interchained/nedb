// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC

//! wasm-bindgen bindings: expose the v2 DAG Db to JavaScript running in a
//! browser (or any wasm32-unknown-unknown host) as `NedbWasm`.
//!
//! In-memory only: `Db::in_memory()` — zero disk I/O, zero threads, no server.
//! The API surface mirrors the napi binding (crates/nedb-node) so the same
//! engine contract holds across Node-native and browser runtimes; where napi
//! uses BigInt, this binding takes plain JS numbers (seq counts fit in f64).

use std::sync::Arc;
use nedb_engine::{Db, nql};
use serde_json::Value;
use wasm_bindgen::prelude::*;

fn jerr(e: impl std::fmt::Display) -> JsValue {
    JsValue::from_str(&e.to_string())
}

/// Serialize a node exactly like the napi binding's `node_to_json_str`:
/// user doc + `_id`/`_hash`/`_seq`/`_coll`, plus the causal (`_caused_by`)
/// and bi-temporal (`_valid_from`/`_valid_to`) lanes when present.
fn node_to_json_str(node: &nedb_engine::store::Node) -> String {
    let mut obj = if let Value::Object(m) = &node.data { m.clone() } else { Default::default() };
    obj.insert("_id".into(),   Value::String(node.id.clone()));
    obj.insert("_hash".into(), Value::String(node.hash.clone()));
    obj.insert("_seq".into(),  serde_json::json!(node.seq));
    obj.insert("_coll".into(), Value::String(node.coll.clone()));
    if !node.caused_by.is_empty() {
        obj.insert("_caused_by".into(), Value::Array(
            node.caused_by.iter().map(|h| Value::String(h.clone())).collect()
        ));
    }
    if let Some(ref vf) = node.valid_from {
        obj.insert("_valid_from".into(), Value::String(vf.clone()));
    }
    if let Some(ref vt) = node.valid_to {
        obj.insert("_valid_to".into(), Value::String(vt.clone()));
    }
    Value::Object(obj).to_string()
}

#[wasm_bindgen(js_name = "NedbWasm")]
pub struct NedbWasm {
    inner: Arc<Db>,
}

#[wasm_bindgen(js_class = "NedbWasm")]
impl NedbWasm {
    /// Create an in-memory v2 DAG database — zero disk I/O. This is the only
    /// constructor: the browser build has no durable file mode (that's the
    /// Phase-2 OPFS/VFS work).
    #[wasm_bindgen(constructor)]
    pub fn new() -> NedbWasm {
        NedbWasm { inner: Arc::new(Db::in_memory()) }
    }

    // ── Indexes ──────────────────────────────────────────────────────────

    /// Create a sorted index on `field` (all kinds map to sorted, napi parity).
    pub fn create_index(&self, coll: String, field: String) {
        self.inner.create_sorted_index(&coll, &field);
    }

    // ── Writes ───────────────────────────────────────────────────────────

    /// Put a document. `doc_json` is a JSON string; returns the stored doc
    /// (with `_id`/`_hash`/`_seq`/… lanes) as a JSON string.
    pub fn put(&self, coll: String, id: String, doc_json: String) -> Result<String, JsValue> {
        let doc: Value = serde_json::from_str(&doc_json).map_err(jerr)?;
        let caused_by: Vec<String> = doc.get("caused_by")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
            .unwrap_or_default();
        let valid_from = doc.get("valid_from").and_then(|v| v.as_str()).map(str::to_string);
        let valid_to   = doc.get("valid_to").and_then(|v| v.as_str()).map(str::to_string);
        self.inner.put(&coll, &id, doc, caused_by, valid_from, valid_to)
            .map(|n| node_to_json_str(&n))
            .map_err(jerr)
    }

    /// Delete a document (tombstone — history is preserved, napi parity).
    pub fn delete(&self, coll: String, id: String) -> Result<(), JsValue> {
        self.inner.delete(&coll, &id).map(|_| ()).map_err(jerr)
    }

    /// Link: stored as a doc in the `__links__` collection for NQL traversal.
    pub fn link(&self, frm: String, rel: String, to: String) -> Result<(), JsValue> {
        let link_id = format!("{}|{}|{}", frm, rel, to);
        let doc = serde_json::json!({"_from": frm, "_rel": rel, "_to": to});
        self.inner.put("__links__", &link_id, doc, vec![], None, None)
            .map(|_| ()).map_err(jerr)
    }

    /// Remove a link.
    pub fn unlink(&self, frm: String, rel: String, to: String) -> Result<(), JsValue> {
        let link_id = format!("{}|{}|{}", frm, rel, to);
        self.inner.delete("__links__", &link_id).map(|_| ()).map_err(jerr)
    }

    // ── Reads ────────────────────────────────────────────────────────────

    /// Get the current version of a document, or `undefined` when missing.
    pub fn get(&self, coll: String, id: String) -> Option<String> {
        self.inner.get(&coll, &id).as_ref().map(node_to_json_str)
    }

    /// Get the version of a document live at sequence `as_of` (time travel).
    pub fn get_as_of(&self, coll: String, id: String, as_of: f64) -> Option<String> {
        self.inner.get_as_of(&coll, &id, as_of as u64)
            .as_ref().map(node_to_json_str)
    }

    /// Run neSQL — PostgreSQL SQL or NQL, routed structurally like the napi
    /// binding and the `nesql` CLI. Returns an array of JSON strings.
    pub fn query(&self, statement: String) -> Result<Vec<String>, JsValue> {
        self.nesql(statement)
    }

    /// `query()` under the language's own name.
    pub fn nesql(&self, statement: String) -> Result<Vec<String>, JsValue> {
        use nedb_engine::nesql::{route, Dialect};
        match route(&statement).map_err(jerr)? {
            Dialect::Nql => nql::query(&self.inner, &statement)
                .map(|(rows, _)| rows.into_iter().map(|v| v.to_string()).collect())
                .map_err(jerr),
            Dialect::Sql => {
                let db = Arc::new(self.inner.clone());
                nedb_engine::pgwire::execute_sql(&db, &statement, false)
                    .map(|d| d.rows.into_iter().map(|v| v.to_string()).collect())
                    .map_err(jerr)
            }
        }
    }

    /// Which half of neSQL a statement is written in: `"nql"` or `"sql"`.
    pub fn nesql_dialect(&self, statement: String) -> Result<String, JsValue> {
        nedb_engine::nesql::route(&statement)
            .map(|d| d.name().to_string())
            .map_err(jerr)
    }

    /// Outbound neighbors of `frm` via relation `rel`.
    pub fn neighbors(&self, frm: String, rel: String) -> Vec<String> {
        self.neighbors_as_of(frm, rel, self.seq())
    }

    /// Outbound neighbors live at sequence `as_of` (time-travel the DAG).
    pub fn neighbors_as_of(&self, frm: String, rel: String, as_of: f64) -> Vec<String> {
        let nql_str = format!(
            r#"FROM __links__ AS OF {} WHERE _from = "{}" AND _rel = "{}""#,
            as_of as u64, frm, rel);
        nql::query(&self.inner, &nql_str)
            .map(|(rows, _)| rows.iter()
                .filter_map(|r| r.get("_to").and_then(|v| v.as_str()).map(str::to_string))
                .collect())
            .unwrap_or_default()
    }

    /// Inbound neighbors of `to` via relation `rel`.
    pub fn inbound(&self, to: String, rel: String) -> Vec<String> {
        self.inbound_as_of(to, rel, self.seq())
    }

    /// Inbound neighbors live at sequence `as_of`.
    pub fn inbound_as_of(&self, to: String, rel: String, as_of: f64) -> Vec<String> {
        let nql_str = format!(
            r#"FROM __links__ AS OF {} WHERE _to = "{}" AND _rel = "{}""#,
            as_of as u64, to, rel);
        nql::query(&self.inner, &nql_str)
            .map(|(rows, _)| rows.iter()
                .filter_map(|r| r.get("_from").and_then(|v| v.as_str()).map(str::to_string))
                .collect())
            .unwrap_or_default()
    }

    // ── Integrity ────────────────────────────────────────────────────────

    /// Re-walk the hash chain; `true` = untampered.
    pub fn verify(&self) -> bool {
        let (_, tampered) = self.inner.verify();
        tampered.is_empty()
    }

    /// Current Merkle head (64-char hex).
    pub fn head(&self) -> String { self.inner.head() }

    /// Current sequence number.
    pub fn seq(&self) -> f64 {
        self.inner.seq.load(std::sync::atomic::Ordering::SeqCst) as f64
    }

    /// No-op on in-memory (nothing to flush); present for napi API parity.
    pub fn flush(&self) { self.inner.flush_all(); }

    /// The most recent write as a JSON string, or `undefined` when empty.
    pub fn tip(&self) -> Option<String> {
        self.inner.tip().as_ref().map(node_to_json_str)
    }

    /// Most recent write into `coll`, or `undefined` when empty.
    pub fn tip_collection(&self, coll: String) -> Option<String> {
        self.inner.tip_collection(&coll).as_ref().map(node_to_json_str)
    }

    /// Changefeed page after `after_seq` (exclusive) as a JSON envelope:
    /// `{nodes, from_seq, to_seq, head_seq, has_more}`.
    pub fn since(&self, after_seq: f64, limit: i32) -> String {
        let b = self.inner.since(after_seq as u64, limit.max(0) as usize);
        let nodes: Vec<Value> = b.nodes.iter()
            .filter_map(|n| serde_json::from_str::<Value>(&node_to_json_str(n)).ok())
            .collect();
        serde_json::json!({
            "nodes": nodes, "from_seq": b.from_seq, "to_seq": b.to_seq,
            "head_seq": b.head_seq, "has_more": b.has_more
        }).to_string()
    }

    /// Replication readiness as a JSON string.
    pub fn scan_status(&self) -> String {
        let s = self.inner.scan_status();
        serde_json::json!({
            "scan_complete": s.scan_complete, "seq_index_ready": s.seq_index_ready, "tip_seq": s.tip_seq,
            "indexed_seq_min": s.indexed_seq_min, "indexed_seq_max": s.indexed_seq_max,
            "indexed_count": s.indexed_count
        }).to_string()
    }
}
