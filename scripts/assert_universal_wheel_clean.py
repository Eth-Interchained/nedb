#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""
A `py3-none-any` wheel must not contain a platform-specific binary.

    python3 scripts/assert_universal_wheel_clean.py [dist]

# Why this exists

The universal wheel used to carry a native extension, and the intent was
reasonable: let `from nedb._native import NedbCore` work without the platform
wheel installed. It cannot work, for one reason — maturin names an abi3
extension `nedb/_native.abi3.so` for EVERY target. x86_64-glibc, x86_64-musl,
aarch64-glibc, aarch64-musl: one filename, five architectures.

The release workflow downloaded all five with `merge-multiple: true`, which
collapsed them onto a single path. One won, nondeterministically, and got
packaged into a wheel that claims to run everywhere.

It shipped:

    nedb_engine-4.3.2-py3-none-any.whl
      └─ nedb/_native.abi3.so : ELF 64-bit LSB shared object, ARM aarch64

An ARM binary inside the wheel pip hands to every platform on earth.

# Why a GATE rather than a fix alone

Because the failure was SILENT, and silence is what let it survive releases.
An aarch64 .so on x86_64 fails to `dlopen` cleanly, the lazy import falls back
to pure Python, the smoke test passes, and the broken wheel publishes with a
green check. Only a musl-x86_64 .so — right architecture, right ELF class —
gets far enough to load and then die on glibc symbols, which is the SIGSEGV
that finally exposed it.

A fix that removes the staging is one careless re-add away from returning, and
the re-add would look green. So the built artifact is opened and inspected.

# What is allowed

The `nedbd-v2*` SERVER binaries are fine and stay. They are staged under
distinct per-platform names and coexist correctly, which is the contrast worth
keeping in mind: the idea was never wrong, the shared filename was.
"""
import glob
import os
import sys
import zipfile

# Anything that is an importable extension module for ONE platform. The server
# binaries are deliberately excluded -- they are data files with distinct names,
# not things Python will try to dlopen.
EXT_MARKERS = (".so", ".pyd", ".dylib")


def offending(names):
    """Entries that make a wheel platform-specific, with the reason."""
    out = []
    for n in names:
        base = os.path.basename(n)
        if base.startswith("nedbd-v2"):
            continue  # a per-platform server binary, named as such, on purpose
        if "_native" in base:
            out.append((n, "a native extension module"))
        elif base.endswith(EXT_MARKERS):
            out.append((n, f"a compiled shared object ({base})"))
    return out


def main(argv):
    dist = argv[1] if len(argv) > 1 else "dist"
    wheels = sorted(glob.glob(os.path.join(dist, "*.whl")))
    if not wheels:
        # Not "nothing to check, therefore fine". A release step that silently
        # passes when its input is missing is how a gate stops being one.
        print(f"!! no wheels found in {dist!r} — nothing was built, or the path is wrong")
        return 1

    bad = []
    checked = 0
    for whl in wheels:
        name = os.path.basename(whl)
        if "-py3-none-any" not in name:
            print(f"skip  {name}  (a platform wheel — a binary belongs in it)")
            continue
        checked += 1
        with zipfile.ZipFile(whl) as z:
            names = z.namelist()
            hits = offending(names)
        print(f"check {name}  ({len(names)} entries)")
        for n, why in hits:
            print(f"   !! {n} — {why}")
            bad.append((name, n, why))
        if not hits:
            print("   ok — nothing platform-specific")

    if checked == 0:
        print("!! no py3-none-any wheel was built, so the universal wheel is missing")
        return 1

    if bad:
        print(
            "\n!! A platform-specific binary is inside a wheel tagged py3-none-any.\n"
            "   That tag promises every platform, so one architecture's binary is a\n"
            "   lie told to all the others — and it is the exact defect that shipped\n"
            "   an aarch64 .so to every nedb-engine user in 4.3.2.\n"
            "   Platform binaries belong in the platform wheels, which the `wheels`\n"
            "   matrix already publishes."
        )
        return 1

    print(f"\nok — {checked} universal wheel(s) carry no platform-specific binary")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
