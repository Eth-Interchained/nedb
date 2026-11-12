# nedb-core

The production core of [NEDB](https://github.com/Eth-Interchained/nedb): the
nonce-enforced, BLAKE2b hash-chained append-only log, the MVCC store, and
time-travel reads (`AS OF` / `VALID AS OF`) that the rest of the family is
built on.

**State is a pure function of the log.** Every write appends one sealed op —
`h_n = H(h_{n-1} || op_n)` — so history can be replayed to any past sequence,
verified end-to-end, and never quietly rewritten. Causal provenance
(`caused_by`) is sealed into the chain at write time, not reconstructed later.

This crate is the dependency-light foundation. For the full v2
content-addressed DAG engine, HTTP server (`nedbd`), NQL/neSQL and Portals,
see [`nedb-engine`](https://crates.io/crates/nedb-engine). For the language
bindings, see `nedb-node` (Node.js) and `nedb-py` (Python) in the same
workspace.

## License

BUSL-1.1 — free in production for organisations under USD $1M annual
revenue; converts to Apache 2.0 on 2030-09-11. © 2026 INTERCHAINED LLC.
