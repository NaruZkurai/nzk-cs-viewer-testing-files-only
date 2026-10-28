#!/usr/bin/env bash
# run-state-tests.sh — one command that tests the CURRENT state of native-csviewer
# end-to-end and prints a PASS/FAIL summary table.
#
# Runnable from anywhere. Read-only with respect to the tree (scratch stays in
# target/state-tests/). Never aborts on a red suite: every gate runs, then the
# table prints, then the exit code reports the worst result.
#
# See tools/README-state-tests.md for what each check means and how to read a failure.

set -u
set -o pipefail

# ---------------------------------------------------------------- location ----
# Resolve the repo root from this script's own path, so `bash tools/run-state-tests.sh`
# works from any cwd. (There is a space in the parent dir name: quote everything.)
SCRIPT_PATH="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_PATH" ]; do
  _link="$(readlink "$SCRIPT_PATH")"
  case "$_link" in
    /*) SCRIPT_PATH="$_link" ;;
    *)  SCRIPT_PATH="$(dirname "$SCRIPT_PATH")/$_link" ;;
  esac
done
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
WORKDIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$WORKDIR" || exit 2

# Rust toolchain is PROJECT-LOCAL (`builder/{.rustup,.cargo}`, see rust-env.sh).
BUILDER_DIR="$(cd "$WORKDIR/.." && pwd)"
export RUSTUP_HOME="${RUSTUP_HOME:-$BUILDER_DIR/.rustup}"
export CARGO_HOME="${CARGO_HOME:-$BUILDER_DIR/.cargo}"
export PATH="$CARGO_HOME/bin:$PATH"

SCRATCH="$WORKDIR/target/state-tests"
mkdir -p "$SCRATCH" 2>/dev/null || SCRATCH="$WORKDIR/tools/.state-tests"
mkdir -p "$SCRATCH" 2>/dev/null
LOG_APP="$SCRATCH/app-suite.log"
LOG_CSS="$SCRATCH/nzk_ui-suite.log"
LOG_FRAME="$SCRATCH/frame-proof.log"

# Scope for every invariant grep. NEVER grep the bare repo: `target/` and
# `.cargo-home/` produce false matches and false "zero matches" in equal measure.
SCOPE=(app nzk_ui/src)

# Pinned baseline (see tools/README-state-tests.md).
# The gate is ">= pinned AND 0 failed": legitimately ADDED tests are green, while a
# deleted/skipped test or any failure is still caught. (nzk_ui moved 56 -> 61 when
# task-2 landed 5 tests in dom::stylesheet_splice_tests; app was unchanged at 487.)
BASE_APP_PASS=487
BASE_CSS_PASS=61
FRAME_TOKEN="1600 1000 4bd131fa71b2ccc8"
FRAME_TOKEN_SHORT="4bd131fa71b2ccc8"
STALE_TOKEN="13ab049368cdf7ff"

# ------------------------------------------------------------ result store ----
# Parallel arrays: name | status | observed value
R_NAME=(); R_STATUS=(); R_VALUE=(); R_NOTE=()

record() { # record <name> <PASS|FAIL|WARN> <observed> [note]
  R_NAME+=("$1"); R_STATUS+=("$2"); R_VALUE+=("$3"); R_NOTE+=("${4:-}")
}

pad() { # pad <string> <width>
  local s="$1" w="$2"
  printf '%-*s' "$w" "$s"
}

hdr() { printf '\n=== %s ===\n' "$1"; }

# ---------------------------------------------------------------- header ------
HEAD_SHA="$(git -C "$WORKDIR" rev-parse HEAD 2>/dev/null || true)"
HEAD_SHORT="${HEAD_SHA:0:12}"
[ -z "$HEAD_SHORT" ] && HEAD_SHORT="(no git)"
DIRTY="$(git -C "$WORKDIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
HOST="$(hostname 2>/dev/null || echo '?')"

echo "======================================================================"
echo " native-csviewer — CURRENT STATE TEST HARNESS"
echo "======================================================================"
printf ' workdir : %s\n' "$WORKDIR"
printf ' git HEAD: %s\n' "$HEAD_SHORT"
printf ' dirty   : %s path(s) modified vs HEAD (mid-write trees are expected)\n' "$DIRTY"
printf ' started : %s\n' "$(date -Iseconds 2>/dev/null || date)"
printf ' host    : %s\n' "$HOST"
printf ' rustup  : RUSTUP_HOME=%s\n' "$RUSTUP_HOME"
printf ' cargo   : %s\n' "$(cargo --version 2>&1 | head -1)"
printf ' baseline: app >= %s passed / nzk_ui >= %s passed, each with 0 failed\n' \
  "$BASE_APP_PASS" "$BASE_CSS_PASS"
printf ' frame   : token %s\n' "$FRAME_TOKEN"
echo "----------------------------------------------------------------------"
printf ' logs    : %s\n' "$SCRATCH"

# A suite "failed to COMPILE" is a distinct outcome from "a test failed".
# classify_suite <logfile> -> echoes: ok | testfail | compilefail | unparsed
#
# BINARY-SAFE PARSING (the `-a` everywhere below). A cargo test log can contain a
# raw control byte; grep then decides the file is binary and prints
#   `grep: <log>: binary file matches`
# to STDERR while emitting NOTHING to stdout. The exit status is still 0, so a naive
# `$(grep ... | tail -1)` yields an EMPTY string and the counts silently collapse to
# `0 passed / 0 failed` — which then reads as a bogus WARN "0 != baseline".
# `-a`/`--text` forces text mode so the match always reaches stdout.
classify_suite() {
  local log="$1"
  if grep -aq "^error\[E[0-9]" "$log" 2>/dev/null || grep -aq "^error: could not compile" "$log" 2>/dev/null; then
    echo "compilefail"; return
  fi
  if grep -aq "^test result:" "$log" 2>/dev/null; then
    # any failed count > 0 ?
    if grep -a "^test result:" "$log" | grep -qv " 0 failed"; then echo "testfail"; else echo "ok"; fi
    return
  fi
  echo "unparsed"
}

# extract_pass_fail <logfile> -> "passed failed" from the LAST `test result:` line,
# or EMPTY when no result line could be parsed. Callers MUST treat empty as UNPARSED
# rather than defaulting both numbers to 0.
extract_pass_fail() {
  grep -a "^test result:" "$1" 2>/dev/null | tail -1 | \
    sed -n 's/.* \([0-9][0-9]*\) passed; \([0-9][0-9]*\) failed.*/\1 \2/p'
}

