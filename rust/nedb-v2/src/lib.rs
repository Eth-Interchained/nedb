// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! NEDB v2 — Content-addressed DAG storage engine.
//!
//! Architecture:
//!
//!   objects/     — content-addressed, encrypted, BLAKE2b-verified nodes (immutable)
//!   indexes/     — id index (file-per-doc) + sorted indexes (BTreeMap, in-memory)
//!   graph/       — typed DAG edges (filesystem entries)
//!   MANIFEST     — BLAKE2b Merkle root of all collection HEADs
//!
//! Properties:
//!   - Uncorruptable: atomic writes (tmp→rename), hash verification on read
//!   - Parallel: no global lock on writes; each doc has its own index file
//!   - Instant cold start: no AOF replay; rebuild sorted indexes in parallel
//!   - Self-healing: migrate.rs handles v1→v2 on first startup
//!   - Tamper-evident: BLAKE2b chain from MANIFEST → collection heads → nodes

pub mod store;
pub mod segment;
pub mod index;
pub mod graph;
pub mod namespace;
pub mod nesql;
pub mod relation;
pub mod root;
pub mod diff;
pub mod refs;
pub mod constitution;
pub mod cause;
pub mod wallclock;
pub mod branch;
pub mod merge;
pub mod conflict;
pub mod migrate;
pub mod db;
pub mod exit;
pub mod nql;
pub mod pgwire;
pub mod pgcatalog;
pub mod sqlselect;
pub mod sqljoin;
pub mod sqlplan;
pub mod sqlpush;
#[cfg(feature = "cast")]
pub mod cast;
// The HTTP server (axum/tokio) does not target wasm32-unknown-unknown.
// The wasm build ships the engine library only — no nedbd, no pgwire
// networking. Native builds are untouched.
#[cfg(not(target_arch = "wasm32"))]
pub mod server;
// wasm32 has no threads: rayon is unavailable there, so the parallel
// iterators degrade to sequential ones via this shim (native keeps rayon).
#[cfg(target_arch = "wasm32")]
pub(crate) mod rayon_compat;

pub use store::{Dek, Node, ObjectStore};
pub use index::{IdIndex, OrderedValue, SortedIndexes};
pub use graph::GraphStore;
pub use db::Db;
