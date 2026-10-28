#!/usr/bin/env bash
# =============================================================================
#  ab-css.sh — A/B the CSS-driven renderer (css-shell) against the real original.
#
#  Same corpus, same size, same diff math as native-csviewer/tools/ab.sh, but the
#  fork side is rendered by the css-shell crate (Blitz/Stylo + Taffy + Parley +
#  Vello-CPU, no browser engine, no JS).
#
#  Usage (from anywhere):
#    "cs viewer builder/native-csviewer/css-shell/tools/ab-css.sh"
#    ... --corpus /tmp/fid-corpus --size 1600x1000 --out DIR
#    ... --skip-ref          reuse the reference PNG already in --out
#    ... --bin PATH          css-shell binary (default: css-shell/target/debug/css-shell)
#
#  The reference is the real Tauri/web viewer over Chromium+CDP via
#  ../../rust-only-csviewer/tools/with_serve.sh (private ports, always torn down).
#  The corpus must be a COPY under /tmp: indexing writes .csgraph.v2 into it.
# =============================================================================
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"   # css-shell/tools
CRATE="$(cd -- "$HERE/.." && pwd -P)"                           # css-shell/
FORK="$(cd -- "$CRATE/.." && pwd -P)"                           # native-csviewer/
BUILDER="$(cd -- "$FORK/.." && pwd -P)"                         # "cs viewer builder"
ORIG="$BUILDER/rust-only-csviewer"

CORPUS="/tmp/fid-corpus"
CORPUS_DEFAULT=1
SIZE="1600x1000"
OUT="$FORK/evidence/css-ab"
BIN=""
BIN_REL="$CRATE/target/release/css-shell"
SKIP_REF=0
# Default: capture a FRESH reference in the same run as the fork. Reusing the
# shared harness's PNG is cheaper but loses the status-line text, and the
# original re-measures `parse Nms` every run, so a stale copy silently
# compares two different status strings. Pass --ref-src PATH to reuse one.
REF_SRC=""

while [ $# -gt 0 ]; do
  case "$1" in
    --corpus) CORPUS="$2"; CORPUS_DEFAULT=0; shift 2 ;;
    --size)   SIZE="$2"; shift 2 ;;
    --out)    OUT="$2"; shift 2 ;;
    --bin)    BIN="$2"; shift ;;
    --skip-ref) SKIP_REF=1; shift ;;
    --ref-src) REF_SRC="$2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "ab-css.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

W="${SIZE%x*}"; H="${SIZE#*x}"
mkdir -p "$OUT"
REF="$OUT/reference-${W}x${H}.png"
FRK="$OUT/fork-css-${W}x${H}.png"

if [ ! -d "$CORPUS" ]; then
  if [ "$CORPUS_DEFAULT" = 1 ] && [ -d "$FORK/tools/ab-corpus" ]; then
    echo "ab-css.sh: seeding corpus $CORPUS from native-csviewer/tools/ab-corpus"
    rm -rf "$CORPUS"
    cp -r "$FORK/tools/ab-corpus" "$CORPUS"
  else
    echo "ab-css.sh: corpus $CORPUS does not exist (a /tmp copy of the corpus)" >&2
    exit 2
  fi
fi

STATUS=""
if [ "$SKIP_REF" != 1 ]; then
  if [ -f "$REF_SRC" ] && [ "$SIZE" = "1600x1000" ]; then
    # The shared harness already captured this exact reference; reuse it rather
    # than spawning a second server + Chromium. Re-capture with --ref-src ''.
    echo "ab-css.sh: reusing reference $REF_SRC"
    cp -f "$REF_SRC" "$REF"
  else
    echo "ab-css.sh: capturing reference ($SIZE) from the original on $CORPUS"
    if ! ( cd "$ORIG" && ./tools/with_serve.sh --repo "$CORPUS" --root . --timeout 120 -- \
        node "$FORK/tools/shoot.mjs" --out "$REF" --width "$W" --height "$H" ) \
        > "$OUT/reference-capture.log" 2>&1; then
      echo "ab-css.sh: reference capture failed" >&2
      tail -5 "$OUT/reference-capture.log" >&2
      exit 1
    fi
    # shoot.mjs prints `meta {json}`; its `status` is the exact engine data the
    # original put in the status line. Carry it into the css-shell render so the
    # two frames are compared with the SAME status text — otherwise the volatile
    # `parse Nms` reads as a fidelity difference that is really data drift.
    STATUS=$(python3 - "$OUT/reference-capture.log" <<'PY'
import json, re, sys
log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = re.search(r"^meta (\{.*\})$", log, re.M)
print(json.loads(m.group(1)).get("status", "") if m else "")
PY
)
    echo "ab-css.sh: reference status text: ${STATUS:-<not captured>}"
  fi
fi

# Prefer the release build: the debug build's vello_cpu raster is ~500x slower.
[ -n "$BIN" ] || BIN="$BIN_REL"
[ -x "$BIN" ] || BIN="$CRATE/target/debug/css-shell"
if [ ! -x "$BIN" ]; then
  echo "ab-css.sh: $BIN missing; build it with:" >&2
  echo "  cd /tmp && CARGO_HOME=\"$FORK/.cargo-home\" CARGO_TARGET_DIR=\"$CRATE/target\" \\" >&2
  echo "    cargo build --manifest-path \"$CRATE/Cargo.toml\"" >&2
  exit 2
fi

echo "ab-css.sh: rendering css-shell ($SIZE) for $CORPUS"
( cd "$CRATE" && env -u DISPLAY -u WAYLAND_DISPLAY "$BIN" \
    --out "$FRK" --size "$SIZE" --repo "$CORPUS" --status "$STATUS" ) || {
  echo "ab-css.sh: css-shell render failed" >&2; exit 1; }

echo "ab-css.sh: diffing"
python3 "$FORK/tools/ab.py" --ref "$REF" --fork "$FRK" --out "$OUT"
# Record the pinned inputs so two runs can be proven comparable.
python3 - "$OUT" "$REF" "$FRK" "$STATUS" <<'PY'
import hashlib, json, os, sys
out, ref, frk, status = sys.argv[1:5]
def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
json.dump({"reference_sha256_16": sha(ref), "fork_sha256_16": sha(frk),
           "status_text": status},
          open(os.path.join(out, "inputs.json"), "w"), indent=2)
print("ab-css.sh: inputs.json", json.load(open(os.path.join(out, "inputs.json"))))
PY
echo "ab-css.sh: done"
echo "  reference : $REF"
echo "  fork(css) : $FRK"
echo "  strip     : $OUT/reference-fork-diff.png"
echo "  metrics   : $OUT/metrics.json"