# verdict_for_suite <class> <pass> <fail> <baseline> <rc>
# ">= pinned AND 0 failed" — added tests stay green; a deleted/skipped test or any
# failure is still caught. Empty counts are their own UNPARSED verdict.
verdict_for_suite() {
  local class="$1" pass="$2" fail="$3" base="$4" rc="$5"
  case "$class" in
    compilefail) echo "FAIL|DID NOT COMPILE (exit $rc)|build break, not a test failure" ;;
    testfail)    echo "FAIL|$pass passed / $fail failed|real test failure(s)" ;;
    unparsed)    echo "UNPARSED|no parseable 'test result:' line (exit $rc)|log unreadable or suite aborted — NOT a zero-count result" ;;
    ok)
      if [ -z "$pass" ]; then
        echo "UNPARSED|no parseable 'test result:' line (exit $rc)|suite reported but counts unreadable"
      elif [ "$fail" -ne 0 ]; then
        echo "FAIL|$pass passed / $fail failed|real test failure(s)"
      elif [ "$pass" -lt "$base" ]; then
        echo "FAIL|$pass passed / $fail failed|BELOW pinned baseline $base (tests removed/skipped?)"
      elif [ "$pass" -gt "$base" ]; then
        echo "PASS|$pass passed / $fail failed|above baseline $base (+$((pass-base)) new tests) — healthy growth"
      else
        echo "PASS|$pass passed / $fail failed|matches pinned baseline"
      fi
      ;;
    *) echo "FAIL|unknown classification '$class'|internal error" ;;
  esac
}

# --------------------------------------------------------- 1. app suite -------
hdr "1/4  APP SUITE   cargo test --offline --bin native-csviewer"
( export RUSTUP_HOME="$RUSTUP_HOME"; cargo test --offline --bin native-csviewer ) \
  >"$LOG_APP" 2>&1
APP_RC=$?
APP_LINE="$(grep -a "^test result:" "$LOG_APP" 2>/dev/null | tail -1)"
APP_CLASS="$(classify_suite "$LOG_APP")"
read -r APP_PASS APP_FAIL <<<"$(extract_pass_fail "$LOG_APP")"
: "${APP_PASS:=}"; : "${APP_FAIL:=}"
# A nonzero exit with no failed count (e.g. SIGKILL) must not read as 0/0.
[ -z "$APP_PASS" ] && APP_CLASS="unparsed"

