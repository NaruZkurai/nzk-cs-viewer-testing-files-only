# D5 — one layout pass, one persistent child

The app used to spawn `css-shell` **twice per chrome rebuild**: once with
`--out` for the PNG, once with `--boxes` for the geometry. Measured on this
machine at 1600x1000:

| step | cost |
|---|---|
| process spawn (`--help`, no work) | ~1 ms |
| parse + style + layout (html5ever → Stylo → Taffy) | ~33 ms |
| warm repaint of an unchanged document | ~8-11 ms |
| png encode | ~2 ms |
| **one-shot `--out`** | **~47-51 ms** |
| **one-shot `--boxes`** | **~34-36 ms** |
| **the old pair, together** | **84-90 ms** |

So ~11 ms of that was rendering and the rest was paying for **the same parse
twice, in two processes**. Worse, because the two passes were independent, the
geometry could describe a different layout than the pixels — the drift the D2
guard exists to catch.

## What changed

`css-shell --serve` keeps one child alive and answers **one request with
geometry, the scroll report, the paint and a paint digest from a single layout
pass**, and caches the laid-out document so subsequent frames skip the parse.

Route chosen: **(A) persistent child**, by measurement. Route (B) — linking
Blitz into `app/` — would merge `css-shell`'s Blitz/Stylo/parley/vello universe
with `app/`'s egui/wgpu/winit universe for a ~1 ms spawn saving, against a
≤90 s build budget. The app still builds in ~15 s incremental / ~60 s cold
release and **no dependency merge happened**.

| | before | after |
|---|---|---|
| child invocations per rebuild | 2 | 1 (0 after the first) |
| layout passes per rebuild | 2 | 1 |
| geometry ↔ paint correspondence | two independent passes | provably the same pass (`@frame` digest) |
| steady-state rebuild | 84-90 ms | **~26 ms** end-to-end; 7-11 ms paint + 2 ms encode server-side |

The remaining ~26 ms in the harness measurement is dominated by PNG transport
and the Python driver's decode; the child's own accounting (`stage=reuse`) is
7-11 ms paint + 2 ms PNG encode on a warm frame.

### Latency

Method: `tools/serve-proto.py` drives the real protocol; cold numbers are
`css-shell --out` (fresh process → PNG on disk); WebKit numbers are
`tools/webkit-baseline.py`. Full transcript in `evidence/task-3/latency.txt`.

| measurement | best | method |
|---|---|---|
| old two-invocation pair | 84 ms | `--out` + `--boxes`, 2 fresh processes |
| **new warm frame** | **25-27 ms** | `--serve`, cached layout |
| new cold first frame | 60-205 ms | `--serve` first request (one-time font/parley warm-up) |
| css-shell cold process → PNG | **47 ms** | `--out`, 5 runs |
| WebKitGTK 2.52.6 process → pixels | **576-580 ms** | `tools/webkit-baseline.py` |

**Honest scope note on the WebKit comparison.** WebKitGTK *is* installed and
measurable here, and the script measures it loading the **same**
`index.html` + `static/style.css` at the same 1600x1000, offscreen, then
snapshotting to pixels. That is chrome-content → pixels. It is **not** the full
Tauri/Chromium viewer that `tools/ab.sh` drives (no `/api/*` wiring, no graph
canvas, no Tauri startup). The ~12x figure is for the comparable step only, and
the script prints that caveat itself rather than implying it timed the whole
application.

### Multithreading

Measured, and modest — reported as such:

* `/proc/<pid>/task` peaks at **9 threads** once a frame is in flight (1 before).
  `anyrender_vello_cpu = { features = ["multithreading"] }` is what enables the
  raster worker pool.
* **cpu/wall ≈ 1.26x** across 5 warm frames (170 ms CPU / 134 ms wall, on a
  24-core host). Real parallel work, but only ~a quarter of a core's worth is
  actually parallelised — this is *evidence that the multithreading feature is
  live*, **not** evidence of a scaling win.

## Orphan safety

A long-lived child is a leak waiting to happen. `tools/serve-orphan-proof.sh`
demonstrates, with `/proc` and `kill` only (no X, no synthetic input):

