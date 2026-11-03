# nedb-wasm

NEDB v2 engine compiled to WebAssembly — the browser build. Phase 1 of the
WASM milestone (`M7` in `docs/SPEC.md`).

## What it is

`wasm-bindgen` bindings over `Db::in_memory()`: the full engine (NQL + neSQL,
time-travel, bi-temporal, causal provenance, hash-chain `verify()`, graph
links) running inside a web page or any `wasm32-unknown-unknown` host. The API
mirrors the napi binding (`rust/crates/nedb-node`) so the same engine contract
holds across Node-native and browser runtimes.

**In-memory only.** No filesystem, no server, no threads, no pgwire
networking — those are compiled out with `cfg(target_arch = "wasm32")` (see
"How the engine was made wasm-safe" below). Durable browser storage (OPFS /
storage-trait VFS) is Phase 2.

## Build

```bash
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.129   # match wasm-bindgen in Cargo.toml

./build-wasm.sh            # dev profile -> pkg/
./build-wasm.sh release    # release profile -> pkg/
node smoke.cjs             # smoke test against pkg/
```

`pkg/` holds `nedb_wasm.js` (use `--target nodejs` output in Node, or
re-run wasm-bindgen with `--target web`/`bundler` for browsers) plus
`nedb_wasm_bg.wasm`.

## JS API

```js
const { NedbWasm } = require('./pkg/nedb_wasm.js');
const db = new NedbWasm();

db.put('users', 'alice', JSON.stringify({ age: 32 })); // -> JSON string
JSON.parse(db.get('users', 'alice'));                  // -> doc or undefined
db.query('FROM users WHERE age = 32');                 // NQL -> string[]
db.query("SELECT _id FROM users WHERE age > 30");      // SQL -> string[]
db.get_as_of('users', 'alice', 41);                    // time travel
db.link('alice', 'knows', 'bob');
db.neighbors('alice', 'knows');                        // -> ['bob']
db.verify();                                           // hash chain -> bool
db.head(); db.seq(); db.tip(); db.since(0, 100);
```

`query()` accepts both halves of neSQL (NQL and PostgreSQL), routed by
`nedb_engine::nesql::route` — the same router the `nesql` CLI uses.

## How the engine was made wasm-safe

All changes are `cfg(target_arch = "wasm32")`-gated; native builds compile
and behave exactly as before (verified: `cargo check -p nedb-engine` and
`-p nedb-node` clean).

| Blocker | Fix |
|---|---|
| tokio/axum (server, pgwire networking) | deps + `pub mod server` + pgwire `handle`/`run`/wire helpers gated to non-wasm32 |
| rayon (no threads on wasm) | dep gated out; `src/rayon_compat.rs` shim gives `par_iter`/`par_iter_mut` sequential semantics at the 4 use sites |
| `std::thread::spawn` (cold-scan, manifest ticker, index flusher) | compiles on wasm (never called — all durable-path only) |
| fs2 advisory dir lock | `acquire_dir_lock` is a no-op twin on wasm32 |
| `SystemTime::now()` (no clock on wasm32-unknown-unknown) | `now()` falls back to a monotonic counter (ordering preserved) |
| `read_at` (unix `FileExt` / windows `seek_read` only) | portable seek+read fallback in `segment.rs` |
| getrandom (via aes-gcm) | `"js"` feature enabled from this crate (Web Crypto) |
| napi-rs | not used — this crate is the wasm-bindgen twin of it |

## Not yet

- Durable storage (Phase 2: OPFS-backed VFS or storage-trait swap).
- `Db::open()` — present in the engine, unusable without a filesystem.
- Real wall-clock `ts` on wasm (monotonic counter for now).
- wasm-bindgen-test suite (smoke test is plain Node against the built pkg).
