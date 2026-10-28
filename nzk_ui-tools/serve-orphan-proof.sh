#!/usr/bin/env bash
# =============================================================================
#  serve-orphan-proof.sh — demonstrate that the persistent css-shell child
#  cannot outlive its host, including when the host is SIGKILLed.
#
#  Three scenarios, each of which must end with ZERO surviving css-shell --serve
#  processes:
#
#    1. NORMAL EXIT   the host calls ShellClient::shutdown()/Drop (QUIT).
#    2. HOST SIGKILL  the host is killed with SIGKILL, so no Drop runs; the
#                     child must still die because its stdin pipe closes.
#    3. HOST SIGSEGV  same, via an abnormal signal, to prove it is the pipe and
#                     not an orderly path doing the work.
#
#  Only /proc and `kill` are used — no X, no synthetic input, nothing that could
#  reach the owner's desktop.
# =============================================================================
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"   # css-shell/tools
CRATE="$(cd -- "$HERE/.." && pwd -P)"                           # css-shell/
BIN="${1:-$CRATE/../../target/release/css-shell}"
TMP="$(mktemp -d /tmp/d5-orphan.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

if [ ! -x "$BIN" ]; then
  echo "serve-orphan-proof: no css-shell at $BIN" >&2
  exit 2
fi
echo "binary: $BIN"

# Every live `css-shell --serve` that is NOT this script. Restricting to
# `--serve` keeps the check about the persistent child specifically.
survivors() {
  local out=""
  for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    local cmd
    cmd="$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)" || continue
    case "$cmd" in
      *css-shell*--serve*) ;;
      *) continue;;
    esac
    # Never count this script or its shell.
    [ "${p#/proc/}" = "$$" ] && continue
    case "$cmd" in
      *serve-orphan-proof*|*d5-host*) continue;;
    esac
    out="$out ${p#/proc/}"
  done
  echo "$out"
}

# Wait up to ~3s for a pid set to empty; returns 0 if it emptied.
wait_gone() {
  local what="$1" i
  for i in $(seq 1 30); do
    local s
    s="$(survivors)"
    if [ -z "${s// /}" ]; then
      echo "  PASS: no surviving css-shell --serve after $what (after ${i} x 100ms)"
      return 0
    fi
    sleep 0.1
  done
  echo "  FAIL: css-shell --serve still alive after $what:$(survivors)" >&2
  return 1
}

fail=0

# A tiny host that spawns the child exactly like the app does: own process
# group, stdin pipe, stderr to /dev/null, then optionally reads the banner.
cat > "$TMP/host.py" <<'PY'
import subprocess, sys, os, time
mode = sys.argv[1]
binp = sys.argv[2]
p = subprocess.Popen([binp, "--serve", "--size", "1600x1000"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, start_new_session=True)
banner = p.stdout.readline()
assert banner.startswith(b"css-shell "), banner
# One real request so the child is definitely past startup.
p.stdin.write(b"REQ 1600x1000 0 1 /tmp/fid-corpus\n")
p.stdin.flush()
hdr = p.stdout.readline().split()
n = int(hdr[3]) + int(hdr[4]) + 2
p.stdout.read(n)
print(f"HOST pid={os.getpid()} child={p.pid} mode={mode}", flush=True)
if mode == "normal":
    p.stdin.write(b"QUIT\n"); p.stdin.flush()
    print(f"HOST rc={p.wait(timeout=10)}", flush=True)
else:
    # Park: the harness will SIGKILL/SIGSEGV THIS process. No cleanup runs.
    print("HOST parked, kill me", flush=True)
    time.sleep(300)
PY

run_case() {
  local mode="$1" sig="$2" label="$3"
  echo "== $label =="
  python3 "$TMP/host.py" "$mode" "$BIN" > "$TMP/out.$mode" 2>&1 &
  host=$!
  # Wait for the child to be up and past a real request.
  for i in $(seq 1 50); do
    grep -q "^HOST " "$TMP/out.$mode" 2>/dev/null && break
    sleep 0.1
  done
  cat "$TMP/out.$mode" | sed 's/^/  /'
  child="$(sed -n 's/.*child=\([0-9]*\).*/\1/p' "$TMP/out.$mode")"
  if [ -z "$child" ]; then echo "  FAIL: host never reported a child" >&2; fail=1; return; fi

  if [ "$mode" = "normal" ]; then
    # The host already sent QUIT and reaped the child itself, so by the time we
    # look the child is legitimately gone. Assert the host's own exit code, then
    # assert the child is not left behind.
    grep -q "HOST rc=0" "$TMP/out.$mode" \
      || { echo "  FAIL: host did not report a clean child exit" >&2; fail=1; return; }
    wait "$host" 2>/dev/null
    wait_gone "$label" || fail=1
  else
    kill -0 "$child" 2>/dev/null \
      || { echo "  FAIL: child $child was not running before the kill" >&2; fail=1; return; }
    echo "  child $child confirmed alive before the kill"
    kill "-$sig" "$host" 2>/dev/null
    wait "$host" 2>/dev/null
    echo "  host killed with SIG$sig (no Drop, no QUIT)"
    # The child must notice its stdin pipe closed. Measured, not assumed.
    wait_gone "$label" || fail=1
  fi
  echo
}

run_case normal   TERM "1. normal shutdown (QUIT + Drop)"
run_case killme   KILL "2. host SIGKILL — no cleanup path runs"
run_case segv     SEGV "3. host SIGSEGV — abnormal termination"

# Final broad sweep: nothing anywhere may be left.
left="$(survivors)"
echo "== final sweep =="
if [ -z "${left// /}" ]; then
  echo "  PASS: zero css-shell --serve processes remain"
else
  echo "  FAIL: leftovers:$left" >&2
  fail=1
fi
exit "$fail"
