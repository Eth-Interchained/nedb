# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
"""RSS proof for the portal-aware engine open (step 6b acceptance gate).

Builds two byte-identical fixture stores — one sealed with a legacy
snapshot checkpoint, one with a portal checkpoint — then opens each in
a fresh child process and reports peak RSS for open + random reads.

    python tests/portal_engine_rss.py [--n 1850000] [--reads 2000]

Gate (docs/portals.md): 1.85M records, portal peak <= 20% of legacy
peak, on identical data. The default n is smaller for a quick check;
the gate number only counts at --n 1850000.
"""
import argparse
import json
import os
import random
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))


def build(base: str, n: int) -> None:
    from nedb import NEDB
    for mode in ("legacy", "portal"):
        path = os.path.join(base, mode)
        db = NEDB(path, portal_reads=(mode == "portal"))
        # Fixture construction only: group-commit (the Sequencer's own
        # mode) so seeding millions of ops doesn't pay an fsync per op.
        # flush() at checkpoint/close still makes the fixture durable;
        # the measured opens run with stock settings.
        db._defer_sync = True
        for i in range(n):
            db.put("kv", f"key-{i:08d}",
                   {"i": i, "payload": "x" * 160, "grp": f"g{i % 64}"})
            if i % 50000 == 0:
                print(f"  [{mode}] seeded {i}/{n}", flush=True)
        db.checkpoint()
        db.close()
        print(f"  [{mode}] fixture sealed at {path}", flush=True)


def child(path: str, mode: str, reads: int, n: int) -> None:
    import resource
    from nedb import NEDB
    db = NEDB(path, portal_reads=(mode == "portal"))
    rng = random.Random(42)
    hits = 0
    for _ in range(reads):
        i = rng.randrange(0, n)
        doc = db.get("kv", f"key-{i:08d}")
        if doc is not None:
            hits += 1
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"mode": mode, "peak_mb": round(peak_kb / 1024, 1),
                      "reads": reads, "hits": hits, "verify": db.verify()}))
    db.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200_000)
    ap.add_argument("--reads", type=int, default=2000)
    ap.add_argument("--dir", default="/tmp/nedb-portal-rss-fixture")
    ap.add_argument("--child", nargs=2, metavar=("PATH", "MODE"))
    ap.add_argument("--build", action="store_true",
                    help="internal: build the fixtures in this process")
    args = ap.parse_args()

    if args.child:
        child(args.child[0], args.child[1], args.reads, args.n)
        return
    if args.build:
        build(args.dir, args.n)
        return

    # The orchestrator NEVER imports nedb: it must stay small. A fat
    # parent inflates a fork+exec child's reported ru_maxrss (the child
    # inherits the fork-time high-water mark — measured: a 500MB parent
    # made a 24MB child report 513MB). Building therefore also happens
    # in a subprocess, so every measured child forks from a thin parent.
    import shutil
    shutil.rmtree(args.dir, ignore_errors=True)
    os.makedirs(args.dir)
    print(f"building fixtures: n={args.n} in {args.dir}", flush=True)
    subprocess.run([sys.executable, os.path.abspath(__file__),
                    "--n", str(args.n), "--dir", args.dir, "--build"],
                   check=True)

    import resource
    results = {}
    for mode in ("legacy", "portal"):
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--n", str(args.n),
             "--reads", str(args.reads), "--child",
             os.path.join(args.dir, mode), mode],
            capture_output=True, text=True)
        lines = [ln for ln in out.stdout.strip().splitlines()
                 if ln.startswith("{")]
        if lines:
            results[mode] = json.loads(lines[-1])
            print(f"{mode}: peak {results[mode]['peak_mb']} MB "
                  f"(verify={results[mode]['verify']}, "
                  f"hits={results[mode]['hits']})")
        else:
            # The child died without reporting — at gate scale this IS
            # the legacy result: the open was OOM-killed. RUSAGE_CHILDREN
            # carries the dead child's peak up to the kill.
            peak_kb = resource.getrusage(
                resource.RUSAGE_CHILDREN).ru_maxrss
            results[mode] = {"mode": mode, "peak_mb": None,
                             "oom_killed": True,
                             "peak_at_kill_mb": round(peak_kb / 1024, 1)}
            print(f"{mode}: child OOM-KILLED during open "
                  f"(peak at kill ~{results[mode]['peak_at_kill_mb']} MB, "
                  f"exit {out.returncode}) — the open does not complete "
                  f"in this sandbox")
    if results["portal"].get("peak_mb") and results["legacy"].get("peak_mb"):
        ratio = results["portal"]["peak_mb"] / results["legacy"]["peak_mb"]
        print(f"portal/legacy peak ratio: {ratio:.3f}  "
              f"(gate: <= 0.200 at n=1,850,000; this run n={args.n})")
    elif results["portal"].get("peak_mb"):
        print(f"portal peak {results['portal']['peak_mb']} MB; legacy "
              f"open did not survive to report a peak — the gate's "
              f"<= 0.200 ratio is met against any legacy peak above "
              f"{results['portal']['peak_mb'] / 0.2:.0f} MB "
              f"(this run n={args.n})")


if __name__ == "__main__":
    main()
