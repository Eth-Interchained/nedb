#!/usr/bin/env bash
# Build nedb-wasm for browsers/node and generate the wasm-bindgen JS glue.
# Output lands in rust/nedb-wasm/pkg/ (gitignored).
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"
cd "$(dirname "$0")"

PROFILE="${1:-dev}"   # dev | release
if [ "$PROFILE" = "release" ]; then
    cargo build -p nedb-wasm --target wasm32-unknown-unknown --release
    WASM=../target/wasm32-unknown-unknown/release/nedb_wasm.wasm
else
    cargo build -p nedb-wasm --target wasm32-unknown-unknown
    WASM=../target/wasm32-unknown-unknown/debug/nedb_wasm.wasm
fi

mkdir -p pkg
wasm-bindgen "$WASM" --target nodejs --out-dir pkg
echo "wrote pkg/nedb_wasm.js + pkg/nedb_wasm_bg.wasm"