echo " exit code : $APP_RC"
echo " result    : ${APP_LINE:-<no parseable 'test result:' line>}"
case "$APP_CLASS" in
  compilefail) echo " NOTE      : DID NOT COMPILE — this is a build break, not a test failure" ;;
  unparsed)    echo " NOTE      : UNPARSED — no result line could be read; this is NOT 0 passed" ;;
esac
IFS='|' read -r APP_VST APP_VAL APP_VNOTE <<<"$(verdict_for_suite "$APP_CLASS" "$APP_PASS" "$APP_FAIL" "$BASE_APP_PASS" "$APP_RC")"
[ "$APP_VST" = "FAIL" ] && [ "$APP_CLASS" = "compilefail" ] && \
  APP_VNOTE="build break — first error: $(grep -a -m1 '^error' "$LOG_APP" | cut -c1-90)"
record "app suite" "$APP_VST" "$APP_VAL" "$APP_VNOTE"

# ----------------------------------------------------- 2. nzk_ui suite -----
hdr "2/4  NZK_UI SUITE   (cd nzk_ui && cargo test --offline --lib)"
# nzk_ui is its OWN workspace: `-p nzk_ui` from the root does NOT work.
# RUSTUP_HOME is exported globally near the top, so this subshell inherits it.
if [ -d "$WORKDIR/nzk_ui" ]; then
  ( cd "$WORKDIR/nzk_ui" && cargo test --offline --lib ) >"$LOG_CSS" 2>&1
  CSS_RC=$?
  CSS_LINE="$(grep -a "^test result:" "$LOG_CSS" 2>/dev/null | tail -1)"
  CSS_CLASS="$(classify_suite "$LOG_CSS")"
  read -r CSS_PASS CSS_FAIL <<<"$(extract_pass_fail "$LOG_CSS")"
  [ -z "$CSS_PASS" ] && CSS_CLASS="unparsed"

  echo " exit code : $CSS_RC"
  echo " result    : ${CSS_LINE:-<no parseable 'test result:' line>}"
  case "$CSS_CLASS" in
    compilefail) echo " NOTE      : DID NOT COMPILE — possible mid-write state of nzk_ui/src" ;;
    unparsed)    echo " NOTE      : UNPARSED — no result line could be read; this is NOT 0 passed" ;;
  esac
  IFS='|' read -r CSS_VST CSS_VAL CSS_VNOTE <<<"$(verdict_for_suite "$CSS_CLASS" "$CSS_PASS" "$CSS_FAIL" "$BASE_CSS_PASS" "$CSS_RC")"
  [ "$CSS_VST" = "FAIL" ] && [ "$CSS_CLASS" = "compilefail" ] && \
    CSS_VNOTE="MAY BE TRANSIENT if nzk_ui/src is mid-write"
  record "nzk_ui suite" "$CSS_VST" "$CSS_VAL" "$CSS_VNOTE"
else
  record "nzk_ui suite" FAIL "nzk_ui/ missing" ""
fi

# ------------------------------------------------- 3. @frame real render ------
hdr "3/4  @FRAME PROOF (real render; spawns nzk_ui --serve, digests painted bytes)"
( export RUSTUP_HOME="$RUSTUP_HOME"; cargo test --offline --bin native-csviewer \
    -- --nocapture a_closed_picker_keeps_the_default_frame_token ) >"$LOG_FRAME" 2>&1
FRAME_RC=$?
FRAME_LINE="$(grep -a "^test result:" "$LOG_FRAME" 2>/dev/null | tail -1)"
FRAME_CLASS="$(classify_suite "$LOG_FRAME")"

echo " exit code : $FRAME_RC"
echo " result    : ${FRAME_LINE:-<no parseable 'test result:' line>}"
# The token is asserted INSIDE the test (`tsv.contains("@frame\t...")` in app/src/chrome.rs),
# so it never reaches stdout even under --nocapture. Verify (a) the source assertion still
# names exactly this token, and (b) the real-render test ran and passed.
if grep -rqF "$FRAME_TOKEN_SHORT" "${SCOPE[@]}" 2>/dev/null && [ "$FRAME_RC" -eq 0 ]; then
  echo " token     : PINNED in source + real-render test passed"
  echo "             $(grep -rnF "$FRAME_TOKEN_SHORT" app/src/chrome.rs 2>/dev/null | head -1 | cut -c1-110)"
  TOKEN_OK=1
else
  TOKEN_OK=0
  echo " token     : NOT verified (test rc=$FRAME_RC)"
fi
if [ "$FRAME_RC" -eq 0 ] && [ "$TOKEN_OK" -eq 1 ]; then
  record "@frame token" PASS "$FRAME_TOKEN" "pinned in source + real-render test passed"
