# nedb-node

napi-rs bindings for the NEDB engine — the native module behind the
[`@interchained/nedb`](https://www.npmjs.com/package/@interchained/nedb)
npm package.

This crate is a binding layer over
[`nedb-engine`](https://crates.io/crates/nedb-engine) (the v2
content-addressed DAG engine): the same engine contract — NQL/neSQL
queries, time-travel, bi-temporal reads, causal provenance, hash-chain
`verify()` — exposed to Node.js through napi-rs (napi8).

Most users should install the npm package rather than build this crate:

```bash
npm install @interchained/nedb
```

Build it directly only when working on the bindings themselves:

```bash
cargo build -p nedb-node --target x86_64-pc-windows-msvc   # Windows: MSVC only
# or via napi:  npx napi build --cargo-cwd rust/crates/nedb-node
```

## License

BUSL-1.1 — free in production for organisations under USD $1M annual
revenue; converts to Apache 2.0 on 2030-09-11. © 2026 INTERCHAINED LLC.
