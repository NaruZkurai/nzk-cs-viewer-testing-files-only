#!/usr/bin/env bash
# Optional dev harness: run nzk-cs-viewer's tests plus the engine-free gate.
# The viewer itself does not need this repository.
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
V3="${CSVIEWER_REPO:-$HERE/../nzk-cs-viewer}"
FULL="${FULL:-0}"

fail=0
say() { printf '\n== %s ==\n' "$*"; }
run() { say "$*"; "$@" || fail=1; }

[ -f "$V3/Cargo.toml" ] || { echo "not a nzk-cs-viewer checkout: $V3" >&2; exit 2; }

# The host's fixture tests look for ../tools/fixtures; link them in for the run.
mkdir -p "$V3/tools"
ln -sfn "$HERE/tools/fixtures" "$V3/tools/fixtures"
ln -sfn "$HERE/tools/check-engine-free.sh" "$V3/tools/check-engine-free.sh"
ln -sfn "$HERE/tools/run-state-tests.sh" "$V3/tools/run-state-tests.sh"
ln -sfn "$HERE/tools/README-state-tests.md" "$V3/tools/README-state-tests.md"
cleanup() {
  rm -f "$V3/tools/fixtures" "$V3/tools/check-engine-free.sh" \
        "$V3/tools/run-state-tests.sh" "$V3/tools/README-state-tests.md"
  rmdir "$V3/tools" 2>/dev/null || true
}
trap cleanup EXIT

run bash -lc "cd '$V3' && cargo test --workspace"
run bash -lc "cd '$V3/nzk_ui' && cargo test"
run bash "$V3/tools/check-engine-free.sh"

if [ "$FULL" = 1 ]; then
  run bash "$V3/tools/run-state-tests.sh"
fi

say "summary"
if [ "$fail" = 0 ]; then echo "ALL GREEN"; else echo "FAILURES (see above)"; fi
exit "$fail"
