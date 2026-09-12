#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
The Node leg: runs `tests/pgwire_node.js` and folds its assertions into this
harness's count.

The JS file owns the queries and this file owns the fixture, so Node's checks
are counted exactly like every other driver's rather than reduced to a single
"the node suite passed". A suite that stopped after two of its twenty
assertions and one that ran all twenty must not look the same.

`requires` cannot express "the npm package `pg` is installed", so the check is
made here: a missing `pg` (or a missing `node`) SKIPS with a reason, and under
`NEDB_REQUIRE_ALL=1` the runner turns that into a failure — which is how CI
guarantees the Node leg actually ran rather than quietly sat out.
"""
import json
import shutil
import subprocess

from pgwire_suite import HERE, ROOT, suite


def _node_can_require_pg(node: str) -> bool:
    """Is the `pg` package resolvable from the repo root?

    Checked by asking Node, not by looking for a directory: a package can be
    hoisted, linked, or installed in a parent, and only the resolver knows.
    """
    r = subprocess.run(
        [node, "-e", "require.resolve('pg'); process.stdout.write('ok')"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0 and "ok" in r.stdout


@suite("node-pg")
def run(fx, c):
    node = shutil.which("node")

    if not node or not _node_can_require_pg(node):
        # Recorded as a SKIP with its reason, which the runner turns into a
        # failure under NEDB_REQUIRE_ALL=1. Never silent either way.
        c.skip("node is not installed" if not node else
               "the npm package 'pg' is not installed (npm install pg)")
        return

    r = subprocess.run(
        [node, str(HERE / "pgwire_node.js"), str(fx.pg_port), fx.dbname],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        c.ok("the node runner exited cleanly", False,
             (r.stderr or r.stdout).strip()[:300])
        return

    saw_any = False
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Stray output is reported rather than ignored: it usually means
            # the JS side printed a warning that will mask a real failure next
            # time somebody reads this log.
            print(f"      note  node said: {line[:160]}")
            continue
        if "skip" in rec:
            c.skip(rec["skip"])
            return
        saw_any = True
        c.ok(rec["name"], rec["ok"], rec.get("detail", ""))

    c.ok("the node suite reported its assertions", saw_any,
         "it printed nothing at all")
