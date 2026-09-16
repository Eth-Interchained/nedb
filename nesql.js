#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

// nesql — thin platform shim that locates and spawns the prebuilt neSQL CLI
// binary for the current platform/arch.
//
// The binaries ship alongside this file in the npm package root, using the
// same naming convention as nedbd-v2 so the release workflow stages both the
// same way:
//
//   Linux x64    -> nesql-linux-x64
//   Windows x64  -> nesql-win-x64.exe
//   macOS arm64  -> nesql-darwin-arm64
//   macOS x64    -> nesql-darwin-x64
//
// # The exit code is the contract
//
// `nesql` distinguishes 0 success / 1 failure / 2 usage / 3 could-not-determine
// / 4 not found / 5 unsupported, and callers script against those. So this shim
// propagates the child's code EXACTLY and never substitutes one of its own for
// a successful spawn. `npx nesql …` has to be indistinguishable from invoking
// the binary, or the codes stop meaning anything.
//
// A shim failure — unsupported platform, missing binary — exits 5
// (unsupported), which is the CLI's own word for "this build cannot do that".
// Exiting 1 would claim the command ran and failed.

"use strict";

const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

// Keyed platform-arch, with the Linux legs split by libc.
//
// This table promised four platforms while the published package carried a
// binary for NONE of them (v8.0.0 through v9.0.0): the build that arch-named
// these files ran in a job that never uploaded them. The table was not the
// bug, but it was the thing that made the bug speak -- so it now covers every
// binary the release actually builds, rather than a subset chosen by hand.
const SUPPORTED = {
  "linux-x64": "nesql-linux-x64",
  "linux-arm64": "nesql-linux-arm64",
  "linux-x64-musl": "nesql-linux-x64-musl",
  "linux-arm64-musl": "nesql-linux-arm64-musl",
  "win32-x64": "nesql-win-x64.exe",
  "darwin-arm64": "nesql-darwin-arm64",
  "darwin-x64": "nesql-darwin-x64",
};

// musl and glibc binaries are not interchangeable, and the failure when they
// are swapped is a loader error that names neither libc. Node reports the
// runtime glibc version in its process report; on musl the field is simply
// absent. That is the same probe napi-rs uses to pick an addon, so a musl
// Alpine container resolves the musl CLI for the same reason it resolves the
// musl addon.
function libcSuffix() {
  if (process.platform !== "linux") return "";
  try {
    const rep = typeof process.report?.getReport === "function"
      ? process.report.getReport()
      : null;
    if (rep && rep.header && !rep.header.glibcVersionRuntime) return "-musl";
  } catch (err) {
    // Named, not swallowed: if the probe itself breaks we fall through to the
    // glibc name, and the reader needs to know that is a GUESS rather than a
    // detection, because the resulting error will be a confusing loader
    // failure rather than a clean "unsupported platform".
    process.stderr.write(
      `nesql: could not probe libc (${err.message}); assuming glibc. ` +
        `If this is Alpine/musl and the next error is a loader failure, that ` +
        `is why.\n`
    );
  }
  return "";
}

function main() {
  const key = `${process.platform}-${process.arch}${libcSuffix()}`;
  const name = SUPPORTED[key];

  if (!name) {
    process.stderr.write(
      `nesql: unsupported platform/arch: ${key}\n` +
        `Supported: ${Object.keys(SUPPORTED).join(", ")}\n` +
        `Build from source instead:  cargo install nesql\n`
    );
    process.exit(5);
  }

  const binPath = path.join(__dirname, name);
  if (!fs.existsSync(binPath)) {
    // The remedy list used to lead with `npm install --force nedb-engine`,
    // which cannot work and wasted the reader's time: through v9.0.0 no
    // published version contained a nesql binary for ANY platform, so
    // reinstalling fetched the same incomplete tarball. Do not suggest a
    // reinstall as a fix for a packaging gap -- name the gap.
    process.stderr.write(
      `nesql: the prebuilt binary is missing from this package.\n` +
        `  expected: ${binPath}\n` +
        `  platform: ${key}\n\n` +
        `This is a packaging gap, not a corrupt install -- reinstalling will\n` +
        `fetch the same tarball. Working alternatives right now:\n\n` +
        `  pip install nedb-engine && nesql --help   # the wheel ships the CLI\n` +
        `  cargo install nesql                       # builds from source\n\n` +
        `Please report it with the two lines above:\n` +
        `  https://github.com/Eth-Interchained/nedb/issues\n`
    );
    process.exit(5);
  }

  const child = spawn(binPath, process.argv.slice(2), { stdio: "inherit" });

  child.on("error", (err) => {
    // Named rather than swallowed: a spawn failure and a non-zero exit from a
    // binary that DID run are different diagnoses, and collapsing them sends
    // the reader looking in the wrong place.
    process.stderr.write(`nesql: failed to execute ${binPath}: ${err.message}\n`);
    process.exit(5);
  });

  child.on("exit", (code, signal) => {
    if (signal) {
      // Reproduce the signal death rather than translating it to a number,
      // so a Ctrl-C through npx behaves like a Ctrl-C to the binary.
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 5 : code);
  });
}

main();
