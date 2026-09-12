#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
The master pgwire harness: every client driver, one nedbd, one exit code.

# Why this exists

The psql suite proves that psql works. It cannot prove that *your framework*
works, and the difference is not cosmetic — every defect this harness was
built to catch was invisible to psql:

  * SQLAlchemy could not open a connection at all. Its dialect opens with
    `select pg_catalog.version()`, one character of qualification away from the
    canned `SELECT VERSION()` the endpoint recognised, so the very first
    statement of dialect initialisation was refused and NO SQLAlchemy
    application could connect.
  * A qualified column in a `WHERE` clause returned ZERO ROWS. Silently. NQL
    looks a field up flat, so `WHERE orders.status = 'paid'` asked for a field
    literally named "orders.status", found none, and answered empty — which
    reads exactly like "you have no paid orders". Every ORM qualifies its
    predicates, so every filtered query lied.
  * asyncpg refused integer parameters against catalogue columns, client-side,
    before sending a byte — because the server advertised `text` for
    `pg_type.oid`.

psql tolerates all three. A driver does not. So the drivers run here.

# The shape

One `nedbd` is booted, seeded, and handed to every suite as a `PgFixture`.
Suites register themselves with `@suite(name, requires="driver")` and are
discovered from any `tests/pgwire_*.py` file — adding a driver is one new file
and no edit to this one.

A missing driver SKIPS locally so the harness is usable on a laptop, and
FAILS under `NEDB_REQUIRE_ALL=1` so CI can never quietly run a reduced suite
and report success. That distinction is the whole reason the flag exists: a
self-skipping test is indistinguishable from a passing one.

Run:  python3 tests/pgwire_suite.py
CI:   NEDB_REQUIRE_ALL=1 python3 tests/pgwire_suite.py
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

# GitHub surfaces `::error` lines as check-run ANNOTATIONS, which are readable
# through the API without the `actions:read` scope a job log needs. That is not
# a cosmetic difference: with only the log, a red check on this job says
# "Process completed with exit code 1" and nothing else, and diagnosing it
# means guessing. Every failure here is therefore also emitted as an
# annotation, so the reason travels with the result.
_GHA = os.environ.get("GITHUB_ACTIONS") == "true"


def _annotate(level: str, title: str, message: str) -> None:
    if not _GHA:
        return
    # Annotations are one line: newlines and the `::` delimiter are escaped
    # rather than dropped, or a multi-line refusal would truncate to nothing.
    flat = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::{level} title={title}::{flat}", flush=True)


