# `run-state-tests.sh` — current-state end-to-end test harness

One command that answers: **"is the tree healthy right now?"**

```bash
bash tools/run-state-tests.sh          # run from anywhere
echo $?                                # 0 = no failing gate, 1 = at least one FAIL
```

It is **read-only** with respect to the tree. Scratch logs go to `target/state-tests/`
(never `/tmp` — `/tmp` is not reliably writable in this environment). It never aborts on
a red suite: every gate runs, then the table prints, then the exit code reports the worst
result. A suite that fails to **compile** is reported as `DID NOT COMPILE` — a build
break, explicitly distinguished from a test failure.

---

## What each check means

### 1. app suite — `cargo test --offline --bin native-csviewer`
The main crate's unit tests. Requires `RUSTUP_HOME=/home/naruzkurai/.rustup` (the script
exports it **once, globally, near the top** so both suites inherit it; the toolchain is not
under the default `HOME` here).

- **PASS** = green **and** `N >= 487` (pinned) with `0 failed`.
- **FAIL** = any `failed != 0`, **`N < 487`** (tests removed or skipped), a compile error,
  or no parseable result line.
- **UNPARSED** = the `test result:` line could not be read. This is *not* the same as
  "0 passed" — see *Binary-safe parsing* below.

### 2. nzk_ui suite — `cd nzk_ui && cargo test --offline --lib`
> **`-p nzk_ui` does NOT work.** `nzk_ui/` is its **own workspace** with its own
> `Cargo.lock`. You must `cd` into it. The script does this for you.

- **PASS** = green **and** `N >= 61` (pinned) with `0 failed`.
- **FAIL** = failures, a compile error, or `N < 61`.
- Historical note: the pinned count moved **56 → 61** when task-2 added 5 tests in
  `dom::stylesheet_splice_tests`. That is test-count *growth*, not a regression — which is
  exactly why the gate is `>=` rather than `==`.

### 3. @frame proof — real render, not a mock
```
cargo test --offline --bin native-csviewer -- --nocapture a_closed_picker_keeps_the_default_frame_token
```
`app::chrome::dispatch_tests::a_closed_picker_keeps_the_default_frame_token` spawns a real
`nzk_ui --serve` child and digests the bytes it actually paints, asserting the default
frame is byte-identical: token **`1600 1000 4bd131fa71b2ccc8`**
(`app/src/chrome.rs:3415`, `tsv.contains("@frame\t1600\t1000\t4bd131fa71b2ccc8")`).

> **R27 re-pin (glyph raster parity, 2026-10-18).** The tracked vendor patch
> `tools/patches/blitz-paint-unhinted.patch` (applied by
> `tools/apply-vendor-patches.sh`) renders chrome text unhinted with a
> fractional baseline, which matches the Chromium reference. That moved the
> default-frame token `af24db28dd6cb0c7` → `58009007b0f07c08` and improved the
> real-corpus gate 1.3262 → **1.2122** (sidebar 4.1451 → 3.7077, topbar 5.1309 →
> 4.9883, canvas 0.2385 → 0.2205), deterministic across runs (evidence
> `evidence/v3launch/verify-r27/`). Re-pin procedure unchanged: a REAL
> `nzk_ui --serve` render, twice, with the reason recorded here.

> **R31 shapes re-pin (stroked chevron + checkbox tick, 2026-10-19).** The
> second r31 shape pass (`nzk_ui/src/dom.rs`: `apply_select_chevrons` paints
> the reference's stroked-V chevron as sampled 1-px runs; `apply_form` paints a
> stroked checkbox tick) intentionally moved the token
> `cd9d12c1876cfc3c` → **`58009007b0f07c08`** and the real-corpus gate
> 1.0834 → **1.0806** (sidebar 3.1454 → 3.1322, topbar/canvas unchanged),
> deterministic across runs (evidence `evidence/v3launch/r31-shapes/`).

> **R32 input-font re-pin (topbar path shaping, 2026-10-19).** The tracked vendor
> patch `tools/patches/blitz-dom-input-font.patch` (applied by
> `tools/apply-vendor-patches.sh`) propagates the full computed font style into
> Blitz's text-input editor; previously `<input>` values shaped in Parley's
> default proportional sans, so the topbar repo-path chip fit ~3 characters more
> than the reference. With compat.css §17 (`#rootInput { padding-left: 8px }`)
> the token moved `1ca437c98858977e` → **`1ba3e1672fa2800a`** and the
> real-corpus gate improved 1.0806 → **1.0195** (topbar 4.4178 → 3.6361;
> sidebar 3.1322 → 3.1338, canvas unchanged), deterministic across runs
> (evidence `evidence/v3launch/r32-font/`).

