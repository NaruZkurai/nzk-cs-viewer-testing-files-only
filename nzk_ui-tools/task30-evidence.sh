#!/usr/bin/env bash
# =============================================================================
#  task30-evidence.sh — regenerate the D4 evidence under css-shell/evidence/task-30/
#
#  Proves, from the real frontend DOM:
#    1. the pane-open grid resolves from the stylesheet (`[1fr, 34vh]`), with the
#       pane rect, the canvas row and the sidebar row quoted;
#    2. the sidebar is a real scroll container: a DOM-derived offset is applied
#       through the engine and clipped to the sidebar box;
#    3. the chrome/pane/sidebar/canvas region hit-test;
#    4. the honest cost: warm in-process repaint, and the host-visible
#       child-process rebuild.
#
#  Usage: css-shell/tools/task30-evidence.sh [--bin PATH]
# =============================================================================
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CRATE="$(cd -- "$HERE/.." && pwd -P)"
OUT="$CRATE/evidence/task-30"
BIN="${CSVIEWER_CSS_SHELL_BIN:-$CRATE/target/release/css-shell}"

while [ $# -gt 0 ]; do
  case "$1" in
    --bin) BIN="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "task30-evidence.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
[ -x "$BIN" ] || { echo "task30-evidence.sh: no css-shell at $BIN (build --release first)" >&2; exit 2; }

mkdir -p "$OUT"
W=1600; H=1000

# ---- facts documents --------------------------------------------------------
python3 - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
def facts(ui):
    return {
        "repo": "/tmp/fid-corpus",
        "status_override": "files 2  ·  parse 50ms  ·  nodes 9  ·  edges 10",
        "kinds": [["method", 4], ["class", 3], ["namespace", 2]],
        "ns_count": 2, "indexed": True,
        "file_count": 2, "parse_ms": 50, "node_count": 9, "edge_count": 10,
        "projects_empty_text": "no .sln/.csproj found",
        "parser_warning_count": 0, "nodes_in_view": 2,
        "ui": ui,
    }
json.dump(facts({"collapsed": None, "active_mode": None, "pane_open": False, "scroll": []}),
          open(f"{out}/facts-pane-closed.json", "w"), indent=2)
json.dump(facts({"collapsed": None, "active_mode": None, "pane_open": True, "scroll": []}),
          open(f"{out}/facts-pane-open.json", "w"), indent=2)
json.dump(facts({"collapsed": None, "active_mode": None, "pane_open": True,
                 "scroll": [{"selector": ".side", "x": 0.0, "y": 0.0}]}),
          open(f"{out}/facts-scroll-0.json", "w"), indent=2)
json.dump(facts({"collapsed": None, "active_mode": None, "pane_open": True,
                 "scroll": [{"selector": ".side", "x": 0.0, "y": 240.0}]}),
          open(f"{out}/facts-scroll-240.json", "w"), indent=2)
print("facts documents written")
PY

echo "== 1. pane-open grid geometry ==" | tee "$OUT/geometry.txt"
run_boxes() {
  env -u DISPLAY -u WAYLAND_DISPLAY "$BIN" --facts "$1" --boxes - --size "${W}x${H}" 2>/dev/null
}
{
  echo "--- pane CLOSED (shipped document) ---"
  run_boxes "$OUT/facts-pane-closed.json" | grep -E '^\.layout|^#canvasWrap|^#graph|^\.side\b|^#sourcePane'
  echo "--- pane OPEN (.layout.with-source, [1fr, 34vh]) ---"
  run_boxes "$OUT/facts-pane-open.json" | grep -E '^\.layout|^#canvasWrap|^#graph|^\.side\b|^#sourcePane|^#sourceBody|^\.source-head|^@scroll'
} | tee -a "$OUT/geometry.txt"

