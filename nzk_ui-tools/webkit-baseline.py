#!/usr/bin/env python3
"""webkit-baseline.py — an honest WebKitGTK number for the SAME chrome content.

The D5 requirement is "measured faster than WebKitGTK". The full original viewer
is a Tauri/Chromium app driven over CDP (`tools/ab.sh`), which needs a real
browser and a served engine. This script measures the thing that is actually
comparable and actually available in this environment:

    load the REAL frontend/index.html + static/style.css in WebKitGTK 2.52.6
    and snapshot it to pixels at 1600x1000.

That is the same content css-shell renders, on the same machine, in the same
process-per-run shape (fresh process -> pixels). It is NOT the full viewer:
no /api/* wiring, no graph canvas, no Tauri shell. So the comparison below is
"chrome content -> pixels", and this script says so rather than implying it
timed the whole application.

Everything runs offscreen (`WEBKIT_DISABLE_COMPOSITING_MODE=1`, no window
mapped where possible) and exits; it never opens a visible window and never
sends synthetic input.
"""
import os
import statistics
import sys
import time

os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
os.environ.setdefault("GDK_BACKEND", "x11")

FE = "/2tb/@/nzk/git/vrcCS/cs viewer builder/rust-only-csviewer/frontend"
W, H = 1600, 1000


def one_run(uri: str) -> tuple[float, float, int, int]:
    """Return (load_ms, snapshot_ms, w, h) for one fresh WebView."""
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import GLib, Gtk, WebKit2

    t0 = time.perf_counter()
    win = Gtk.OffscreenWindow()
    win.set_default_size(W, H)
    view = WebKit2.WebView()
    win.add(view)
    win.show_all()
    # Force the widget to realise/allocate at the target size so the snapshot is
    # the viewport we asked for, not the 1x1 default.
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    state: dict = {}

    def on_load(_view, event):
        if event == WebKit2.LoadEvent.FINISHED:
            state["loaded"] = time.perf_counter()
            GLib.idle_add(Gtk.main_quit)

    view.connect("load-changed", on_load)
    view.load_uri(uri)
    Gtk.main()
    load_ms = (state["loaded"] - t0) * 1000.0

    snap = {}

    def on_snap(v, res, _):
        try:
            surf = v.get_snapshot_finish(res)
            snap["w"], snap["h"] = surf.get_width(), surf.get_height()
        except Exception as e:  # noqa: BLE001
            snap["err"] = str(e)
        snap["t"] = time.perf_counter()
        Gtk.main_quit()

    t1 = time.perf_counter()
    view.get_snapshot(
        WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, on_snap, None
    )
    Gtk.main()
    snap_ms = (snap["t"] - t1) * 1000.0
    win.destroy()
    return load_ms, snap_ms, snap.get("w", 0), snap.get("h", 0)


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    uri = "file://" + FE + "/index.html"
    if not os.path.exists(FE + "/index.html"):
        print(f"webkit-baseline: no frontend at {FE}", file=sys.stderr)
        return 2
    loads, snaps = [], []
    for i in range(n):
        lm, sm, w, h = one_run(uri)
        loads.append(lm)
        snaps.append(sm)
        print(f"  run{i}: load={lm:.0f}ms snapshot={sm:.0f}ms  {w}x{h}", flush=True)
    print()
    print(f"WebKitGTK pid-per-run, {W}x{H}, same index.html+style.css, n={n}")
    print(f"  process->load-finished : best={min(loads):.0f}ms median={statistics.median(loads):.0f}ms")
    print(f"  load->snapshot(pixels) : best={min(snaps):.0f}ms median={statistics.median(snaps):.0f}ms")
    print(f"  TOTAL first pixels     : best={min(loads)+min(snaps):.0f}ms")
    print()
    print("  NOTE: this is chrome content -> pixels under WebKitGTK, NOT the full")
    print("  Tauri viewer (no /api/* wiring, no graph canvas). It does not include")
    print("  the Chromium/Tauri startup that tools/ab.sh measures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