> **R33 shims re-pin (select inset, check line box, canvas-tools widths,
> 2026-10-19).** Three compat.css rules that are no-ops under Chromium but close
> Blitz engine deltas: §18 `#nsSelect` inner-text inset/centring (Blitz has no
> native select appearance: reference ink x1260-1376 y173-182 vs fork
> x1256-1372 y167-177), §19 `.check` line-height 13->14 px, §20
> `#upBtn/#fitBtn/#resettleBtn` advance widths (47/40/81 px; v2's mono advance is
> 1 px narrower on those labels and the flex `gap:5px` carried the drift across
> the row). Token moved `1ba3e1672fa2800a` -> **`58009007b0f07c08`**; real-corpus
> gate 1.0195 -> **0.9768** (sidebar 3.1338 -> 2.9692, canvas 0.2205 -> 0.2101,
> topbar unchanged), deterministic (evidence `evidence/v3launch/r33-ship/`).

> **R34 edge-card row model + R35 spinner/status re-pins (2026-10-18).**
> **R34:** compat.css §21 pins `.edgekey` to a 16px border-box with a 0.25/0.75px
> padding split (Chromium computes 16px, Blitz inherited a fractional 12.51px
> line box -> 17px) plus `#kindChildWrap{margin-top:2.6875px}`; dom.rs pins the
> painted UA checkbox to 13x13 at the rounded origin (layout untouched). Token
> `1ba3e1672fa2800a`-lineage -> **`58009007b0f07c08`**, gate 0.9768 -> **0.9175**.
> **R35:** §14 `#statusLine{margin-left:2px;margin-right:-2px}` (Blitz ignores
> horizontal padding on that flex item; status run was 2px left), §22
> `#depthSelect{margin-left:-2px}` (label-advance delta pushed the box 2px
> right), and the dom.rs depthSelect spinner shim (darker #2b2b2c strip, 9x5
> 1px-row arrows at rows 572-576/583-587, right-aligned value text; Blitz paints
> neither the UA spin buttons nor the number-input value). Token moved
> `58009007b0f07c08` -> **`58009007b0f07c08`**; real-corpus gate 0.9175 ->
> **0.8660** (topbar 3.6361 -> 2.7809, sidebar 2.6912 -> 2.6586, canvas 0.2101
> unchanged), two-run byte-identical (md5 `c2b7ff4bfd1d78f5abf6110bf806d6bf`),
> evidence `evidence/v3launch/final-r35/`, `r35-topbar/`, `r35-spinner/`.

> **R36 (CSS-only sidebar register, 2026-10-19).** §23
> `#resetFilters,#pruneHidden{padding-top:3px}`: Blitz centres the sidebar action
> button label with its own normal line-box metrics, 1.5-2px above the reference
> (label ink rows 636-644 vs ref 638-645), so a 3px top padding shrinks the 28px
> content box and slides the centred label onto the reference rows (band
> y618-660 MAE 4.323 -> 1.644, residual is glyph-rasteriser AA). §21 `.edgekey`
> split re-measured at a fixed 16px border-box: 1.25/0.75 -> 1.375/0.625 (rows
> y440-445 2-3 MAE better each). Token moved `58009007b0f07c08` ->
> **`4bd131fa71b2ccc8`**; real-corpus gate 0.8660 -> **0.8414** (sidebar
> 2.6586 -> 2.5433, topbar 2.7809 and canvas 0.2101 unchanged), two-run
> byte-identical, evidence `evidence/v3launch/final-r36/`.

> **Why the harness checks the source, not stdout.** The token is asserted *inside* the
> test, so it never appears in the captured output even under `--nocapture`. Grepping the
> log for it produced a false FAIL. The gate therefore requires **both** (a) the literal
> is still pinned in `app/src/chrome.rs` and (b) the test ran and passed.

