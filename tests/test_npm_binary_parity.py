#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""Every binary a JS shim promises must be built by some pipeline.

# The defect this exists to prevent

`nesql.js` shipped in `nedb-engine` from v8.0.0 through v9.0.0 declaring a
four-platform `SUPPORTED` table. The npm tarball contained a binary for NONE of
them. Every CI run was green, every release published, and the first report came
from a human typing `nesql --help` on his own machine four releases later.

Nothing was wrong with the shim or with the build. The binary was built in the
`wheels` job, which arch-named it for npm and then ended -- and nothing in that
job uploads an artifact or a release asset, so the file died with the runner.
The two ends were individually correct and had never been checked against each
other.

That is the shape this file guards: not "does the build work" but "does the
thing the package PROMISES correspond to something the build PRODUCES". A test
that reads only one side cannot see it, which is why both sides are parsed here
from their real sources rather than from a list maintained by hand.

# Why static parsing rather than inspecting a tarball

A published tarball can only be checked after publishing, which is one release
too late -- exactly how this ran for four. Reading the workflow definitions
fails the PR instead.

Run: python3 tests/test_npm_binary_parity.py
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_fail = 0


def check(name: str, cond: bool, detail: str = "", why: str = "") -> None:
    """`detail` prints either way; `why` explains a FAILURE and prints only then.

    Kept separate because the first version of this file printed the failure
    rationale beside every PASSING line, so a fully green run read as though
    every check had failed. A test whose output cannot be skimmed does not get
    skimmed, and an unskimmable green is how a real failure hides.
    """
    global _fail
    if cond:
        print("  ok    %s%s" % (name, ("  — " + detail) if detail else ""))
    else:
        _fail += 1
        print("  FAIL  %s  — %s" % (name, why or detail))


def read(*parts: str) -> str:
    return open(os.path.join(ROOT, *parts), encoding="utf8").read()


def shim_promises(js: str) -> dict[str, str]:
    """The `SUPPORTED` map out of a shim: platform-key -> binary filename."""
    m = re.search(r"const SUPPORTED\s*=\s*\{(.*?)\};", js, re.S)
    if not m:
        return {}
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', m.group(1)))


def main() -> int:
    print("\n── what nesql.js promises ──")
    promised = shim_promises(read("nesql.js"))
    check("nesql.js declares a SUPPORTED table", bool(promised),
          "%d platforms" % len(promised))
    for k, v in sorted(promised.items()):
        print("        %-18s -> %s" % (k, v))

    print("\n── what the pipelines actually build ──")
    # GitHub Actions: the DEST names in the node-binaries nesql step.
    rel = read(".github", "workflows", "release.yml")
    gha = set(re.findall(r'DEST="(nesql-[^"]+)"', rel))
    # Codemagic: the Mac halves, named at the copy site.
    cm = set(re.findall(r"cp \"\$NESQL_SRC\" (nesql-darwin-\S+)", read("codemagic.yaml")))
    built = gha | cm
    check("release.yml builds nesql binaries", bool(gha), "%s" % sorted(gha))
    check("codemagic.yaml builds the Mac halves", bool(cm), "%s" % sorted(cm))

    print("\n── every promise is kept by some pipeline ──")
    for key, binary in sorted(promised.items()):
        check("%s (%s) is built" % (binary, key), binary in built,
              why="promised by nesql.js but produced by NO pipeline — this is "
                  "the v8.0.0-v9.0.0 defect: the shim would exit 5 here")

    print("\n── nothing is built that the shim cannot resolve ──")
    # The weaker direction, but it catches the reverse drift: a pipeline
    # renaming its output while the shim keeps looking for the old name.
    for binary in sorted(built):
        check("%s is resolvable by the shim" % binary, binary in promised.values(),
              why="built and uploaded, but nesql.js has no SUPPORTED entry "
                  "naming it, so no platform will ever load it")

    print("\n── the binary must be inside the npm `files` allowlist ──")
    pkg = json.loads(read("package.json"))
    files = pkg.get("files") or []
    # A glob covering the arch-named binaries has to be present, or npm packs
    # the shim and drops every binary beside it.
    covered = any(f in ("nesql-*", "nesql*") for f in files)
    check("package.json files[] carries a nesql-* glob", covered,
          why="files=%s — without a nesql-* glob npm packs the shim and drops "
              "every binary beside it" % files)
    check("nesql.js itself is packed", "nesql.js" in files)

    print("\n── the npm wait must gate on the nesql binaries, not the addons ──")
    # npm 10.30.90 shipped nesql-darwin-arm64 and NO nesql-darwin-x64. The
    # wait loop gated on any `darwin-x64` asset, which `nedb.darwin-x64.node`
    # satisfies -- and once the nesql stage moved to the END of Codemagic's
    # script, the addon started landing FIRST. The gate passed at 00:16:19,
    # npm published at 00:16:25, and nesql-darwin-x64 arrived at 00:16:41.
    #
    # A 16-second race is not something a human re-reads the workflow and
    # spots, so it is asserted: the wait must name the nesql assets exactly.
    wait_ok = ('"nesql-darwin-arm64"' in rel) and ('"nesql-darwin-x64"' in rel)
    check("the Mac wait names both nesql binaries exactly", wait_ok,
          why="the wait loop must grep for '\"nesql-darwin-arm64\"' and "
              "'\"nesql-darwin-x64\"'. Gating on a looser darwin pattern "
              "matches the .node addons, which now upload BEFORE nesql -- "
              "that is the v10.30.90 race that shipped npm without the "
              "Intel Mac CLI")

    print("\n── no build log may ship inside the npm package ──")
    # The diagnostic log added for the Mac failures was named
    # `nesql-build-<arch>.log`, which `files: ["nesql-*"]` happily packed --
    # so npm 10.30.90 carried 1.3KB of CI build output. Harmless but sloppy,
    # and the fix (rename outside the glob) is the kind of thing that silently
    # regresses the next time someone names a log.
    cm = read("codemagic.yaml")
    # Only files the pipeline actually WRITES or uploads -- anchored on the
    # redirection and on `gh release upload`. A bare `\S+\.log` also matched
    # `console.log` from unrelated JS, which passed but made the output lie
    # about what it was checking.
    logs = set(re.findall(r'>\s*([A-Za-z0-9_.-]+\.log)', cm))
    logs |= set(re.findall(r'gh release upload "\$TAG" ([A-Za-z0-9_.-]+\.log)', cm))
    for lg in sorted(logs):
        packed = any(
            lg.startswith(f[:-1]) if f.endswith("*") else lg == f
            for f in files
        )
        check("%s stays out of the npm tarball" % lg, not packed,
              why="it matches an entry in package.json files[] (%s), so npm "
                  "packs CI build output into a public package" % files)

    print("\n── the shim must not promise a reinstall as the remedy ──")
    # `npm install --force` cannot conjure a binary no version ever shipped,
    # and suggesting it sent the first reporter down a dead end.
    # Comments are stripped first. The first version of this check matched the
    # comment that DOCUMENTS the old remedy, so it failed on the very commit
    # that removed it -- a test that cannot tell code from commentary.
    code = "\n".join(
        l for l in read("nesql.js").splitlines() if not l.lstrip().startswith("//")
    )
    check("no `npm install --force` remedy in the error text",
          "npm install --force" not in code,
          why="a packaging gap is not a corrupt install; naming a reinstall as "
              "the fix sends the reporter down a dead end")

    print("\n" + "=" * 64)
    print("npm binary parity: %s" % ("ALL PASSED" if not _fail else "%d FAILED" % _fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
