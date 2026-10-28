#!/usr/bin/env bash
# Optional dev harness: run nzk-cs-viewer's tests plus the engine-free gate.
# The viewer itself does not need this repository.
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
V3="${VIEWER_REPO:-${CSVIEWER_REPO:-$HERE/../nzk-cs-viewer}}"
FULL="${FULL:-0}"

fail=0
say() { printf '\n== %s ==\n' "$*"; }
run() { say "$*"; "$@" || fail=1; }

[ -f "$V3/Cargo.toml" ] || { echo "not a nzk-cs-viewer checkout: $V3" >&2; exit 2; }

# nzk_ui is its own module repository now; fall back to the old in-tree path.
NZK_UI="${NZK_UI_SRC:-$V3/../NZKRepoDeps/CSV Module (nzk-cs-rs_nzk_ui)}"
[ -d "$NZK_UI" ] || NZK_UI="$HERE/../NZKRepoDeps/CSV Module (nzk-cs-rs_nzk_ui)"
[ -d "$NZK_UI" ] || NZK_UI="$V3/nzk_ui"
[ -f "$NZK_UI/Cargo.toml" ] || { echo "nzk_ui module not found (set NZK_UI_SRC)" >&2; exit 2; }

# The host's fixture tests look for ../tools/fixtures; link them in for the run.
LIVE_CORPUS="$(mktemp -d "${TMPDIR:-/tmp}/nzkcs-live.XXXXXX")" || exit 2
cat > "$LIVE_CORPUS/C.cs" <<'CS'
namespace LiveTest {
  public class C {
    public int F() { return 1; }
  }
}
CS
mkdir -p "$V3/tools"
ln -sfn "$HERE/tools/fixtures" "$V3/tools/fixtures"
mkdir -p "$NZK_UI/tests"
ln -sfn "$HERE/nzk_ui-tests/nzk_ui_wire.rs" "$NZK_UI/tests/nzk_ui_wire.rs"
ln -sfn "$HERE/tools/check-engine-free.sh" "$V3/tools/check-engine-free.sh"
ln -sfn "$HERE/tools/run-state-tests.sh" "$V3/tools/run-state-tests.sh"
ln -sfn "$HERE/tools/README-state-tests.md" "$V3/tools/README-state-tests.md"
cleanup() {
  rm -f "$V3/tools/fixtures" "$V3/tools/check-engine-free.sh" \
        "$V3/tools/run-state-tests.sh" "$V3/tools/README-state-tests.md"
  rmdir "$V3/tools" 2>/dev/null || true
  rm -f "$NZK_UI/tests/nzk_ui_wire.rs"
  rmdir "$NZK_UI/tests" 2>/dev/null || true
  rm -rf "$LIVE_CORPUS"
}
trap cleanup EXIT

run bash -lc "cd '$V3' && CSVIEWER_REPO='$LIVE_CORPUS' cargo test --workspace"
run bash -lc "cd '$NZK_UI' && cargo test"
run bash "$V3/tools/check-engine-free.sh"

if [ "$FULL" = 1 ]; then
  run bash "$V3/tools/run-state-tests.sh"
fi

say "summary"
if [ "$fail" = 0 ]; then echo "ALL GREEN"; else echo "FAILURES (see above)"; fi
exit "$fail"
