# nedb-wrap

Embed **NEDB causal provenance** into the databases you already run — the Rust leg of the wrap adapter family.

The [`nedb-engine`](https://crates.io/crates/nedb-engine) crate is the DAG storage engine. This crate layers the **wrap surface** on top: register host key patterns → backfill existing data → shadow future writes → full NEDB API (`query`/`AS OF`/`TRACE caused_by`/`verify`).

```rust
use nedb_wrap::Surface;

let s = Surface::in_memory();               // or Surface::open(Path::new("./nedb-data"))?
s.register("driver:*", "driver");
s.shadow_writes.store(true, std::sync::atomic::Ordering::Relaxed);

// after your own Redis/SQL write succeeds:
s.shadow("driver:d1", serde_json::json!({"name": "Bob", "status": "active"}), true)?;

s.query(r#"FROM driver WHERE status = "active""#)?;
assert!(s.verify());                        // BLAKE2b chain intact
```

## Surface contract (same in Python / JS / Rust)

| Step | Call | What it does |
|---|---|---|
| 1 | `register(pattern, collection)` | map host key globs (`driver:*`) to NEDB collections |
| 2 | backfill | import existing host records once (host adapter-specific) |
| 3 | `shadow_writes = true` | future host writes are chained into the DAG |
| 4 | `.query("FROM ...")` | NQL over the shadowed data — time-travel, provenance, verification |

## Adapters

- **`Tracked`** (default, zero deps) — wrap any Redis/SQL connection; after each successful write call `shadow_cmd(cmd, key, value)`. SET-family → full replace, HSET/INCR-family → merge, DEL → tombstone, unknown → raw tamper-evidence chain entry.
- **`redis` feature** — `Tracked<redis::Connection>` integration helpers.
- SQL/Mongo/Postgres embedding — use `Surface` directly from your ORM hooks; the contract is 3 calls.

## Isolation guarantee

NEDB **never writes to the host database's namespace**. Shadow data lives only in the embedded engine.

## Engine modes

- `Surface::in_memory()` — zero disk I/O, ephemeral
- `Surface::open(path)` — durable v2 DAG store (v3 substrate via `NEDB_DAG_V3`)
- `Surface::open_encrypted(path, tmk)` — AES-256-GCM at rest, key derived exactly as `nedbd` derives it

## Family

| Language | Package |
|---|---|
| Python | `pip install nedb-engine` |
| Node.js | `npm install nedb-engine` → `require('nedb-engine/wrap')` |
| **Rust** | **`cargo add nedb-wrap`** ← you are here |

MIT © 2026 INTERCHAINED LLC