elif [ "$FRAME_CLASS" = "compilefail" ]; then
  record "@frame token" FAIL "DID NOT COMPILE" "build break"
elif [ "$FRAME_RC" -eq 0 ]; then
  record "@frame token" FAIL "test green but token not pinned in source" "literal moved/removed"
else
  record "@frame token" FAIL "test failed (exit $FRAME_RC)" "see $(basename "$LOG_FRAME")"
fi

# --------------------------------------------------- 4. invariant greps -------
hdr "4/4  INVARIANT GREPS (scoped to: ${SCOPE[*]})"

# 4a. include_str! must be 0 (user ruling: no CSS/HTML stored in Rust source).
# Count CODE occurrences only. `include_str!` also appears inside doc comments that
# *describe* the removal — counting those produced a false 4-hit FAIL. Comment lines
# are excluded (`//`, `///`, `//!`); block-comment `*` lines are excluded too.
INC_HITS="$(grep -rn "include_str!" "${SCOPE[@]}" 2>/dev/null | grep -vE ':[0-9]+: *(//|\*|/\*)' || true)"
INC_N=$(printf '%s' "$INC_HITS" | grep -c . || true)
INC_COMMENT_N=$(grep -rn "include_str!" "${SCOPE[@]}" 2>/dev/null | grep -cE ':[0-9]+: *(//|\*|/\*)' || true)
if [ "$INC_N" -eq 0 ]; then
  record "include_str! == 0" PASS "0 in code" \
    "$INC_COMMENT_N doc-comment mention(s) describe the removal (not code)"
else
  record "include_str! == 0" FAIL "$INC_N in code" "$(printf '%s' "$INC_HITS" | head -3 | tr '\n' ' ')"
  printf '%s\n' "$INC_HITS" | head -5 | sed 's/^/    /'
fi

# 4b. stale frame token must be absent.
STALE_N=$(grep -rnF "$STALE_TOKEN" "${SCOPE[@]}" 2>/dev/null | wc -l | tr -d ' ')
if [ "$STALE_N" -eq 0 ]; then
  record "stale token absent" PASS "0" "$STALE_TOKEN absent"
else
  record "stale token absent" FAIL "$STALE_N hit(s)" "$STALE_TOKEN present"
  grep -rnF "$STALE_TOKEN" "${SCOPE[@]}" 2>/dev/null | head -5 | sed 's/^/    /'
fi

# 4c. unsafe / extern "C".
# Use `grep -q`-style status capture (NEVER `grep | head; echo $?` — that reports head's).
grep -rq "unsafe[[:space:]]*{" "${SCOPE[@]}" 2>/dev/null; UNSAFE_BLK_RC=$?
grep -rq "unsafe fn" "${SCOPE[@]}" 2>/dev/null;            UNSAFE_FN_RC=$?
grep -rq 'extern "C"' "${SCOPE[@]}" 2>/dev/null;           EXTERN_RC=$?

echo " unsafe {   : rc=$UNSAFE_BLK_RC $([ $UNSAFE_BLK_RC -eq 0 ] && echo '(found)' || echo '(none)')"
echo " unsafe fn  : rc=$UNSAFE_FN_RC $([ $UNSAFE_FN_RC -eq 0 ] && echo '(found)' || echo '(none)')"
echo " extern \"C\": rc=$EXTERN_RC $([ $EXTERN_RC -eq 0 ] && echo '(found)' || echo '(none)')"
# Report every literal hit with file:line when present.
grep -rn "unsafe[[:space:]]*{\|unsafe fn\|extern \"C\"" "${SCOPE[@]}" 2>/dev/null | sed 's/^/    /'

UNSAFE_TOTAL=$(( (UNSAFE_BLK_RC==0) + (UNSAFE_FN_RC==0) + (EXTERN_RC==0) ))
UNSAFE_N=$(grep -rn "unsafe[[:space:]]*{\|unsafe fn\|extern \"C\"" "${SCOPE[@]}" 2>/dev/null | wc -l | tr -d ' ')
# The rule permits up to ONE unsafe C FFI call (the parallelizer). The tree currently
# uses ZERO, which is WITHIN the rule — permitted, not mandated. Verified: the
# parallelizer is pure safe Rust (app/src/render.rs:251 available_parallelism(),
# :265 std::thread::scope, :268 scope.spawn) — no FFI, no C, no build-script wrapper.
# Only >= 2 occurrences can be a violation.
if [ "$UNSAFE_N" -le 1 ]; then
  record "unsafe/FFI <= 1" PASS "$UNSAFE_N occurrence(s)" \
    "parallelizer is safe Rust (render.rs:251-268 std::thread::scope); rule permits up to 1 unsafe C FFI call, tree uses $UNSAFE_N"
