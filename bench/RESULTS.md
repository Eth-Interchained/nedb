# NEDB Benchmark Results

**Version:** `8.0.0`  
**Python:** `3.11.7`  
**Platform:** `Darwin x86_64`  
**Date:** `2026-09-15`  

> Run: `python3 bench/benchmarks.py --save`

---

### Core operations

| Operation | Throughput | Latency (avg) |
|-----------|-----------|---------------|
| PUT (replace, no index) | 65.2K/s | 15.34 µs |
| GET (point read, HEAD) | 1.39M/s | 0.72 µs |
| GET (AS OF — time-travel) | 1.77M/s | 0.56 µs |

### Index performance

| Operation | Throughput | Latency (avg) |
|-----------|-----------|---------------|
| QUERY: eq filter, no index (scan) | 776.0K/s | 1.29 µs |
| QUERY: eq filter, eq index | 1.97M/s | 0.51 µs |
| QUERY: ORDER BY, ordered index, LIMIT 20 | 500.7K/s | 2.00 µs |
| QUERY: SEARCH, inverted index | 507.3K/s | 1.97 µs |

### Adapter overhead (SQL · Redis · AutoIndex)

| Operation | Throughput | Latency (avg) |
|-----------|-----------|---------------|
| NQL: WHERE eq (raw) | 2.85M/s | 0.35 µs |
| SQL: SELECT WHERE (adapter → NQL) | 2.55M/s | 0.39 µs |
| Redis: HSET ×10 (adapter) | 55.0K/s | 18.17 µs |
| Redis: HGET ×10 (adapter) | 512.8K/s | 1.95 µs |
| AutoIndexDB: same query via wrapper | 2.66M/s | 0.38 µs |

### Persistence: in-memory vs AOF

| Operation | Throughput | Latency (avg) |
|-----------|-----------|---------------|
| PUT in-memory (no AOF) | 78.3K/s | 12.76 µs |
| PUT durable (AOF + fsync) | 7.9K/s | 126.79 µs |
| RELOAD from AOF (1000 ops) | — | 48.3 ms total |

### NEDB embedded vs nedbd HTTP

| Operation | Throughput | Latency (avg) |
|-----------|-----------|---------------|
| NEDB embedded query (in-process) | 1.02M/s | 0.98 µs |
| nedbd HTTP query (over TCP) | 43.0K/s | 23.27 µs |

---

## Reading these numbers honestly

Two rows above will be misread if taken at face value. Both are properties of
the harness, not of the engine.

**`GET (AS OF)` is not comparable to `GET (point read)`.** The point-read loop
walks all `MEDIUM` (10,000) keys; the AS OF loop walks `min(n, 1000)` — a
tenth of the working set, which fits cache far better. The resulting "AS OF is
27% faster than a point read" is an artifact of that difference. Time-travel
reads are *competitive* with point reads here, which is the real and still
notable result; they are not faster. Fixing the comparison means equalising the
two loops in `bench/benchmarks.py` and re-running.

**`SQL: SELECT WHERE (adapter → NQL)` measures the PYTHON reference
translator**, via `from nedb.sql import sql_exec` — not the Rust evaluator that
serves nedbd, the pgwire port and the native wheels. The label is accurate for
what it measures, and the distinction matters because nothing in this file
exercises the Rust SQL evaluator at all. Those baselines live in
`docs/BENCH-sqlselect.md` and come from:

```text
cargo run --release --example sqlbench
cargo run --release --example fusebench
```

**Platform note:** taken on `Darwin x86_64`. These are Intel Mac numbers, not
Apple-silicon ones, so they are not comparable with any arm64 run.