```
1. normal shutdown (QUIT + Drop)      PASS
2. host SIGKILL — no cleanup runs     PASS
3. host SIGSEGV — abnormal exit       PASS
final sweep: zero css-shell --serve   PASS
```

Scenarios 2 and 3 are the load-bearing ones: no `Drop`, no `QUIT`, no signal
handler — the child still dies. It does so because its request loop returns on
**stdin EOF**, and the host holds the only write end of that pipe: when the host
process dies for *any* reason the kernel closes it. The child is also spawned
with `process_group(0)` so it never receives the host terminal's signals.

There is deliberately **no `unsafe` and no C FFI**. A `prctl(PR_SET_PDEATHSIG)`
guard would need FFI, so parent death is detected through the pipe instead.
Known residual: a third process holding the child's stdin open could outlive the
host; nothing in this app does that.

## Correctness checks

* `cargo build --offline --bin native-csviewer` — green, ~15 s incremental.
* `cargo test --offline --bin native-csviewer` — **111 passed, 0 failed**
  (includes 5 new `shell_client` tests: token parsing, `none` → unproven, scroll
  max, a live child answering one pass then exiting cleanly, and a refused
  request leaving the child usable).
* `css-shell --selftest` — **309 checks** (SHA-256 NIST vectors, the 1M-'a'
  vector, every tail length 0..=300, a 6.4 MB frame, and the `@frame` digest
  contract).

### `cargo test` cannot link inside css-shell in this environment

Pre-existing and unrelated to this change: the vendored tree lacks
`hashbrown`/`fontique`/`style`/`stylo_atoms` for the dev profile, so `cargo test`
dies in `parley`/`stylo_taffy` with `E0463` whatever the source says.
`cargo build --lib` and `--bin` both work. That is why the SHA-256 and digest
vectors live in `css-shell --selftest` (run from the built binary) instead of
`#[test]` fns that would never execute here.

## Build-location trap (cost real time; worth knowing)

`rust-env.sh` exports `CARGO_TARGET_DIR=<builder>/target`, **not**
`native-csviewer/css-shell/target`. A plain `cd css-shell && cargo build` writes
`<builder>/target/release/css-shell`, while `find_bin` searches
`native-csviewer/css-shell/target/release/css-shell` **first**. A stale copy there
silently made an early end-to-end run fall back to the one-shot path. To land a
build where the app looks:

```sh
cd "cs viewer builder" && . ./rust-env.sh && cd native-csviewer/css-shell
CARGO_TARGET_DIR="$PWD/target" cargo build --release --offline --bin css-shell
```

## Files

| path | role |
|---|---|
| `css-shell/src/serve.rs` | the protocol: one request → one layout pass, cached |
| `css-shell/src/sha256.rs` | SHA-256 (selftest) + `frame_digest` (FNV-1a-64 wire format) |
| `css-shell/src/main.rs` | `--serve`, `--selftest`, `--no-token` |
| `app/src/app/shell_client.rs` | host side: spawn, request, handshake, shutdown, `Drop` |
| `app/src/chrome.rs` | `build` → `render_via_child`, one-shot kept as fallback |
| `css-shell/tools/serve-proto.py` | drives the protocol; verifies token == SHA-256 of decoded PNG |
| `css-shell/tools/serve-orphan-proof.sh` | the three death scenarios |
| `css-shell/tools/webkit-baseline.py` | the honest WebKitGTK number |

## The `@frame` digest is a cross-module contract

`@frame\t<w>\t<h>\t<16 lowercase hex | none>` is a **FNV-1a-64 with a length
fold**, matching `app/src/chrome.rs::digest_rgba` — not SHA-256:

```text
h = 0xcbf29ce484222325
for each byte b: h ^= b; h = h.wrapping_mul(0x100000001b3)
h ^= len;             h = h.wrapping_mul(0x100000001b3)
format!("{h:016x}")
```

The two sides were first written independently against a verbal spec that said
SHA-256, and the D2 gate correctly **hard-failed** the mismatch end-to-end
before this was reconciled. The digest is over the bytes that get PNG-encoded
(post alpha fixup, row-major, `w*h*4`). Dropping the length fold produces a
different 16-hex value that reads as *real* drift, so both sides now pin it with
vectors. `none` means "no digest available" = UNPROVEN, never a pass.