# ── the fixture ─────────────────────────────────────────────────────────────
class PgFixture:
    """A live nedbd, with one seeded DATABASE per suite.

    Every suite shares ONE daemon — booting one per driver would multiply the
    slowest part of the run by the number of drivers and prove nothing extra,
    since the drivers are the variable under test, not the server.
    
    But they do NOT share a database, and that is not a detail. The asyncpg
    suite asserts time travel, which means it must WRITE; the first version of
    this harness let that `UPDATE` land in the same `shop` database SQLAlchemy
    read afterwards, and four SQLAlchemy assertions failed on a value asyncpg
    had changed. A suite that fails because of what another suite did tells you
    nothing about the driver it was testing. So each gets its own database on
    the same process, seeded identically, and every suite can write freely.
    """

    def __init__(self, nedbd_bin: str, data_dir: str, http_port: int, pg_port: int):
        self.http_port = http_port
        self.pg_port = pg_port
        self.dbname = "shop"
        self.dsn = f"host=127.0.0.1 port={pg_port} dbname=shop user=nedb"
        self.url = f"postgresql://nedb@127.0.0.1:{pg_port}/shop"
        self._proc = subprocess.Popen(
            [nedbd_bin, "--data", data_dir,
             "--port", str(http_port), "--pg-port", str(pg_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            # The sweeper would checkpoint mid-suite; nothing here tests it.
            env={**os.environ, "NEDBD_SWEEP_S": "0"},
        )
        self._wait_ready()
        self._seed()

    def _wait_ready(self) -> None:
        for _ in range(80):
            if self._proc.poll() is not None:
                sys.exit(f"nedbd exited during startup with {self._proc.returncode}")
            time.sleep(0.25)
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.http_port}/health", timeout=2).read()
                return
            except Exception:                                        # noqa: BLE001
                continue
        sys.exit("nedbd never became healthy")

    def http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.http_port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"null")

    def _seed(self, dbname: str = "shop") -> None:
        """Three orders and one driver — the same shape the psql suite uses.

        `total` is an integer in every document on purpose: a field that is a
        number in one document and a string in another must be advertised as
        text, and that would make the parameter-typing assertions test the
        wrong thing.
        """
        self.http("POST", "/v1/databases", {"name": dbname})
        for id_, doc in [("1", {"status": "paid", "total": 120}),
                         ("2", {"status": "open", "total": 40}),
                         ("3", {"status": "paid", "total": 300})]:
            self.http("POST", f"/v1/databases/{dbname}/put",
                      {"coll": "orders", "id": id_, "doc": doc})
        self.http("POST", f"/v1/databases/{dbname}/put",
                  {"coll": "drivers", "id": "d1", "doc": {"name": "Bob", "active": True}})

    def for_suite(self, name: str) -> "SuiteDb":
        """A freshly seeded database of this suite's own."""
        dbname = f"shop_{name}".replace("-", "_")
        self._seed(dbname)
        return SuiteDb(self, dbname)

    def teardown(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class SuiteDb:
    """One suite's view of the daemon: its own database, the shared process.

    Exposes the same attribute names a suite would reach for on the fixture
    itself, so a suite never has to know that isolation exists.
    """

    def __init__(self, fx: "PgFixture", dbname: str):
        self._fx = fx
        self.dbname = dbname
        self.http_port = fx.http_port
        self.pg_port = fx.pg_port
        self.dsn = f"host=127.0.0.1 port={fx.pg_port} dbname={dbname} user=nedb"
        self.url = f"postgresql://nedb@127.0.0.1:{fx.pg_port}/{dbname}"

    def http(self, method: str, path: str, body=None):
        return self._fx.http(method, path, body)


# ── per-suite assertion recorder ────────────────────────────────────────────
class Checks:
    """Counts assertions so a suite reports coverage, not just pass/fail.

    A suite that silently stopped after its second assertion and a suite that
    ran all thirty both look like "ok" without this.
    """

    def __init__(self, label: str):
        self.label = label
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: str | None = None

    def skip(self, reason: str) -> None:                             # noqa: D401
        """Declare this suite unrunnable, with the reason.

        A suite that cannot run is NOT a suite that passed, and it is not a
        suite that failed either. Recording it as its own state is what lets
        `NEDB_REQUIRE_ALL=1` turn it into a failure in CI while leaving it a
        plain skip on a laptop — and what stops a skipped leg reporting
        "ok, 0 checks", which reads like success.
        """
        self.skipped = reason

    def eq(self, name, got, want):
        self.ok(name, got == want, f"got {got!r}, want {want!r}")

    def ok(self, name, cond, detail=""):
        if cond:
            self.passed += 1
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"      FAIL  {name}  — {detail}")
            _annotate("error", f"{self.label}: {name}", detail or "assertion failed")

    def raises(self, name, fn, needle):
        """Assert a refusal, and that its message NAMES the boundary.

        "it errored" is not the assertion — an error that says nothing useful
        is its own defect, and every refusal in this engine is supposed to name
        the construct it could not handle.
        """
        try:
            fn()
        except Exception as e:                                       # noqa: BLE001
            self.ok(name, needle.lower() in str(e).lower(),
                    f"refused, but not by name: {str(e)[:140]}")
            return
        self.ok(name, False, "it answered instead of refusing")


# ── suite registry ─────────────────────────────────────────────────────────
_SUITES: list[tuple[str, str | None, object]] = []


def suite(name: str, requires: str | None = None):
    """Register a suite. `requires` is an importable driver module name."""
    def wrap(fn):
        _SUITES.append((name, requires, fn))
        return fn
    return wrap


def _discover() -> None:
    sys.path.insert(0, str(HERE))
    for p in sorted(HERE.glob("pgwire_*.py")):
        if p.name == "pgwire_suite.py":
            continue
        importlib.import_module(p.stem)


