# nzk-cs-viewer — testing files only

Optional, development-only companion to
[`nzk-cs-viewer`](https://github.com/NaruZkurai/nzk-cs-viewer). The viewer does
**not** need this repository to build or run; everything here is smoke tests,
fixtures, probes, and evidence used while developing it.

## Use it (optional)

Clone it beside a `nzk-cs-viewer` checkout:

```
parent/
├── nzk-cs-viewer/
└── nzk-cs-viewer-testing-files-only/   <- this repo
```

Then:

```bash
./run-all.sh
```

`run-all.sh` finds the viewer (`${CSVIEWER_REPO:-../nzk-cs-viewer}`), links the
fixture corpus into it for the duration of the run, and executes the viewer's
own test suites plus the engine-free gate. Nothing is written into the viewer
except temporary `tools/` symlinks that are removed on exit.

```bash
CSVIEWER_REPO=/path/to/nzk-cs-viewer ./run-all.sh
FULL=1 ./run-all.sh        # also run the state-test harness
```

## Contents

```
tools/fixtures/            real C# project corpora used by the host tests
tools/state tests, A/B + pixel probes, text/UI probes, corpora
nzk_ui-tools/              DOM/box/proof probes for the nzk_ui rasteriser
reference/frontend-static/ the v2 JS the DOM builders mirror (read-only oracle)
evidence/                  captured proof from rounds (screenshots, JSON, TSV)
```

The A/B scripts under `tools/` (`ab.sh`, `ab-launch.sh`, `ab.py`) capture the
historical Tauri v2 viewer and therefore expect its source tree; they are kept
for provenance and are not part of `run-all.sh`. `run-state-tests.sh` is the
current host state gate.

Everything here is MIT, same as the viewer.
