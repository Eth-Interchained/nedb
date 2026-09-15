# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
# NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

"""Console-script shim for the `nesql` CLI.

`nesql` is a Rust binary. This module exists so `pip install nedb-engine`
provides a working `nesql` command: the platform wheel carries the binary next
to this file, and the console script locates and EXECs it.

# Why exec rather than reimplement

The CLI's contract is its exit code — 0 success, 2 usage, 3 could-not-determine,
4 not found, 5 unsupported — and scripts depend on those. A wrapper that
re-interpreted results would become a second, quieter authority on what the CLI
decided. `os.execv` replaces this process entirely, so the exit code the caller
sees is the one the binary produced, not one this file chose to pass along.

Windows has no execv that preserves the process, so there it falls through to
`subprocess.call` and propagates the code explicitly.

# Resolution order

Deliberately the same order, and the same per-platform naming, that
`nedb.server` uses for `nedbd-v2`. One convention for both binaries means the
release workflow stages them the same way and a reader who understands one
understands the other.

  1. Bundled beside this file  (the platform wheel)
  2. PATH                      (a system or cargo install)
  3. A local cargo release build, so a source checkout works without installing
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

#: What the release workflow stages into this directory, per platform.
#:
#: macOS gets arch-suffixed names because the Mac wheel is FAT — it carries both
#: arm64 and x86_64 binaries, and a bare `nesql` would be one architecture's
#: file presented to both. Linux and Windows wheels are per-platform already,
#: so the unsuffixed name is unambiguous there.
def _staged_names() -> list[str]:
    ext = ".exe" if sys.platform == "win32" else ""
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"
    names: list[str] = []
    if sys.platform == "darwin":
        names.append(f"nesql-darwin-{arch}")
    names += ["nesql" + ext, "nesql_bin" + ext]
    if sys.platform != "win32":
        # A wheel built on Windows and unpacked elsewhere still resolves.
        names.append("nesql.exe")
    return names


def _candidates() -> list[str]:
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    names = _staged_names()
    cargo_names = ["nesql", "nesql.exe"]
    cargo_dirs = [
        os.path.join(cwd, "rust", "target", "release"),
        os.path.join(cwd, "target", "release"),
        os.path.join(os.path.dirname(pkg_dir), "rust", "target", "release"),
    ]
    return (
        [os.path.join(pkg_dir, n) for n in names]
        + [shutil.which(n) or "" for n in names]
        + [os.path.join(d, n) for d in cargo_dirs for n in cargo_names]
    )


def _not_found() -> "int":
    # A missing binary is reported with the CLI's OWN "unsupported" code (5)
    # rather than a generic 1, so a script that switches on exit codes sees
    # "this build cannot do that" instead of "the query failed".
    print("", file=sys.stderr)
    print("  nesql: the CLI binary was not found.", file=sys.stderr)
    print("", file=sys.stderr)
    print("  nesql is a Rust binary that ships alongside this package on", file=sys.stderr)
    print("  supported platforms. This install did not get one.", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  platform: {sys.platform} / {platform.machine()}", file=sys.stderr)
    print(f"  looked for: {', '.join(_staged_names())}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  Fixes:", file=sys.stderr)
    print("    pip install --force-reinstall --no-cache-dir nedb-engine", file=sys.stderr)
    print("    cargo install nesql        # builds from source, any platform", file=sys.stderr)
    print("", file=sys.stderr)
    return 5


def main() -> None:
    binary = next((c for c in _candidates() if c and os.path.isfile(c)), None)
    if binary is None:
        sys.exit(_not_found())
    argv = [binary] + sys.argv[1:]
    if sys.platform == "win32":
        sys.exit(subprocess.call(argv))
    os.execv(binary, argv)


if __name__ == "__main__":
    main()
