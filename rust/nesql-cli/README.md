<!-- SPDX-FileCopyrightText: 2026 INTERCHAINED LLC -->
<!-- SPDX-License-Identifier: BUSL-1.1 -->

# nesql

The command-line client for [NEDB](https://github.com/Eth-Interchained/nedb) — a
database that cannot quietly forget. `nesql` opens a store directly: no daemon,
no port, no server to start.

```console
$ cargo install nesql-cli
$ nesql --db ./store status
```

## neSQL = PostgreSQL SQL + NEDB SQL

```
neSQL  =  PostgreSQL SQL   ·  inherited whole, not reimplemented
       +  NEDB SQL         ·  what a permanent, hash-chained store can answer
```

**We inherit, then we gain.** The SQL half is PostgreSQL's real grammar —
`gram.y`, 19,513 lines and 492 keywords, vendored from 17.4 with its licence
intact. Not a subset and not a lookalike: the definition every other tool was
built against. The NEDB half is the clauses PostgreSQL has no spelling for,
because a store that overwrites has nothing to point them at.

`query` takes either, and picks by the leading keyword:

```console
$ nesql --db ./store query "SELECT who, total FROM orders ORDER BY total DESC"
{"who":"globex","total":250}
{"who":"acme","total":100}
(2 rows)

$ nesql --db ./store query "FROM orders WHERE total > 150"
{"who":"globex","total":250,...}
(1 rows, 1 scanned)
```

Routing is **structural, not guessed**. NQL's form begins `FROM`; PostgreSQL has
no statement form that begins with `FROM`, so the first word partitions the two
vocabularies rather than hinting at them. A first word in neither is refused
*naming both*. `--nql` / `--sql` force one when you want that dialect's own
error instead of a routing error — `query --nql "SELECT 1"` tells you
`expected keyword FROM`, which is the useful answer when you are debugging a
rejection.

## What NEDB adds

```sql
SELECT who FROM orders AS OF SYSTEM TIME 412      -- the exact state at a sequence
SELECT who FROM orders VALID AS OF '2026-01-01'   -- what was BELIEVED TRUE then
SELECT who FROM orders SEARCH 'acme'              -- full text over the document
SELECT who FROM orders TRACE caused_by            -- the causal chain that produced it
SELECT who FROM orders TRAVERSE ships_to          -- one hop along a named relation
```

These are table-level qualifiers, so ordinary SQL composes **around** them:

```sql
SELECT o.who FROM orders SEARCH 'acme' o JOIN orders n ON o._id = n._id
```

## Built for scripts as much as for people

`--json` emits exactly one JSON object on stdout. Engine diagnostics go to
stderr, so a pipe stays clean. The exit code carries the verdict:

| code | meaning |
| --- | --- |
| `0` | success — the thing was done, or the check ran and passed |
| `1` | failure — the operation ran and did not succeed |
| `2` | usage — the command line was not understood, or was ambiguous |
| `3` | **could not determine** — the check could not run (history pruned) |
| `4` | not found |
| `5` | unsupported — a version or format this build does not know |

**`3` is the one that matters.** A pruned history is not a corrupt one, and an
operator who cannot tell them apart will either ignore a real alarm or panic at
a routine one. `root verify` reports the stored record and the recomputation as
two independent facts and never collapses them:

```console
$ nesql --db ./store root verify
at_seq         2
root_record    valid
recomputation  matches
exit           0
```

`root_record valid` with `recomputation unavailable` and exit 3 is a pruned
store answering honestly. Only `recomputation DIFFERS` means something is wrong.

## The rest of the surface

| | |
| --- | --- |
| `status` | path, sequence, collections, state root, history floor |
| `log` | writes in a range, newest first |
| `inspect` | a collection, a document, a sequence, or a persisted root |
| `root` | `create` · `inspect` · `verify` · `list` |
| `diff` | what changed between two sequences |
| `tag` | name a sequence, immutably |
| `branch` · `merge` | branch a store, replay it back, resolve conflicts |
| `grammar` | the command surface and its digest |
| `constitution` | the engine's guarantees, and whether this build matches |

`inspect` names things **by kind**, because the kinds are not distinguishable by
shape — `collection:users`, `doc:users/u1`, `seq:42`, `root:42`. A bare `42` is
**refused**: it could be `seq:42` or `root:42`, and guessing for you is how a
tool answers a question you did not ask.

## Licence

BUSL-1.1 — free in production under USD $1M annual revenue, converting to
Apache 2.0 automatically. See [`LICENSE`](../../LICENSE).

The vendored PostgreSQL sources remain under the PostgreSQL Licence,
reproduced verbatim. Our thanks to the PostgreSQL Global Development Group —
thirty years of grammar we did not have to guess at.

---

**© INTERCHAINED LLC** · built with **Vex** (Claude Opus 5)
