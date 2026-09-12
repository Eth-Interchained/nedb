#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
Does the tag gate stop the tag that shipped the wrong version?

Run:  python3 scripts/test_release_tag_gate.py     (needs GITHUB_TOKEN; read-only)

Asserted against the REAL COMMITS from 2026-09-12 rather than fixtures, because
the bug was never that the logic was wrong in the abstract -- it was about which
commit `refs/heads/master` happened to name one second after a merge. Real shas
are the only fixtures that cannot drift away from the incident:

    d81874b5   declares 4.2.0   <- v4.3.0 was tagged HERE (wrong)
    7b39312d   declares 4.3.0   <- v4.3.1 was tagged HERE (wrong)
    0f105613   declares 4.3.0      the actual 4.3.0 bump merge
    31a0fc1e   declares 4.3.1      the actual 4.3.1 bump merge
    e911e3f9   declares 4.2.0      v4.2.0, which won the coin flip

READ-ONLY BY CONSTRUCTION, and that matters more than it sounds: the first
draft of this file called `tag_flagship` directly, and its "a correct commit
passes" case did exactly what it asked for -- created a real annotated tag,
which fired release.yml and release-distros.yml on a junk version name before
anyone could read the output. Testing a guard by arming the weapon behind it is
not a test. So the guard is a pure function (`verify_tag_target`) and this file
only ever calls that and `declared_version_at`; neither can write.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location("rel", os.path.join(HERE, "release.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    if not os.environ.get("GITHUB_TOKEN"):
        sys.exit("GITHUB_TOKEN not set (run via the nedb-release skill)")
    m = load()
    fails = 0

    def check(name, ok, detail=""):
        nonlocal fails
        if not ok:
            fails += 1
        print(("ok   " if ok else "FAIL ") + name + (("  -- " + detail) if detail else ""))

    # This file must not be able to write. Asserted, not assumed.
    src = open(os.path.abspath(__file__)).read()
    # The needle is assembled rather than written, or the line performing the
    # check is itself a match and the assertion can never pass.
    needle = "tag_flagship" + "("
    check("this test never calls the tagger", needle not in src)

    # ── 1. the reader: what a commit DECLARES, whatever a ref calls it ───────
    for sha, want in (("d81874b5", "4.2.0"), ("7b39312d", "4.3.0"),
                      ("0f105613", "4.3.0"), ("31a0fc1e", "4.3.1"),
                      ("e911e3f9", "4.2.0")):
        got = m.declared_version_at(sha)
        check("%s declares %s" % (sha, want), got == want, "got %r" % (got,))

    check("an unreadable ref is None, not a guess",
          m.declared_version_at("0" * 40) is None)

    # ── 2. the gate ─────────────────────────────────────────────────────────
    for sha, to, vto, label in (("7b39312d", "4.3.1", "v4.3.1", "the v4.3.1 mis-tag"),
                                ("d81874b5", "4.3.0", "v4.3.0", "the v4.3.0 mis-tag")):
        msg = m.verify_tag_target(sha, to, vto)
        check("REFUSES %s" % label, bool(msg) and "REFUSING TO TAG" in msg,
              (msg or "it let the wrong version through").splitlines()[0][:110])

    # ...and does not cry wolf on the commits that are actually right, or the
    # gate gets disabled by the first person it blocks wrongly.
    for sha, to, vto in (("31a0fc1e", "4.3.1", "v4.3.1"), ("0f105613", "4.3.0", "v4.3.0"),
                         ("e911e3f9", "4.2.0", "v4.2.0")):
        msg = m.verify_tag_target(sha, to, vto)
        check("PASSES %s for %s" % (sha, to), msg is None, str(msg)[:110])

    check("no commit at all is refused",
          "no commit to tag" in (m.verify_tag_target("", "4.3.1", "v4.3.1") or ""))
    check("an unreadable commit is refused rather than tagged blind",
          "refusing to tag blind" in (m.verify_tag_target("0" * 40, "4.3.1", "v4.3.1") or ""))

    print("\n%d failure(s)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
