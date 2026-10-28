#!/usr/bin/env python3
"""parity-audit.py — honest, category-split audit of a reference-vs-fork UI frame.

The plain MAE gate cannot distinguish "layout/colour still wrong" from "the same
glyphs rasterised by a different anti-aliasing engine", which is the difference
between a real UI defect and an inherent Blitz/Vello/egui-vs-Chromium artefact.
This tool reports both:

  * RAW        whole-frame and per-region mae / gt16 / identical% (the gate metric)
  * THUMBNAIL  4x4 box-downsampled diff (AA and glyph edges vanish; layout,
               colour and any structural difference of >2px survives)
  * FLAT       exact-match rate over locally-uniform pixels (3x3 std < 2 on both
               sides): backgrounds, fills, borders, panels — should be ~100%
  * TEXT       residual split into "reference glyph ink" vs "everything else"
               using the v2 textprobe text-removed frame (chrome only), with a
               2 px dilation so a 1-2 px glyph placement delta is not called
               structural
  * CLUSTERS   the remaining non-text diff clusters >= --min-px, bbox + crops

Usage:
  tools/parity-audit.py --ref REF.png --fork FORK.png --out DIR \
      [--notext NOTE_TEXT_REMOVED.png] [--textprobe textprobe.json]

--notext is the original viewer's same frame with text ink hidden (textprobe.mjs
writes <out>.notext.png). It is only reliable for the chrome: the reference
textprobe capture may have different canvas node positions, so canvas pixels are
never classified as text-ink from it.
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

REGIONS = [
    ("whole", 0, 0, 1600, 1000),
    ("topbar", 0, 0, 1600, 52),
    ("toolbar", 0, 52, 1240, 110),
    ("canvas", 0, 52, 1240, 1000),
    ("sidebar", 1240, 52, 1600, 1000),
]
BG = np.array([30, 30, 30], dtype=np.int16)


def load_rgb(path):
    im = Image.open(path)
    a = np.asarray(im, dtype=np.float64)
    if a.ndim == 2:
        a = np.stack([a] * 3, axis=-1)
    if a.shape[2] == 4:
        a = a[:, :, :3] * (a[:, :, 3:4] / 255.0)
    return np.asarray(a, dtype=np.int16)


def stats(d):
    dm = d.max(axis=2)
    return {
        "mae": round(float(d.mean()), 4),
        "gt16": int((dm > 16).sum()),
        "gt16_pct": round(float((dm > 16).mean() * 100), 4),
        "identical_pct": round(float((dm == 0).mean() * 100), 4),
    }


def label_components(mask):
    h, w = mask.shape
    parent = []

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    rows, prev = [], []
    for y in range(h):
        xs = np.flatnonzero(mask[y])
        runs = []
        if xs.size:
            starts = xs[np.r_[True, np.diff(xs) > 1]]
            ends = xs[np.r_[np.diff(xs) > 1, True]]
            for x0, x1 in zip(starts.tolist(), ends.tolist()):
                lab = len(parent)
                parent.append(lab)
                runs.append((x0, x1, lab))
                for px0, px1, plab in prev:
                    if px0 <= x1 and x0 <= px1:
                        union(plab, lab)
        rows.append(runs)
        prev = runs
    out = np.zeros((h, w), dtype=np.int32)
    roots = [find(i) for i in range(len(parent))]
    for y, runs in enumerate(rows):
        for x0, x1, lab in runs:
            out[y, x0:x1 + 1] = roots[lab] + 1
    return out


def dilate(mask, r=2):
    out = mask.copy()
    h, w = mask.shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            src = mask[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)]
            dst = out[max(0, -dy):h + min(0, -dy), max(0, -dx):w + min(0, -dx)]
            dst |= src
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--fork", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--notext")
    ap.add_argument("--textprobe")
    ap.add_argument("--thr", type=int, default=16)
    ap.add_argument("--min-px", type=int, default=40)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    ref = load_rgb(args.ref)
    fork = load_rgb(args.fork)
    if ref.shape != fork.shape:
        sys.exit(f"size mismatch {ref.shape} vs {fork.shape}")
    h, w = ref.shape[:2]
    d = np.abs(ref.astype(np.int32) - fork.astype(np.int32)).astype(np.int16)
    dm = d.max(axis=2)
    raw = stats(d)

    # ---- regions ----------------------------------------------------------
    regions = {}
    for name, x0, y0, x1, y1 in REGIONS:
        x1, y1 = min(x1, w), min(y1, h)
        regions[name] = stats(d[y0:y1, x0:x1])

    # ---- thumbnail (4x4 box average; AA/glyph edges average out) ----------
    hh, ww = (h // 4) * 4, (w // 4) * 4
    ra = ref[:hh, :ww].astype(np.float64).reshape(hh // 4, 4, ww // 4, 4, 3).mean(axis=(1, 3))
    fa = fork[:hh, :ww].astype(np.float64).reshape(hh // 4, 4, ww // 4, 4, 3).mean(axis=(1, 3))
    td = np.abs(ra - fa)
    thumbnail = {
        "mae": round(float(td.mean()), 4),
        "gt8": int((td.max(axis=2) > 8).sum()),
        "gt8_pct": round(float((td.max(axis=2) > 8).mean() * 100), 4),
        "pixels": int(td.shape[0] * td.shape[1]),
    }

    # ---- flat-region exact parity -----------------------------------------
    def local_std(a):
        g = a.mean(axis=2)
        p = np.pad(g, ((1, 1), (1, 1)), mode="reflect")
        acc = np.zeros_like(g)
        acc2 = np.zeros_like(g)
        for dy in range(3):
            for dx in range(3):
                v = p[dy:dy + h, dx:dx + w]
                acc += v
                acc2 += v * v
        m = acc / 9.0
        return np.sqrt(np.maximum(acc2 / 9.0 - m * m, 0))
    flat = (local_std(ref) < 2) & (local_std(fork) < 2)
    flat_px = int(flat.sum())
    flat_bad = int(((dm > 0) & flat).sum())
    flat_stats = {
        "pixels": flat_px,
        "pct_of_frame": round(flat_px / (w * h) * 100, 2),
        "exact_match_pct": round(100.0 * (1 - flat_bad / max(flat_px, 1)), 4),
        "bad_px": flat_bad,
        "bad_px_gt16": int(((dm > 16) & flat).sum()),
    }

    # ---- text-ink split (chrome only; canvas text never from notext) ------
    text_mask = np.zeros((h, w), dtype=bool)
    text_split_ok = False
    if args.notext and os.path.exists(args.notext):
        note = load_rgb(args.notext)
        if note.shape == ref.shape:
            changed = np.abs(ref.astype(np.int32) - note.astype(np.int32)).max(axis=2) > 8
            # canvas text masked out is unreliable across captures; keep chrome
            changed[52:, :1240] = False
            text_mask = dilate(changed, 2)
            text_split_ok = True
    text_stats = None
    if text_split_ok:
        tm = int(text_mask.sum())
        dm_in = dm[text_mask]
        dm_out = dm[~text_mask]
        text_stats = {
            "text_ink_px": tm,
            "text_ink_pct_of_frame": round(tm / (w * h) * 100, 3),
            "residual_on_text_ink": {
                "px": tm,
                "mae": round(float(dm_in.mean()), 3),
                "gt16": int((dm_in > args.thr).sum()),
                "gt16_pct": round(float((dm_in > args.thr).mean() * 100), 3),
            },
            "residual_off_text_ink": {
                "px": int((~text_mask).sum()),
                "mae": round(float(dm_out.mean()), 3),
                "gt16": int((dm_out > args.thr).sum()),
                "gt16_pct": round(float((dm_out > args.thr).mean() * 100), 4),
            },
            "share_of_gt16_on_text_ink": round(
                float((dm_in > args.thr).sum()) / max(int((dm > args.thr).sum()), 1) * 100, 2),
        }

    # ---- remaining non-text clusters --------------------------------------
    mask = dm > args.thr
    if text_split_ok:
        mask &= ~text_mask
    labels = label_components(mask)
    n = int(labels.max())
    clusters = []
    if n:
        flat_l = labels.ravel()
        idx = np.argsort(flat_l, kind="stable")
        flat_s = flat_l[idx]
        bounds = np.searchsorted(flat_s, np.arange(1, n + 2))
        ys, xs = np.divmod(idx, w)
        for lab in range(1, n + 1):
            s, e = bounds[lab - 1], bounds[lab]
            if e - s < args.min_px:
                continue
            cx, cy = xs[s:e], ys[s:e]
            x0, x1 = int(cx.min()), int(cx.max()) + 1
            y0, y1 = int(cy.min()), int(cy.max()) + 1
            b = d[y0:y1, x0:x1]
            clusters.append({
                "bbox": [x0, y0, x1, y1],
                "px": int(e - s),
                "gt16": int((b.max(axis=2) > args.thr).sum()),
                "mean": round(float(b.mean()), 2),
                "max": int(b.max()),
            })
    clusters.sort(key=lambda c: -c["px"])
    for i, c in enumerate(clusters[: args.top]):
        x0, y0, x1, y1 = c["bbox"]
        pad = 6
        cx0, cy0 = max(0, x0 - pad), max(0, y0 - pad)
        cx1, cy1 = min(w, x1 + pad), min(h, y1 + pad)
        sc = max(1, min(8, 360 // max(1, cx1 - cx0)))
        Image.fromarray(ref[cy0:cy1, cx0:cx1].astype(np.uint8)).resize(
            ((cx1 - cx0) * sc, (cy1 - cy0) * sc), Image.NEAREST
        ).save(os.path.join(args.out, f"rem{i:02d}-ref.png"))
        Image.fromarray(fork[cy0:cy1, cx0:cx1].astype(np.uint8)).resize(
            ((cx1 - cx0) * sc, (cy1 - cy0) * sc), Image.NEAREST
        ).save(os.path.join(args.out, f"rem{i:02d}-fork.png"))
        tint = np.zeros((cy1 - cy0, cx1 - cx0, 3), dtype=np.uint8)
        dd = dm[cy0:cy1, cx0:cx1]
        tint[:, :, 0] = np.clip(dd * 4, 0, 255)
        tint[:, :, 2] = np.clip(dd, 0, 255)
        Image.fromarray(tint).resize(((cx1 - cx0) * sc, (cy1 - cy0) * sc), Image.NEAREST).save(
            os.path.join(args.out, f"rem{i:02d}-diff.png"))

    summary = {
        "ref": args.ref,
        "fork": args.fork,
        "size": [w, h],
        "raw": raw,
        "regions": regions,
        "thumbnail_4x4": thumbnail,
        "flat_regions": flat_stats,
        "text_split": text_stats,
        "non_text_clusters_over_min_px": len(clusters),
        "non_text_cluster_px_total": int(sum(c["px"] for c in clusters)),
        "clusters": clusters[: args.top],
    }
    with open(os.path.join(args.out, "parity-audit.json"), "w") as fh:
        json.dump(summary, fh, indent=1)

    print(f"RAW      mae {raw['mae']}  gt16 {raw['gt16']} ({raw['gt16_pct']}%)  identical {raw['identical_pct']}%")
    for name, *_ in REGIONS:
        r = regions[name]
        print(f"  {name:8s} mae {r['mae']:8.4f}  gt16 {r['gt16']:6d} ({r['gt16_pct']:.3f}%)")
    print(f"THUMB4x  mae {thumbnail['mae']}  cells>8 {thumbnail['gt8']} ({thumbnail['gt8_pct']}% of {thumbnail['pixels']})")
    print(f"FLAT     {flat_stats['exact_match_pct']}% exact over {flat_stats['pct_of_frame']}% of frame "
          f"(bad {flat_stats['bad_px']}px, {flat_stats['bad_px_gt16']} >16)")
    if text_stats:
        t, o = text_stats["residual_on_text_ink"], text_stats["residual_off_text_ink"]
        print(f"TEXT     ink {text_stats['text_ink_px']}px ({text_stats['text_ink_pct_of_frame']}%): "
              f"mae {t['mae']} gt16 {t['gt16']}; OFF-ink gt16 {o['gt16']} "
              f"({text_stats['share_of_gt16_on_text_ink']}% of all gt16 is on text ink)")
    print(f"NON-TEXT clusters: {len(clusters)} over {args.min_px}px, total {summary['non_text_cluster_px_total']}px")
    for i, c in enumerate(clusters[:15]):
        print(f"  {i:02d} bbox={c['bbox']} px={c['px']} gt16={c['gt16']} mean={c['mean']} max={c['max']}")


if __name__ == "__main__":
    main()