else
  record "unsafe/FFI <= 1" FAIL "$UNSAFE_N occurrence(s)" "rule permits at most 1 — see file:line above"
fi
echo " NOTE: matches for the WORD 'unsafe' that are doc comments (context, not code):"
grep -rn "unsafe" "${SCOPE[@]}" 2>/dev/null | grep -c "^\S*:[0-9]*: *//" | sed 's/^/    doc-comment lines: /'
echo " NOTE: *.c / *.h / build.rs hits OUTSIDE app/ and nzk_ui/src/ are vendored"
echo "       third-party crates under .cargo-home/ and target/ — NOT our code (same"
echo "       trap class as an unscoped 'Outline' grep hitting generated stylo CSS)."

# 4d. todo!/unimplemented!/FIXME/XXX must be empty in sources.
TODO_N=$(grep -rnE "todo!|unimplemented!|FIXME|XXX" "${SCOPE[@]}" 2>/dev/null | wc -l | tr -d ' ')
if [ "$TODO_N" -eq 0 ]; then
  record "no todo/FIXME" PASS "0" "todo!/unimplemented!/FIXME/XXX empty"
else
  record "no todo/FIXME" FAIL "$TODO_N occurrence(s)" ""
  grep -rnE "todo!|unimplemented!|FIXME|XXX" "${SCOPE[@]}" 2>/dev/null | head -5 | sed 's/^/    /'
fi

# ------------------------------------------------------------- summary --------
echo
echo "======================================================================"
echo " SUMMARY"
echo "======================================================================"
printf ' %s %s %s\n' "$(pad 'CHECK' 22)" "$(pad 'STATUS' 7)" "OBSERVED"
echo " ----------------------------------------------------------------------"
FAILS=0; WARNS=0; UNPARSED=0
for i in "${!R_NAME[@]}"; do
  printf ' %s %s %s\n' "$(pad "${R_NAME[$i]}" 22)" "$(pad "${R_STATUS[$i]}" 7)" "${R_VALUE[$i]}"
  if [ -n "${R_NOTE[$i]}" ]; then
    printf ' %s %s\n' "$(pad '' 22)" "└─ ${R_NOTE[$i]}"
  fi
  [ "${R_STATUS[$i]}" = "FAIL" ] && FAILS=$((FAILS+1))
  [ "${R_STATUS[$i]}" = "WARN" ] && WARNS=$((WARNS+1))
  [ "${R_STATUS[$i]}" = "UNPARSED" ] && UNPARSED=$((UNPARSED+1))
done
echo " ----------------------------------------------------------------------"
printf ' gates: %s  FAIL: %s  UNPARSED: %s  WARN: %s\n' "${#R_NAME[@]}" "$FAILS" "$UNPARSED" "$WARNS"
echo " baseline: app $BASE_APP_PASS / nzk_ui $BASE_CSS_PASS passed (gate: >= pinned AND 0 failed)"
echo "           @frame $FRAME_TOKEN"
if [ "$FAILS" -eq 0 ] && [ "$UNPARSED" -eq 0 ]; then
  if [ "$WARNS" -eq 0 ]; then
    echo " RESULT: ALL GREEN"
  else
    echo " RESULT: GREEN ($WARNS warning(s) — read notes above)"
  fi
else
  echo " RESULT: RED ($FAILS failing, $UNPARSED unparsed gate(s))"
  if [ "$UNPARSED" -ne 0 ]; then
    echo " UNPARSED means a suite's 'test result:' line could not be read — it does NOT"
    echo " mean 0 tests passed. Check the log; a binary control byte in the log is the"
    echo " usual cause and is handled with grep -a."
  fi
  echo
  echo " Reminder: if nzk_ui/src was being edited during this run, a nzk_ui"
  echo " compile failure may be TRANSIENT. Re-run to confirm before calling it a break."
fi
echo " logs: $SCRATCH"
echo " finished: $(date -Iseconds 2>/dev/null || date)"
echo "======================================================================"

# ---------------------------------------------------------- exit code ---------
# Non-zero when any gate is FAIL or UNPARSED. WARN does not fail the run.
if [ "$FAILS" -eq 0 ] && [ "$UNPARSED" -eq 0 ]; then exit 0; fi
exit 1