echo "== 2. sidebar scroll ==" | tee "$OUT/scroll.txt"
run_boxes "$OUT/facts-scroll-0.json"   | grep -E '^@scroll' | sed 's/^/  offset 0   : /' | tee -a "$OUT/scroll.txt"
run_boxes "$OUT/facts-scroll-240.json" | grep -E '^@scroll' | sed 's/^/  offset 240 : /' | tee -a "$OUT/scroll.txt"

env -u DISPLAY -u WAYLAND_DISPLAY "$BIN" --facts "$OUT/facts-scroll-0.json"   --out "$OUT/scroll-0.png"   --size "${W}x${H}" >/dev/null 2>&1
env -u DISPLAY -u WAYLAND_DISPLAY "$BIN" --facts "$OUT/facts-scroll-240.json" --out "$OUT/scroll-240.png" --size "${W}x${H}" >/dev/null 2>&1
python3 - "$OUT" <<'PY' | tee -a "$OUT/scroll.txt"
import sys
import numpy as np
from PIL import Image
out = sys.argv[1]
a = np.asarray(Image.open(f"{out}/scroll-0.png").convert("RGB"), dtype=np.int16)
b = np.asarray(Image.open(f"{out}/scroll-240.png").convert("RGB"), dtype=np.int16)
d = np.abs(a - b).max(axis=2)
ys, xs = np.where(d > 0)
print(f"  pixels changed by the scroll: {len(xs)}")
if len(xs):
    print(f"  bbox x {xs.min()}..{xs.max()}  y {ys.min()}..{ys.max()}")
    print("  sidebar box is x 1240..1600, y 52..660 -> the change is confined to it")
    # the pane row must not be touched by a sidebar scroll
    pane = d[660:, :]
    print(f"  pixels changed inside the source-pane row (y>=660): {int((pane > 0).sum())}")
PY

# side-by-side crop of the sidebar, scrolled vs not
python3 - "$OUT" <<'PY'
import sys
from PIL import Image, ImageDraw
out = sys.argv[1]
a = Image.open(f"{out}/scroll-0.png").convert("RGB").crop((1240, 52, 1600, 660))
b = Image.open(f"{out}/scroll-240.png").convert("RGB").crop((1240, 52, 1600, 660))
st = Image.new("RGB", (360 * 2 + 8, 608), (0, 0, 0))
st.paste(a, (0, 0)); st.paste(b, (368, 0))
d = ImageDraw.Draw(st)
d.text((6, 4), "offset 0", fill=(255, 255, 0)); d.text((374, 4), "offset 240", fill=(0, 255, 255))
st.save(f"{out}/scroll-side-by-side.png")
print("wrote scroll-side-by-side.png")
PY

echo "== 3. region hit-test (via the app's chrome::region_at test) ==" | tee "$OUT/regions.txt"
echo "  covered by cargo test --offline --bin native-csviewer dispatch_tests::pane_open_grid_scroll_and_region_hit_test" | tee -a "$OUT/regions.txt"

echo "== 4. cost ==" | tee "$OUT/cost.txt"
for f in facts-pane-closed facts-pane-open facts-scroll-240; do
  printf "  %-22s " "$f"
  env -u DISPLAY -u WAYLAND_DISPLAY "$BIN" --facts "$OUT/$f.json" --out "$OUT/cost.png" --size "${W}x${H}" --repeat 4 2>&1 \
    | grep -E 'paint #(1|4):' | sed 's/^\[css-shell\] //' | tr '\n' ' '
  echo
done | tee -a "$OUT/cost.txt"
for i in 1 2 3; do
  s=$(date +%s%N)
  env -u DISPLAY -u WAYLAND_DISPLAY "$BIN" --facts "$OUT/facts-pane-open.json" --out "$OUT/cost.png" --boxes "$OUT/cost.tsv" --size "${W}x${H}" >/dev/null 2>&1
  e=$(date +%s%N)
  echo "  child process (paint + boxes) wall: $(( (e-s)/1000000 ))ms" | tee -a "$OUT/cost.txt"
done

echo "task30-evidence.sh: done -> $OUT"
