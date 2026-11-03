// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC

//! wasm32 compatibility shim for rayon.
//!
//! wasm32-unknown-unknown has no threads, so rayon's parallel iterators cannot
//! run there. This module provides the tiny slice of the rayon API the engine
//! actually uses (`par_iter` / `par_iter_mut` on slices) with sequential
//! semantics, selected at the call sites via:
//!
//! ```ignore
//! #[cfg(not(target_arch = "wasm32"))]
//! use rayon::prelude::*;
//! #[cfg(target_arch = "wasm32")]
//! use crate::rayon_compat::prelude::*;
//! ```
//!
//! Native builds keep the real rayon; nothing about their behavior changes.
//! Only compiled on wasm32 (see `lib.rs`).

/// Drop-in for `rayon::prelude` covering exactly what the engine uses.
pub mod prelude {
    /// Sequential stand-in for rayon's parallel slice iterators.
    pub trait ParSlice<T> {
        /// Sequential `par_iter` — same items, same order, calling thread.
        fn par_iter(&self) -> std::slice::Iter<'_, T>;
        /// Sequential `par_iter_mut` — same items, same order, calling thread.
        fn par_iter_mut(&mut self) -> std::slice::IterMut<'_, T>;
    }

    impl<T> ParSlice<T> for [T] {
        fn par_iter(&self) -> std::slice::Iter<'_, T> {
            self.iter()
        }
        fn par_iter_mut(&mut self) -> std::slice::IterMut<'_, T> {
            self.iter_mut()
        }
    }
}
