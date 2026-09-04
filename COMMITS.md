# NEDB — Commit & Release Log

Living progress log for the NEDB engine and its published **distributions**. The engine is the source of truth; the distributions (`crypto-database`, `aof-db`) and downstream consumers (itcd) are tracked where they exercise engine capabilities.

_Last updated: 2026-09-04 — release **v2.8.5** (embedded bindings run the 1 s manifest ticker; SIGKILL loses at most one tick, was: everything since open)._

---

## Releases

| Version | What shipped | Registries |
|---|---|---|
| **v2.8.5** | **Embedded durability parity with nedbd.** `NedbCore.open()` in Node and Python now starts the manifest ticker (1 s; `NEDB_FLUSH_MS` tunes/disables) so a `SIGKILL` / OOM-kill / power cut loses at most one tick of acknowledged writes — before, only the exit hooks flushed and an embedded app lost every write since open. Found by CHALK / Sports-Rater (acknowledged fan writes gone after `kill -9`). Also: the Node exit-flush wrapper had **never shipped** (CI's `napi build` overwrote `index.js` with the generated loader) and could not have worked (`open` is a non-writable static) — now wraps by subclass, CI builds with `--js native.js` and gates the publish on the wrapper + `test/durability.test.mjs` (SIGKILL kept via ticker · SIGTERM kept via wrapper · ticker-off SIGKILL lost, the control). | PyPI · npm · crates.io |
| **v2.4.468** | Distro **npm packages now bundle macOS native addons** — `release-distros` waits for Codemagic's `<distro>.darwin-arm64/-x64.node` and assembles them before `npm publish` (mirrors the flagship), so crypto-database/aof-db ship full **mac + linux + windows**. Adds the committed **`scripts/release.py "vFROM" "vTO"`** — a one-command, idempotent tri-distribution release tool. `nedb-engine` default realigns everywhere (2.4.468 > 2.4.444). | npm · PyPI · crates.io |
| **v2.4.68** | First **fully-green 3-distribution release**: **nedb-engine** (flagship) + **crypto-database** (verifiable v2/v3 DAG) + **aof-db** (fast append-only) all ship on one tag across npm/PyPI/crates. Two fixes brought it green: the engine crate `rust/nedb-v2` was stranded at `2.4.3` while each wrapper required `^2.4.x` (broke the napi/cargo build) — every version line aligned; and the `release-distros` **startup_failure** caused by double-quoted `"refs/tags/"` in three `if:` conditions (GitHub Actions expressions require single quotes). | npm · PyPI · crates.io |
| **v2.4.3** | **3-distribution split introduced.** Distros live in org forks (`crypto-datab/cryptoDB` → crypto-database, `nitro-db/aof-DB` → aof-db), submoduled under `distributions/`; the central `codemagic.yaml` builds all 6 macOS wheels and `release-distros.yml` publishes the distros — no workflows in the forks. Each distro carries a distinct README + napi/crate/PyPI identity. (Flagship shipped; distro naming + CI iterated to green at v2.4.68.) | PyPI · npm · crates.io (flagship) |
| **v2.4.2** | Bugfix/polish on the complete cross-platform line. `nedbd-v2` gains **real CLI parsing** — `--dag-v3`, `--data`, `--fast-fsync`, `--help`, `--version` are recognized flags (were silently swallowed as the positional data dir, so `--dag-v3` never engaged v3). Ships a cinematic `npm test` smoke demo (`test/smoke.mjs`, now in `package.json` `files`) touring v1→v2 migration · v2 DAG · v3 segments · a causal rideshare audit. Docs/SPEC updated; 9 manifests 2.4.1 → 2.4.2. | PyPI · npm · crates.io |
| **v2.4.1** | CI-fixup re-tag — first **complete** cross-platform publish (all native wheels incl. macOS + the universal wheel) since the Codemagic `GITHUB_TOKEN` fix. Skeleton version bump, no engine change; marked stable in README. | PyPI · npm · crates.io |
| **v2.4.0** | Cycle-closing minor — the v3 storage line consolidated & formally spec'd (`docs/SPEC.md` §3: v2 object store + v3 segment substrate + durability/fast-fsync). No new engine code; packages bumped 2.3.3333 → 2.4.0. | PyPI · npm · crates.io |
| **v2.3.3333** | Opt-in macOS fast-fsync for the v3 segment store (`NEDB_FAST_FSYNC`, default off) — plain `fsync(2)` instead of `F_FULLFSYNC`, no-op off-mac. Closes the 3's cycle; next is 2.4.0. | PyPI · npm · crates.io |
| **v2.3.333** | Comprehensive v3 documentation (README section + this log + ideas.md). Engine code unchanged from 2.3.33. | PyPI · npm · crates.io |
| **v2.3.33** | Durable flush-on-close (`Db::drop` → `flush_all`), cross-platform Windows-safe id-index (percent-encoded filesystem-unsafe ids), idempotent re-writes; `cargo test -p nedb-engine` green (43/43). | PyPI · npm · crates.io |
| **v2.3.3** | NEDB **v3** segment/pack object store landed behind `--dag-v3` (Phases 1–3: segments, compaction/pruning, `.idx` sidecars). Default off. | PyPI · npm · crates.io |
| v2.2.33 | Graph AS-OF time-travel + Node test suite + mini-chain example. | PyPI · npm · crates.io |

---

## The three distributions

All three are built from this one repo and ship the **same version** on every tag.

| Product | Identity | npm | PyPI | crates.io | Fork (distro layer) |
|---|---|---|---|---|---|
| **nedb-engine** | flagship — the full engine | `nedb-engine` | `nedb-engine` | `nedb-engine` | _(this repo)_ |
| **crypto-database** | verifiable v2/v3 — content-addressed Merkle DAG, AS OF / TRACE, BLAKE2b | `crypto-database` | `cryptodb` ¹ | `crypto-database` | `crypto-datab/cryptoDB` → `distributions/crypto-database` |
| **aof-db** | fast/lightweight — append-only op-log, minimal footprint | `aof-db` | `aof-db` | `aof-db` | `nitro-db/aof-DB` → `distributions/aof-db` |

¹ `crypto-database` is taken on PyPI by a third party, so the verifiable distro publishes its wheel as `cryptodb` (which we own). npm + crates.io use `crypto-database`.

---

## NEDB engine — recent commits (newest first)

| Commit / PR | Summary |
|---|---|
| nedb #27 | feat(ci): `distro-publish-npm` waits for Codemagic's `<distro>.darwin-*.node` and assembles full platform coverage before publish; add **`scripts/release.py`** (idempotent `vFROM`→`vTO`) → tag `v2.4.468` |
| nedb #26 | release: align the engine crate + every manifest to one version (fix stranded `rust/nedb-v2` @ 2.4.3 vs wrapper `^2.4.x`), repoint submodules → tag `v2.4.68` |
| nedb #25 | fix(ci): `release-distros` startup_failure — single-quote `'refs/tags/'` in three `if:` expressions (valid YAML, invalid workflow) |
| nedb #24 | fix(ci): point codemagic distro builds at `distributions/crypto-database` + `distributions/aof-db` (post-rename) |
| nedb #22 | feat(distros): submodule crypto-database + aof-db under `distributions/`; central `release-distros.yml` + 6-build `codemagic.yaml` → tag `v2.4.3` |
| `d0f5e92` | perf(v3): opt-in macOS fast fsync (`NEDB_FAST_FSYNC`) — plain `fsync(2)` instead of `F_FULLFSYNC` (#16) |
| `d49dcbe` | fix(engine): cargo-test green — Windows-safe id-index, durable `Drop`, idempotent write (#14) |
| `2eaa0ab` | fix(index): filesystem-safe id-index filenames so link ids persist on Windows |
| `5fa3794` | fix(engine): durable flush-on-close + idempotent re-write; fix nql test-harness temp-dir lifetime |
| `cfdd6c9` | feat(store): NEDB v3 Phase 2 (compaction/pruning) + Phase 3 (`.idx`); bump to 2.3.3 |
| `3888267` | feat(store): NEDB v3 segment/pack ObjectStore behind `--dag-v3` (default off) |

---

## v3 in the wild — itcd integration (downstream)

itcd (Bitcoin Core 0.21 fork; NEDB replaces LevelDB for chainstate + block index via `nedb-ffi`) runs on the v3 segment store via a `-dagv3` flag, with `-dagfastsync` for the macOS fast-fsync path. A warm boot ~500k blocks deep resumed the chainstate from NEDB in seconds and verified its canonical prefix against a peer (Proof-of-Prefix) before syncing forward.

| Commit / PR | Summary |
|---|---|
| `52684625` (itcd #55) | feat(nedb): itcd `-dagv3` — v3 segment store via FFI |
| `ea2c178` | nedb-ffi: pin `nedb-engine`; add `nedb_set_dag_v3()`; `dbwrapper_nedb.cpp` flips it before `nedb_open`; register `-dagv3` in `init.cpp` |

**Measured win** (real chainstate `FlushStateToDisk`, `-dagv3`): 2,002 coins / 275 kB in **1.93 s**, 2,549 coins / 366 kB in **1.71 s** — one `fsync` per batch, not per object. The old loose store's ~185 writes/s metadata ceiling is gone.

---

## Agent PRs

| Repo | PR | Title |
|---|---|---|
| nedb | #10–#14 | NEDB v3 Phases 1–3 (segment store, compaction/pruning, `.idx`) + cargo-test green → tag `v2.3.33` |
| nedb | #17–#21 | release line: docs/spec → `v2.4.0`; Codemagic `GITHUB_TOKEN` CI fix; stable re-tag `v2.4.1`; `nedbd-v2` CLI + smoke demo → `v2.4.2`; smoke pre-publish gate |
| nedb | #22 | 3-distribution split: submodule crypto-database + aof-db, central `release-distros.yml` + 6-build `codemagic.yaml` → tag `v2.4.3` |
| nedb | #24–#27 | distro CI fixes (codemagic paths, `release-distros` startup_failure), full version alignment, distro-npm macOS mac-wait + `scripts/release.py` → tags `v2.4.68`, `v2.4.468` |
| crypto-datab/cryptoDB | #1–#8 | distro layer + distinct README + version alignments (→ `crypto-database`) |
| nitro-db/aof-DB | #1–#8 | distro layer + distinct README + version alignments (→ `aof-db`) |
| itcd | #55 | feat(nedb): `-dagv3` — chainstate/block-index on the NEDB v3 segment store via FFI |
