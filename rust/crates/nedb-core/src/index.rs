// nedb-core — secondary indexes: equality, ordered, and full-text search.
//
// Mirrors the Python reference exactly: equality uses a HashMap, ordered uses a
// sorted Vec (BTreeMap semantics via sort on insert), and search uses an inverted
// word-token map.  All indexes are maintained incrementally on write.

use std::collections::{HashMap, HashSet};

/// Which kind of index was created.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IndexKind {
    Eq,
    Ordered,
    Search,
}

/// One index: collection, field, kind, and the underlying data structure.
pub struct Index {
    pub coll:  String,
    pub field: String,
    pub kind:  IndexKind,
    /// Eq: field-value → {key, ...}
    eq: HashMap<String, HashSet<String>>,
    /// Ordered: sorted Vec<(field_value_as_string, key)> for range/sort
    ordered: Vec<(String, String)>,
    /// Search: token → {key, ...}
    inv: HashMap<String, HashSet<String>>,
}

fn tokenize(s: &str) -> Vec<String> {
    s.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|t| !t.is_empty())
        .map(String::from)
        .collect()
}

fn val_str(v: &serde_json::Value) -> Option<String> {
    match v {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Number(n) => Some(n.to_string()),
        serde_json::Value::Bool(b)   => Some(b.to_string()),
        _ => None,
    }
}

impl Index {
    pub fn new(coll: &str, field: &str, kind: IndexKind) -> Self {
        Self {
            coll: coll.to_string(),
            field: field.to_string(),
            kind,
            eq: HashMap::new(),
            ordered: Vec::new(),
            inv: HashMap::new(),
        }
    }

    pub fn add(&mut self, key: &str, doc: &serde_json::Value) {
        let Some(fval) = doc.get(&self.field) else { return };
        match self.kind {
            IndexKind::Eq => {
                if let Some(s) = val_str(fval) {
                    self.eq.entry(s).or_default().insert(key.to_string());
                }
            }
            IndexKind::Ordered => {
                if let Some(s) = val_str(fval) {
                    // Remove stale entry first (update path)
                    self.ordered.retain(|(_, k)| k != key);
                    let pos = self.ordered.partition_point(|(v, _)| v.as_str() <= s.as_str());
                    self.ordered.insert(pos, (s, key.to_string()));
                }
            }
            IndexKind::Search => {
                if let Some(s) = fval.as_str() {
                    for tok in tokenize(s) {
                        self.inv.entry(tok).or_default().insert(key.to_string());
                    }
                }
            }
        }
    }

    pub fn remove(&mut self, key: &str, doc: &serde_json::Value) {
        let Some(fval) = doc.get(&self.field) else { return };
        match self.kind {
            IndexKind::Eq => {
                if let Some(s) = val_str(fval) {
                    if let Some(set) = self.eq.get_mut(&s) {
                        set.remove(key);
                    }
                }
            }
            IndexKind::Ordered => {
                self.ordered.retain(|(_, k)| k != key);
            }
            IndexKind::Search => {
                if let Some(s) = fval.as_str() {
                    for tok in tokenize(s) {
                        if let Some(set) = self.inv.get_mut(&tok) {
                            set.remove(key);
                        }
                    }
                }
            }
        }
    }

    /// Equality lookup — returns matching store keys.
    pub fn eq_lookup(&self, value: &str) -> Option<HashSet<String>> {
        self.eq.get(value).cloned()
    }

    /// Full-text lookup for a single token — returns matching store keys.
    pub fn search_lookup(&self, token: &str) -> Option<HashSet<String>> {
        self.inv.get(token).cloned()
    }

    /// Full-text lookup: AND of all tokens in the query string.
    pub fn search_all(&self, text: &str) -> HashSet<String> {
        let tokens = tokenize(text);
        if tokens.is_empty() {
            return HashSet::new();
        }
        let mut result: Option<HashSet<String>> = None;
        for tok in &tokens {
            let hits = self.inv.get(tok).cloned().unwrap_or_default();
            result = Some(match result {
                None    => hits,
                Some(r) => r.intersection(&hits).cloned().collect(),
            });
        }
        result.unwrap_or_default()
    }
}

/// The full collection of indexes for a database.
#[derive(Default)]
pub struct Indexes {
    /// (coll, field, kind) — for persistence / round-trip
    pub config: Vec<(String, String, IndexKind)>,
    /// Inner map keyed by (coll, field)
    map: HashMap<(String, String), Index>,
}

impl Indexes {
    pub fn ensure(&mut self, coll: &str, field: &str, kind: IndexKind) {
        let k = (coll.to_string(), field.to_string());
        if !self.map.contains_key(&k) {
            self.config.push((coll.to_string(), field.to_string(), kind.clone()));
            self.map.insert(k, Index::new(coll, field, kind));
        }
    }

    pub fn add(&mut self, coll: &str, key: &str, doc: &serde_json::Value) {
        for idx in self.map.values_mut() {
            if idx.coll == coll {
                idx.add(key, doc);
            }
        }
    }

    pub fn remove(&mut self, coll: &str, key: &str, doc: &serde_json::Value) {
        for idx in self.map.values_mut() {
            if idx.coll == coll {
                idx.remove(key, doc);
            }
        }
    }

    pub fn has_eq(&self, coll: &str, field: &str) -> bool {
        self.map
            .get(&(coll.to_string(), field.to_string()))
            .map_or(false, |i| i.kind == IndexKind::Eq)
    }

    pub fn eq_lookup(&self, coll: &str, field: &str, value: &str) -> Option<HashSet<String>> {
        self.map
            .get(&(coll.to_string(), field.to_string()))
            .and_then(|i| i.eq_lookup(value))
    }

    pub fn search_all(&self, coll: &str, text: &str) -> HashSet<String> {
        let mut result: Option<HashSet<String>> = None;
        for idx in self.map.values() {
            if idx.coll == coll && idx.kind == IndexKind::Search {
                let hits = idx.search_all(text);
                result = Some(match result {
                    None    => hits,
                    Some(r) => r.union(&hits).cloned().collect(),
                });
            }
        }
        result.unwrap_or_default()
    }
}