### 4. Invariant greps — **scoped to `app nzk_ui/src`**
Scoping is not cosmetic. The bare repo also matches `target/` and `.cargo-home/`, which
has produced three false "zero matches" claims on this project (e.g. an unqualified
`Outline` grep hits stylo's generated CSS property in `target/`).

| Check | Gate | Meaning |
|---|---|---|
| `include_str!` | **0 in code** | User ruling: no CSS/HTML stored in Rust source. Doc comments *describing* the removal are counted separately and do not fail the gate. |
| stale token `13ab049368cdf7ff` | **absent** | An old frame digest must not survive anywhere in our sources. |
| `unsafe {` / `unsafe fn` / `extern "C"` | **≤ 1** | Exactly ONE `unsafe` C FFI call (the parallelizer) is permitted tree-wide. |
| `todo!` / `unimplemented!` / `FIXME` / `XXX` | **empty** | No unfinished markers in shipping sources. |

Status is captured with `grep -q PATTERN; echo $?`. **Never** `grep ... | head; echo $?` —
that reports `head`'s status, not `grep`'s, and silently inverts the gate.

#### On the `unsafe/FFI` gate — the tree genuinely uses ZERO, and that is correct
The rule reads *"exactly ONE unsafe C FFI call is permitted (the parallelizer)"* — it
**permits up to one**, it does not require one. As of `e5ebbf8c4c7e` the scoped sources
contain **zero** real occurrences of `unsafe {`, `unsafe fn`, or `extern "C"`, which is
**within the rule**, so the gate reports **PASS**, not WARN.

Why zero is provably correct here, not a missing artifact:
- The parallelizer is **pure safe Rust** — `app/src/render.rs:251`
  `std::thread::available_parallelism()`, `:265` `std::thread::scope(|scope| {`, `:268`
  `scope.spawn(move || {`. No FFI, no C, no build-script wrapper.
- Every match for the *word* `unsafe` in scope is a **doc comment** (6 lines) explaining
  the rule — e.g. `app/src/app/shell_client.rs:50` ("deliberately `unsafe`-free and
  FFI-free") and `app/src/picker.rs:837` ("an `unsafe` `geteuid()` FFI call" — explicitly
  the option *not* taken). The harness reports these separately as context.

`*.c` / `*.h` / `build.rs` hits **outside** `app/` and `nzk_ui/src/` are vendored
third-party crates under `.cargo-home/` and `target/` — **not our code**. This is the same
trap class as an unscoped `Outline` grep hitting generated stylo CSS. Grep the scoped
paths, never the bare repo.

The gate only **FAILs at >= 2** occurrences.

---

## Binary-safe log parsing (why `grep -a` is everywhere)

A cargo test log can contain a raw control byte. `grep` then classifies the file as binary
and prints

```
grep: .../nzk_ui-suite.log: binary file matches
```

to **stderr** while emitting **nothing to stdout** — and it still exits `0`. A naive
`$(grep "^test result:" "$log" | tail -1)` therefore yields an **empty string**, the counts
collapse to `0 passed / 0 failed`, and the run reports a bogus `WARN 0 != baseline`.

Reproduced deliberately: appending a NUL to a good log made plain `grep` return
`stdout=[]` with `binary file matches` on stderr, while `grep -a` returned the full
`test result:` line.

Two defences, both required:
1. **Every** log grep uses `-a` (force text mode), so the match always reaches stdout.
2. **`0/0` is never conflated with a real result.** When no result line can be parsed the
   gate gets its own status, **`UNPARSED`**, which is counted separately in the summary and
   makes the harness exit non-zero. Empty counts are never defaulted to zero.

---

## Pinned baseline

Gate rule for both suites: **`passed >= pinned` AND `failed == 0`**. Added tests stay
green; a deleted/skipped test or any failure still fails.

| Suite | Pinned | Command that proves it |
|---|---|---|
| app | **>= 487 passed / 0 failed** | `RUSTUP_HOME=/home/naruzkurai/.rustup cargo test --offline --bin native-csviewer` |
| nzk_ui | **>= 61 passed / 0 failed** | `cd nzk_ui && cargo test --offline --lib` |
| @frame | **`1600 1000 4bd131fa71b2ccc8`** | `cargo test --offline --bin native-csviewer -- --nocapture a_closed_picker_keeps_the_default_frame_token` |

## How to read a failure

1. **Look at the STATUS column, not the count.** `FAIL` and `UNPARSED` make the run exit
   non-zero; `WARN` never does.
2. **`UNPARSED`** — the suite's result line could not be read. It does **not** mean zero
   tests passed. Check the log; a control byte used to cause this and is now handled with
   `grep -a`, so `UNPARSED` means the suite really produced no result line (aborted,
   killed, OOM).
3. **`DID NOT COMPILE`** — a build break, not a test failure. Check the note: if
   `nzk_ui/src` was being edited concurrently, this may be **transient**. Re-run before
   reporting a break.
4. **`BELOW pinned baseline`** — a green run with *fewer* tests than pinned. Something was
   deleted, skipped, or gated off. Investigate; this is not healthy growth.
5. **`above baseline (+N new tests)`** — healthy growth, PASS.
6. **Full logs** are always at `target/state-tests/{app-suite,nzk_ui-suite,frame-proof}.log`.

### Mid-write trees

This repo is often being edited by several teammates at once, so a run can catch a
half-applied change. `git HEAD` and the `dirty:` count are printed in the header for
exactly this reason. A single red run on a dirty tree is a **hint, not a verdict** —
re-run to confirm before calling it a break.
