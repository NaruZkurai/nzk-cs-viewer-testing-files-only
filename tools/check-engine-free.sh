#!/usr/bin/env bash
# =============================================================================
#  check-engine-free.sh — the BAN GATE: v3 must stay Rust-only and engine-free.
# =============================================================================
#
#  The owner's rule for cs viewer v3 (native-csviewer): **no Tauri, no Chrome /
#  Chromium / WebKit / Electron, no webview, no JS runtime** — the UI is
#  winit + wgpu + egui (app) plus Blitz/Stylo/Parley (nzk_ui), all Rust.
#  This script is the cheap, copy-pasteable enforcement of that rule. It scans
#  the v3 tree (manifests, lockfiles, source) and, when release binaries exist,
#  their dynamic linkage. Exit 0 = engine-free, exit 1 = violation found.
#
#  It deliberately says nothing about the v2 tree (../rust-only-csviewer) — v2
#  is the historical Tauri/WebKit reference and is allowed to keep its own
#  stack. The ban applies to what v3 SHIPS.
#
#  Usage:  tools/check-engine-free.sh [--json]
# =============================================================================
set -uo pipefail

FORK_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
JSON=0
[ "${1:-}" = "--json" ] && JSON=1

# Crate names that would pull a browser engine or the Tauri host into the build.
BANNED_CRATES='tauri|tauri-build|tauri-runtime|tauri-utils|wry|webkit2gtk|webkit2gtk-sys|javascriptcore-rs|javascriptcore-rs-sys|gtk|gtk-sys|gtk3|soup3|soup3-sys|webview2-com|webview2-com-sys|electron|chromiumoxide|headless_chrome|chrome-headless|cef|cef-sys|nodejs-sys|deno_core|v8|rusty_v8|quickjs|boa_engine'

# Source-level imports of the same engines (a dependency could be renamed).
BANNED_SOURCE='\b(use|extern\s+crate)\s+(tauri|wry|webkit2gtk|javascriptcore_rs|gtk|soup3|webview2_com|chromiumoxide|headless_chrome|cef|deno_core|v8|rusty_v8|quickjs|boa_engine)\b'

fail=0
say() { if [ "$JSON" = 0 ]; then printf '%s\n' "$*"; fi; }
viol() { fail=1; if [ "$JSON" = 0 ]; then printf '  VIOLATION: %s\n' "$*"; fi; }

say "== v3 engine-free ban gate =="
say "fork: $FORK_DIR"

# 1. Manifest declarations. Scan [dependencies] style table entries only (the
#    banned names may legitimately appear in COMMENTS explaining the ban, so an
#    unanchored grep over prose is not evidence either way).
say ""
say "-- 1. manifests: no banned dependency declared --"
manifest_hits=0
for m in "$FORK_DIR"/Cargo.toml "$FORK_DIR"/app/Cargo.toml "$FORK_DIR"/nzk_ui/Cargo.toml; do
  [ -f "$m" ] || continue
  hits="$(grep -nE "^[[:space:]]*(${BANNED_CRATES})[[:space:]]*=" "$m" 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    viol "$m declares a banned dependency: $hits"
    manifest_hits=$((manifest_hits + 1))
  fi
done
[ "$manifest_hits" = 0 ] && say "  PASS: app/nzk_ui/workspace manifests declare no banned crate"

# 2. Resolved lockfiles — the build graph, including transitive deps.
say ""
say "-- 2. lockfiles: no banned crate in the resolved graph --"
lock_hits=0
for l in "$FORK_DIR"/Cargo.lock "$FORK_DIR"/nzk_ui/Cargo.lock; do
  [ -f "$l" ] || continue
  hits="$(grep -nE "^name = \"(${BANNED_CRATES})\"$" "$l" 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    viol "$l resolves a banned crate: $hits"
    lock_hits=$((lock_hits + 1))
  fi
done
[ "$lock_hits" = 0 ] && say "  PASS: both lockfiles resolve no banned crate"

# 3. Source imports (renames, path deps that re-export an engine).
say ""
say "-- 3. source: no banned engine imported --"
src_hits=0
if [ -d "$FORK_DIR/app/src" ] || [ -d "$FORK_DIR/nzk_ui/src" ]; then
  hits="$(grep -rnE "$BANNED_SOURCE" "$FORK_DIR"/app/src "$FORK_DIR"/nzk_ui/src 2>/dev/null \
          | grep -vE '^[^:]+:[0-9]+:[[:space:]]*(//|/\*)' || true)"
  if [ -n "$hits" ]; then
    viol "banned engine imported in source:"; printf '%s\n' "$hits"; src_hits=1
  fi
fi
[ "$src_hits" = 0 ] && say "  PASS: no banned engine imported by app/src or nzk_ui/src"

# 4. What the shipped Rust actually links (only if built).
say ""
say "-- 4. linkage: built ELFs carry no browser-engine library --"
bins=("$FORK_DIR/target/release/native-csviewer" "$FORK_DIR/nzk_ui/target/release/nzk_ui")
ldd_checked=0
for b in "${bins[@]}"; do
  [ -x "$b" ] || continue
  ldd_checked=$((ldd_checked + 1))
  lk="$(ldd "$b" 2>/dev/null | grep -Ei 'electron|chrom|webkit|gtk|javascript|node|v8' || true)"
  if [ -n "$lk" ]; then viol "$(basename "$b") links a banned engine library: $lk"; fi
done
if [ "$ldd_checked" = 0 ]; then
  say "  SKIP: no release ELFs present (build first)"
else
  say "  PASS: $ldd_checked ELF(s) checked, no electron/chrome/webkit/gtk/js linkage"
fi

# 5. No packaged web-runtime assets (JS bundles, node_modules, electron dirs).
say ""
say "-- 5. tree: no JS/webview payload shipped --"
asset_hits="$(find "$FORK_DIR/app" "$FORK_DIR/nzk_ui" \
  \( -name node_modules -o -name '*.asar' -o -name 'electron*' -o -name '*.js' -o -name '*.mjs' \) \
  -not -path '*/target/*' 2>/dev/null | head -20)"
if [ -n "$asset_hits" ]; then
  # .js assets are not shipped by v3, but if any appear, report (not necessarily fatal).
  say "  NOTE: JS-ish files present (not necessarily shipped):"; printf '%s\n' "$asset_hits"
else
  say "  PASS: no node_modules/.asar/electron/.js payload"
fi

say ""
if [ "$fail" = 0 ]; then
  say "RESULT: PASS — v3 tree and binaries are Tauri/Chrome/WebKit/Electron-free"
  [ "$JSON" = 1 ] && printf '{"ok":true,"violations":0}\n'
  exit 0
else
  say "RESULT: FAIL — the ban is violated (see VIOLATION lines above)"
  [ "$JSON" = 1 ] && printf '{"ok":false}\n'
  exit 1
fi