# ── runner ─────────────────────────────────────────────────────────────────
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_nedbd() -> str | None:
    for cand in (os.environ.get("NEDBD_BIN"),
                 ROOT / "rust" / "target" / "release" / "nedbd",
                 ROOT / "rust" / "target" / "debug" / "nedbd",
                 shutil.which("nedbd-v2")):
        if cand and pathlib.Path(cand).exists():
            return str(cand)
    return None


def main() -> None:
    require_all = os.environ.get("NEDB_REQUIRE_ALL") == "1"
    binary = find_nedbd()
    if not binary:
        msg = "no nedbd binary — cargo build --release --bin nedbd -p nedb-engine"
        if require_all:
            sys.exit(f"FAIL: NEDB_REQUIRE_ALL=1 but {msg}")
        print(f"SKIP: {msg}")
        sys.exit(0)

    _discover()
    if not _SUITES:
        sys.exit("FAIL: no pgwire_*.py suites were discovered")

    tmp = tempfile.mkdtemp(prefix="nedb-pgwire-")
    print(f"nedbd: {binary}")
    fx = PgFixture(binary, os.path.join(tmp, "data"), free_port(), free_port())
    print(f"up on pg={fx.pg_port} http={fx.http_port}\n")

    passed, failed, skipped, total_checks = [], [], [], 0
    try:
        for name, requires, fn in _SUITES:
            if requires:
                try:
                    importlib.import_module(requires)
                except ImportError:
                    if require_all:
                        failed.append(name)
                        print(f"  FAIL  {name:<14} driver {requires!r} is not installed "
                              f"(NEDB_REQUIRE_ALL=1)")
                    else:
                        skipped.append(name)
                        print(f"  skip  {name:<14} driver {requires!r} is not installed")
                    continue
            c = Checks(name)
            try:
                fn(fx.for_suite(name), c)
            except Exception as e:                                   # noqa: BLE001
                c.failed.append(f"raised {type(e).__name__}: {e}")
                print(f"      FAIL  {name} raised {type(e).__name__}: {str(e)[:200]}")
            total_checks += c.passed
            if c.failed:
                failed.append(name)
                print(f"  FAIL  {name:<14} {c.passed} ok, {len(c.failed)} failed")
            elif c.skipped is not None:
                if require_all:
                    failed.append(name)
                    print(f"  FAIL  {name:<14} {c.skipped} (NEDB_REQUIRE_ALL=1)")
                    _annotate("error", f"{name} did not run",
                              f"{c.skipped} — NEDB_REQUIRE_ALL=1 makes a skipped "
                              f"driver a failure, because a self-skipping test is "
                              f"indistinguishable from a passing one")
                else:
                    skipped.append(name)
                    print(f"  skip  {name:<14} {c.skipped}")
            else:
                passed.append(name)
                print(f"  ok    {name:<14} {c.passed} checks")
    finally:
        fx.teardown()

    print(f"\n{'=' * 62}")
    print(f"pgwire drivers: {len(passed)}/{len(passed) + len(failed)} suites, "
          f"{total_checks} checks passed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    if failed:
        print("FAILED: " + ", ".join(failed))
        _annotate("error", "pgwire drivers",
                  "failed suites: " + ", ".join(failed))
        sys.exit(1)
    _annotate("notice", "pgwire drivers",
              f"{len(passed)} suites, {total_checks} checks passed"
              + (f", skipped: {', '.join(skipped)}" if skipped else ""))
    if skipped and require_all:
        print("FAILED: suites skipped under NEDB_REQUIRE_ALL=1")
        sys.exit(1)


if __name__ == "__main__":
    # Re-enter through the MODULE rather than staying in `__main__`.
    #
    # Every suite file does `from pgwire_suite import suite`. Run directly,
    # this file is `__main__`, so that import creates a SECOND module object
    # with its own empty `_SUITES` — each suite registers itself into a
    # registry the runner never reads, and the runner reports "no suites
    # discovered" while every suite is perfectly fine. Importing ourselves
    # first makes both halves the same module.
    sys.path.insert(0, str(HERE))
    import pgwire_suite

    pgwire_suite.main()
