//! Natural-language query planning — the `cast` feature.
//!
//! Turns a short English prompt into NQL using `nedb-cast-slm`, a 3.34M-parameter
//! model trained specifically on this engine's query language. Off by default;
//! enable with the `cast` cargo feature and `nedbd --cast` (or `NEDBD_CAST=1`).
//!
//! Why this belongs in the engine rather than in a client:
//!
//! The hard part of natural-language querying is not the model, it is knowing the
//! schema. A client has to fetch the collection list and pass it in; the engine
//! already holds it in `db.id_index.collections()`. Putting the planner here means
//! it can be constrained to collections that actually exist, at the moment of the
//! call, for free — and every nedbd client inherits the capability instead of
//! reimplementing it.
//!
//! Safety posture, in order of importance:
//!
//!   1. The model NEVER executes anything itself. It emits NQL text, which is then
//!      handed to the ordinary `nql::query` path. Anything the parser rejects fails
//!      exactly as a hand-typed bad query would — there is no second execution path
//!      to audit.
//!   2. `execute` defaults to FALSE. The endpoint returns a plan for review. A
//!      planner that silently runs a wrong guess is worse than one that admits it
//!      is unsure.
//!   3. Unparseable output is reported as `valid: false` with the raw text attached,
//!      never swallowed into an empty result set. A silent empty result reads as
//!      "no matching rows" and that would be a lie.
//!
//! Model provenance: weights are a `model.cast` container, checksum-verified on
//! load. The same container is consumed by the Python package and the npm binding,
//! and CI holds all three to per-logit parity with the PyTorch reference
//! (max |Δlogit| 7.6e-6, decoded strings byte-identical).

use std::path::PathBuf;
use std::sync::Arc;

use nedb_cast_slm::Cast as CastModel;

/// A loaded planner. Cheap to clone (the weights sit behind an Arc), so the
/// server holds one and shares it across requests.
#[derive(Clone)]
pub struct Caster {
    inner: Arc<CastModel>,
    source: String,
}

impl Caster {
    /// Load from an explicit path, or search the conventional locations.
    ///
    /// Search order, first hit wins:
    ///   1. `$NEDBD_CAST_MODEL`             — explicit override
    ///   2. `<data_dir>/model.cast`         — sits with the database
    ///   3. `$CAST_HOME/model.cast`
    ///   4. `~/.cache/nedb-cast-slm/<tag>/model.cast`  — where the Python and npm
    ///      packages cache it, so a machine that already ran either one is ready
    pub fn load(data_dir: &std::path::Path) -> Result<Self, String> {
        for cand in Self::candidates(data_dir) {
            if !cand.exists() {
                continue;
            }
            return match CastModel::load(&cand) {
                Ok(m) => {
                    if !m.checksum_verified {
                        // Loud, not fatal: a container without a checksum is a
                        // format we still understand, but the operator should know
                        // the bytes were not verified.
                        eprintln!(
                            "  cast: WARNING {} has no checksum; bytes unverified",
                            cand.display()
                        );
                    }
                    Ok(Caster {
                        inner: Arc::new(m),
                        source: cand.display().to_string(),
                    })
                }
                Err(e) => Err(format!("failed to load {}: {}", cand.display(), e)),
            };
        }
        Err(format!(
            "no model.cast found. Looked in:\n  {}\n\
             Download it from \
             https://github.com/aiassistsecure/nedb-cast-slm/releases \
             or set NEDBD_CAST_MODEL to its path.",
            Self::candidates(data_dir)
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join("\n  ")
        ))
    }

    fn candidates(data_dir: &std::path::Path) -> Vec<PathBuf> {
        let mut v = Vec::new();
        if let Ok(p) = std::env::var("NEDBD_CAST_MODEL") {
            v.push(PathBuf::from(p));
        }
        v.push(data_dir.join("model.cast"));
        if let Ok(h) = std::env::var("CAST_HOME") {
            v.push(PathBuf::from(&h).join("model.cast"));
            v.push(PathBuf::from(&h).join("v10.30.90").join("model.cast"));
        }
        if let Ok(home) = std::env::var("HOME") {
            let base = PathBuf::from(home).join(".cache").join("nedb-cast-slm");
            v.push(base.join("v10.30.90").join("model.cast"));
            v.push(base.join("model.cast"));
        }
        v
    }

    pub fn source(&self) -> &str {
        &self.source
    }

    pub fn n_params(&self) -> usize {
        self.inner.n_params()
    }

    pub fn vocab_size(&self) -> usize {
        self.inner.vocab_size()
    }

    /// Cast a prompt into NQL text. Greedy decoding — a DSL has exactly one right
    /// answer, so sampling can only hurt.
    pub fn cast(&self, prompt: &str) -> String {
        self.inner.cast(prompt)
    }

    /// Cast, then check the collection it chose actually exists in this database.
    ///
    /// This is the payoff of living inside the engine. The model was trained on a
    /// synthetic schema universe, so on an unfamiliar database it can name a
    /// plausible-but-absent collection. We cannot un-generate that, but we CAN
    /// detect it precisely, because the engine knows the real collection list.
    /// Returning `unknown_collection` beats returning zero rows and letting the
    /// caller conclude the data isn't there.
    pub fn cast_checked(&self, prompt: &str, collections: &[String]) -> CastResult {
        let nql = self.cast(prompt);
        let coll = extract_collection(&nql);
        let known = match &coll {
            Some(c) => collections.iter().any(|k| k == c),
            None => false,
        };
        CastResult { nql, collection: coll, collection_known: known }
    }
}

#[derive(Debug, Clone)]
pub struct CastResult {
    pub nql: String,
    /// The collection named after FROM, if one could be read off the output.
    pub collection: Option<String>,
    /// Whether that collection exists in the database being queried.
    pub collection_known: bool,
}

/// Pull the collection name out of generated NQL: the token after `FROM`.
///
/// Deliberately a tiny scan rather than a call into the parser — this runs even
/// when the text does not fully parse, so we can report *which* collection was
/// attempted in the error path.
fn extract_collection(nql: &str) -> Option<String> {
    let mut it = nql.split_whitespace();
    while let Some(tok) = it.next() {
        if tok.eq_ignore_ascii_case("FROM") {
            return it.next().map(|s| s.trim_matches('"').to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_collection_after_from() {
        assert_eq!(
            extract_collection("FROM orders WHERE total > 99"),
            Some("orders".to_string())
        );
        assert_eq!(
            extract_collection("from stylists limit 5"),
            Some("stylists".to_string())
        );
        assert_eq!(extract_collection("WHERE total > 99"), None);
        assert_eq!(extract_collection(""), None);
    }

    #[test]
    fn strips_quotes_from_collection() {
        assert_eq!(
            extract_collection("FROM \"my coll\" LIMIT 1"),
            Some("my".to_string()),
            "quoted multiword collections are not a thing in NQL; \
             first token is the right answer"
        );
    }
}
