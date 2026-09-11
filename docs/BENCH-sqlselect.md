<!--
SPDX-License-Identifier: BUSL-1.1
SPDX-FileCopyrightText: © 2026 INTERCHAINED LLC × Claude Sonnet 4.6
-->

# SQL evaluator baselines — nested loop vs hash join

Recorded at engine **4.0.0**, PRs #116 and #117, on the CI-class sandbox this
repo is developed in (Linux x86-64). Reproduce with:

```bash
cargo run --release --example sqlbench
```

## Why this file exists

So that the next person to change join execution can tell whether they made it
faster or only believe they did. The inputs are generated from a **fixed seed**
inside `rust/nedb-v2/examples/sqlbench.rs`, so a number measured later is
comparable with a number measured here. That is the whole value — an absolute
millisecond count on unspecified data is not evidence of anything.

These are **not** marketing figures. The machine is a shared sandbox, the
timings move ±20% run to run, and the relation sizes are small because the
nested loop is quadratic and the largest shape has to finish. Read the
*ratios* and the *trend*, not the absolute milliseconds.

## How to read it honestly

* Rows marked `no join` have no join at all. Both columns time the same scan;
  the ratio is noise.
* Rows marked `both ran Nested Loop — no hash path available` have no provable
  equality key, so both columns time **the same code twice**. A ~1.00x here is
  the harness working correctly, not a disappointing result.
* The strategy in each row is read back from the execution report, not from
  the flag that was requested. A benchmark that times the nested loop twice
  while believing one run was a hash join would manufacture a speedup out of
  nothing, so the harness checks.
* Row counts are compared between the two strategies. A mismatch is printed as
  `!! ROW COUNT DIFFERS` rather than being reported as a speed difference,
  because two strategies answering different questions cannot be compared at
  all.

## Results

### orders=1000, customers=500, 250 distinct keys (median of 5)

| workload | nested (ms) | hash (ms) | speedup | rows |
|---|---:|---:|---:|---:|
| scan | 0.90 | 0.86 | — *(no join)* | 1000 |
| filtered scan | 0.68 | 0.67 | — *(no join)* | 117 |
| equality join | 366.62 | 4.05 | **90.6x** | 1922 |
| left join | 373.27 | 4.03 | **92.5x** | 1961 |
| join + selective pred | 370.32 | 3.85 | **96.3x** | 14 |
| join + broad pred | 369.51 | 6.98 | **52.9x** | 1750 |
| join + sort | 403.51 | 6.35 | **63.6x** | 1922 |
| join + limit | 5.13 | 0.97 | **5.3x** | 20 |
| non-equality join | 344.69 | 346.33 | — *(same path)* | 2500 |

### orders=3000, customers=1000, 500 distinct keys (median of 3)

| workload | nested (ms) | hash (ms) | speedup | rows |
|---|---:|---:|---:|---:|
| scan | 2.88 | 2.88 | — *(no join)* | 3000 |
| filtered scan | 2.21 | 2.26 | — *(no join)* | 320 |
| equality join | 2204.29 | 16.00 | **137.7x** | 5708 |
| left join | 2208.46 | 17.48 | **126.3x** | 5854 |
| join + selective pred | 2229.06 | 12.77 | **174.5x** | 56 |
| join + broad pred | 2212.43 | 19.44 | **113.8x** | 5144 |
| join + sort | 2202.42 | 20.90 | **105.4x** | 5708 |
| join + limit | 10.37 | 2.57 | **4.0x** | 20 |
| non-equality join | 2079.45 | 2125.99 | — *(same path)* | 14000 |

### orders=8000, customers=1500, 1500 distinct keys (single run)

| workload | nested (ms) | hash (ms) | speedup | rows |
|---|---:|---:|---:|---:|
| scan | 8.09 | 7.64 | — *(no join)* | 8000 |
| filtered scan | 6.45 | 6.27 | — *(no join)* | 781 |
| equality join | 8776.76 | 28.77 | **305.0x** | 7578 |
| left join | 8837.90 | 30.17 | **293.0x** | 8000 |
| join + selective pred | 8774.78 | 27.07 | **324.1x** | 64 |
| join + broad pred | 8929.38 | 37.50 | **238.1x** | 6816 |
| join + sort | 8804.33 | 36.91 | **238.5x** | 7578 |
| join + limit | 29.59 | 6.72 | **4.4x** | 20 |
| non-equality join | 8367.82 | 8569.10 | — *(same path)* | 40500 |

## What the numbers say

**The speedup grows with the input, which is the only part that really
matters.** 90x → 138x → 305x across the three shapes is the signature of
O(n·m) being replaced by O(n+m): the ratio is a function of relation size, so
it will keep widening. A fixed multiplier would have suggested a constant-factor
win instead, and that distinction is the difference between an optimisation and
a micro-optimisation.

**A nested-loop join over user collections was genuinely not shippable.** 8.8
seconds to join 8000 rows against 1500 is not a tuning problem, it is a wrong
algorithm — which is exactly why the nested loop proved the semantics and then
stopped being the only option.

**Scans are untouched**, as they should be: identical numbers in both columns
confirm this change is confined to join execution and did not perturb the
surrounding pipeline.

**`join + limit` was the most interesting remaining gap, and #117 closed it.**
`LIMIT` used to be applied *after* the whole join had been materialised, so
neither strategy stopped early. With the row budget, the join stops as soon as
it has enough rows:

| shape | nested before | nested after | hash before | hash after |
|---|---:|---:|---:|---:|
| 1000 x 500 | 368.21 | **5.13** | 4.21 | **0.97** |
| 3000 x 1000 | 2191.93 | **10.37** | 14.82 | **2.57** |
| 8000 x 1500 | 8848.69 | **29.59** | 32.12 | **6.72** |

At the largest shape that is **299x** off the nested loop and **4.8x** off the
hash join. Note what the speedup *column* now shows for that row: a mere 4.4x,
because both strategies got faster. The column compares strategies, not
releases — which is exactly why the before/after has to be stated separately
rather than read off the table.

The residual 6.7ms is almost entirely relation materialisation (the resolver
hands back an owned `Vec`), not join work. Removing that needs a streaming
resolver, not a cleverer join.

**A `WHERE` clause still disqualifies the budget**, because filtering happens
after the join here — capping the join would starve the filter. Fusing `Filter`
into the join is the natural next step, and it is what would let
`join + selective pred` (currently 27ms to return 64 rows) stop early too.

**`join + broad pred` and `join + sort` gain least**, which makes sense —
their cost is dominated by materialising and then sorting ~5–7k output rows,
not by finding the matches. Predicate pushdown is the lever there, and it has
to be done conservatively: a predicate on the nullable side of a `LEFT JOIN`
means something different in `WHERE` than in `ON`, and both spellings are
pinned in `tests/sql_semantics_corpus.rs`.

## Caveats worth stating

* The `Resolver` contract hands back an owned `Vec<Value>`, so every execution
  clones the whole relation before doing any work. That cost is charged to
  both strategies equally and is real in production too, but it means these
  numbers include materialisation and are **not** a measure of join throughput
  alone.
* Key cardinality is a controlled variable (`distinct keys`), because bucket
  depth drives hash-join cost. A single hot key degenerates a hash join toward
  the nested loop's pair count; that case is covered for *correctness* in the
  differential tests but is not benchmarked here yet.
* Timings are medians, not minimums, and the largest shape is a single run.
  Treat sub-2x differences as noise.
