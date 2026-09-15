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

const SUPPORTED = {
  "linux-x64": "nesql-linux-x64",
  "win32-x64": "nesql-win-x64.exe",
  "darwin-arm64": "nesql-darwin-arm64",
  "darwin-x64": "nesql-darwin-x64",
};

function main() {
  const key = `${process.platform}-${process.arch}`;
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
    process.stderr.write(
      `nesql: the prebuilt binary is missing for this platform.\n` +
        `  expected: ${binPath}\n` +
        `  platform: ${key}\n\n` +
        `This nedb-engine install did not include it. Fixes:\n` +
        `  npm install --force nedb-engine\n` +
        `  cargo install nesql        # builds from source, any platform\n`
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
