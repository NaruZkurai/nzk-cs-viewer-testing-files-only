#!/usr/bin/env python3
"""text-run-diff.py — per-text-run delta table between a v2 reference frame and
the v3 fork frame.

The whole-frame MAE is dominated by text placement/rasterisation.  ui-diff.py
finds connected *difference* clusters; this tool instead finds the *ink* runs in
each image independently (so a word that is simply absent is still a run), merges
glyphs into words/lines, matches ref runs to fork runs by bbox proximity, and
reports the per-run geometric / width / colour / content delta plus an error
contribution.  That table is what a fixer can act on.

Pipeline
  1. load ref (RGB) + fork (RGBA composited over opaque black), same shape.
  2. per region (default: topbar, toolbar row 2, sidebar, canvas):
       - luminance, local background = median of 8x8 blocks (nearest-upsampled),
       - ink = |lum - bg| > --thr, both polarities (light and dark text),
       - row-run union-find connected components (as in ui-diff.py),
       - drop components that cannot be glyphs (taller than --max-glyph-h, or
         thin/long rules/icons), then merge glyphs into words: same text line
         (vertical overlap) and horizontal gap <= --merge-gap px.
  3. match ref words to fork words greedily by bbox-centre distance inside a
     |dy|<=--max-dy, |dx|<=--max-dx window, ascending cost.
  4. per pair: ref/fork bbox, dx, dy, width/height deltas, ink colour (mean RGB
     of the strongest ink pixels + p90 luminance), diff pixels / mean diff /
     contribution = diff_pixels * mean_diff inside the union bbox; classify.
     Unmatched ref = MISSING, unmatched fork = EXTRA.
  5. write text-runs.json, text-runs.md (sorted by contribution desc), and x4
     crops of the worst runs.

Honest limitations (also printed in the md):
  * glyph anti-aliasing means run widths differ by 1-3 px even when the advance
    is identical; a WIDTH/SCALE label is a hypothesis, not proof.  A shift can
    also be letter-spacing *inside* a run (the bbox stays put but letters move).
  * connected runs merge touching glyphs; a word with a wide space may split.
  * ink detection cannot tell a text glyph from a small icon/rule of the same
    size; components > --max-glyph-h are dropped as non-text.
  * a run absent on both sides (different content that is still ink, same size)
    is reported as a matched CONTENT/GLYPH delta, not as MISSING/EXTRA.

Usage
  tools/text-run-diff.py --ref REF.png --fork FORK.png --out DIR \
      [--thr 16] [--merge-gap 3] [--max-dx 16] [--max-dy 8] \
      [--region name=x0,y0,x1,y1 ...] [--top 40] [--crops 15] \
      [--textprobe PATH] [--quiet]

Requires numpy + PIL only (no scipy/cv2).  Read-only wrt the frames; writes only
under --out.  Runs in a few seconds on 1600x1000.
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image

DEFAULT_REGIONS = [
    ("topbar", 0, 0, 1600, 52),
    ("toolbar", 0, 52, 1600, 110),
    ("sidebar", 1240, 52, 1600, 1000),
    ("canvas", 0, 110, 1240, 1000),
]

CLASSES = ("GEOMETRY-SHIFT", "WIDTH/SCALE", "COLOUR/AA", "MISSING", "EXTRA", "CONTENT/GLYPH")


def load_rgb(path, shape=None):
    im = Image.open(path)
    if im.mode == "P":
        im = im.convert("RGBA")
    a = np.asarray(im, dtype=np.float64)
    if a.ndim == 2:
        a = np.stack([a] * 3, axis=-1)
    if a.shape[2] == 4:
        a = a[:, :, :3] * (a[:, :, 3:4] / 255.0)  # composite over opaque black
    a = a[:, :, :3]
    if shape is not None and a.shape[:2] != shape:
        raise SystemExit("image size mismatch: %s is %s, expected %s" % (path, a.shape[:2], shape))
    return a


def luma(a):
    return 0.2126 * a[:, :, 0] + 0.7152 * a[:, :, 1] + 0.0722 * a[:, :, 2]


def block_median(lum, bs):
    h, w = lum.shape
    ph, pw = (-h) % bs, (-w) % bs
    if ph or pw:
        lum = np.pad(lum, ((0, ph), (0, pw)), mode="edge")
    H, W = lum.shape
    blocks = lum.reshape(H // bs, bs, W // bs, bs).transpose(0, 2, 1, 3).reshape(H // bs, W // bs, bs * bs)
    med = np.median(blocks, axis=2)
    med = np.repeat(np.repeat(med, bs, axis=0), bs, axis=1)
    return med[:h, :w]


def label_components(mask):
    """Row-run union-find connected components (4-connectivity), int32 labels."""
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

    rows = []
    prev = []
    for y in range(h):
        xs = np.flatnonzero(mask[y])
        runs = []
        if xs.size:
            starts = xs[np.r_[True, np.diff(xs) > 1]]
            ends = xs[np.r_[np.diff(xs) > 1, True]]
            for x0, x1 in zip(starts.tolist(), ends.tolist()):
                lab = len(parent)
                parent.append(lab)
                runs.append([x0, x1, lab])
                for px0, px1, plab in prev:
                    if px0 <= x1 and x0 <= px1:
                        union(plab, lab)
        rows.append(runs)
        prev = runs
    out = np.zeros((h, w), dtype=np.int32)
    if not parent:
        return out
    roots = [find(i) for i in range(len(parent))]
    for y, runs in enumerate(rows):
        for x0, x1, lab in runs:
            out[y, x0:x1 + 1] = roots[lab] + 1
    return out


def glyph_components(ink, lum, rgb, x0, y0, thr, max_h):
    """Components that can plausibly be glyphs, with geometry + ink stats."""
    lab = label_components(ink)
    n = int(lab.max())
    if n == 0:
        return []
    flat = lab.ravel()
    ys, xs = np.divmod(np.flatnonzero(flat), ink.shape[1])
    ids = flat[flat > 0]
    order = np.argsort(ids, kind="stable")
    ids_s = ids[order]
    ys_s, xs_s = ys[order], xs[order]
    bounds = np.flatnonzero(np.diff(ids_s)) + 1
    starts = np.r_[0, bounds]
    ends = np.r_[bounds, len(ids_s)]
    comps = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        yy, xx = ys_s[s:e], xs_s[s:e]
        bx0, bx1 = int(xx.min()), int(xx.max())
        by0, by1 = int(yy.min()), int(yy.max())
        h, w = by1 - by0 + 1, bx1 - bx0 + 1
        if h > max_h or w > 400 or len(yy) < 2:
            continue
        if h <= 2 and w > 12:  # thin rule / border line
            continue
        px = rgb[yy, xx]
        ll = lum[yy, xx]
        top = ll >= np.percentile(ll, 75)
        comps.append({
            "x0": bx0 + x0, "y0": by0 + y0, "x1": bx1 + x0, "y1": by1 + y0,
            "w": w, "h": h, "area": int(len(yy)),
            "ink_rgb": [round(float(v), 1) for v in px[top].mean(axis=0)],
            "ink_lum_p90": round(float(np.percentile(ll, 90)), 1),
            "ink_lum_mean": round(float(ll.mean()), 1),
        })
    return comps


def _voverlap(a, b):
    return max(0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]) + 1)


def merge_words(comps, gap):
    """Merge glyph components into words: same line (vertical overlap >= 40% of
    the shorter glyph) and horizontal gap <= gap px."""
    if not comps:
        return []
    comps = sorted(comps, key=lambda c: (c["y0"], c["x0"]))
    # group into text lines by vertical overlap with the line's running span
    lines = []
    for c in comps:
        best, best_ov = None, 0
        for ln in lines:
            ov = _voverlap(c, ln)
            if ov > best_ov:
                best, best_ov = ln, ov
        hmin = min(c["h"], best["y1"] - best["y0"] + 1) if best else 0
        if best is not None and best_ov >= max(2, 0.4 * hmin):
            best["y0"] = min(best["y0"], c["y0"])
            best["y1"] = max(best["y1"], c["y1"])
            best["items"].append(c)
        else:
            lines.append({"y0": c["y0"], "y1": c["y1"], "items": [c]})
    words = []
    for ln in lines:
        items = sorted(ln["items"], key=lambda c: c["x0"])
        cur = None
        for c in items:
            if cur is not None and c["x0"] - cur["x1"] - 1 <= gap:
                cur["x1"] = max(cur["x1"], c["x1"])
                cur["y0"] = min(cur["y0"], c["y0"])
                cur["y1"] = max(cur["y1"], c["y1"])
                cur["w"] = cur["x1"] - cur["x0"] + 1
                cur["h"] = cur["y1"] - cur["y0"] + 1
                cur["area"] += c["area"]
                cur["glyphs"] += 1
                cur["_lums"].append(c["ink_lum_p90"])
                cur["_rgbs"].append(c["ink_rgb"])
            else:
                cur = {k: c[k] for k in ("x0", "y0", "x1", "y1", "w", "h", "area")}
                cur["glyphs"] = 1
                cur["_lums"] = [c["ink_lum_p90"]]
                cur["_rgbs"] = [c["ink_rgb"]]
                words.append(cur)
    for w in words:
        w["ink_lum_p90"] = round(float(np.mean(w.pop("_lums"))), 1)
        w["ink_rgb"] = [round(float(v), 1) for v in np.mean(np.array(w.pop("_rgbs")), axis=0)]
        w["cx"] = (w["x0"] + w["x1"]) / 2.0
        w["cy"] = (w["y0"] + w["y1"]) / 2.0
    return words


def region_ink(lum, rgb, region, thr, neutral=False):
    x0, y0, x1, y1 = region
    sub = lum[y0:y1, x0:x1]
    bg = block_median(sub, 8)
    ink = np.abs(sub - bg) > thr
    if neutral:
        # canvas graph labels are neutral light grey (#d4d4d4); graph edges and
        # node fills are tinted.  This keeps edges/boxes out of the text runs.
        rgb_sub = rgb[y0:y1, x0:x1]
        sat = rgb_sub.max(axis=2) - rgb_sub.min(axis=2)
        ink &= (sat < 14) & (sub > 120)
    return ink, bg


def detect_words(lum, rgb, region, thr, max_h, gap, neutral=False, max_w=400):
    x0, y0, x1, y1 = region
    sub = lum[y0:y1, x0:x1]
    ink, _ = region_ink(lum, rgb, region, thr, neutral)
    comps = glyph_components(ink, sub, rgb[y0:y1, x0:x1], x0, y0, thr, max_h)
    comps = [c for c in comps if c["w"] <= max_w]
    return merge_words(comps, gap)


def match_words(refs, forks, max_dx, max_dy):
    pairs = []
    for i, r in enumerate(refs):
        for j, f in enumerate(forks):
            dx = f["cx"] - r["cx"]
            dy = f["cy"] - r["cy"]
            if abs(dx) > max_dx or abs(dy) > max_dy:
                continue
            dw = f["w"] - r["w"]
            dh = f["h"] - r["h"]
            cost = math.hypot(dx, dy) + 0.15 * (abs(dw) + abs(dh))
            pairs.append((cost, i, j))
    pairs.sort()
    used_r, used_f, out = set(), set(), []
    for _, i, j in pairs:
        if i in used_r or j in used_f:
            continue
        used_r.add(i)
        used_f.add(j)
        out.append((i, j))
    return out, used_r, used_f


def ink_color(rgb, mask, x0, y0, x1, y1, polarity):
    sub = rgb[y0:y1 + 1, x0:x1 + 1]
    m = mask[y0:y1 + 1, x0:x1 + 1]
    px, ll = sub[m], luma(sub)[m]
    if px.size == 0:
        return [0.0, 0.0, 0.0], 0.0
    # strongest ink: top quartile by distance from the local mean luminance
    cut = np.percentile(ll, 75 if polarity > 0 else 25)
    sel = ll >= cut if polarity > 0 else ll <= cut
    return ([round(float(v), 1) for v in px[sel].mean(axis=0)],
            round(float(np.percentile(ll, 90 if polarity > 0 else 10)), 1))


def classify(dx, dy, dw, dh, ink_dl, mean_diff, diff_px):
    if abs(dx) >= 2 or abs(dy) >= 2:
        return "GEOMETRY-SHIFT"
    if abs(dw) >= 3:
        return "WIDTH/SCALE"
    if abs(ink_dl) >= 20:
        return "COLOUR/AA"
    if mean_diff < 35:
        return "COLOUR/AA"
    return "CONTENT/GLYPH"


def crop(path, a, box, scale=4, pad=4):
    x0, y0, x1, y1 = box
    h, w = a.shape[:2]
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(w - 1, x1 + pad), min(h - 1, y1 + pad)
    im = Image.fromarray(np.clip(a[y0:y1 + 1, x0:x1 + 1], 0, 255).astype(np.uint8))
    return im.resize((im.width * scale, im.height * scale), Image.NEAREST)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--fork", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--thr", type=float, default=16.0, help="ink contrast threshold in luminance levels (default 16)")
    ap.add_argument("--merge-gap", type=int, default=3, help="max horizontal px gap between glyphs of one word (default 3)")
    ap.add_argument("--max-glyph-h", type=int, default=26, help="drop components taller than this as non-text (default 26)")
    ap.add_argument("--max-dx", type=int, default=16)
    ap.add_argument("--max-dy", type=int, default=8)
    ap.add_argument("--region", action="append", default=None,
                    help="name=x0,y0,x1,y1 (x1/y1 exclusive); repeatable; default: topbar/toolbar/sidebar/canvas")
    ap.add_argument("--top", type=int, default=40, help="rows in the markdown table (default 40)")
    ap.add_argument("--crops", type=int, default=15, help="number of worst runs to crop (default 15)")
    ap.add_argument("--textprobe", default=None, help="optional v2 textprobe.json / dom.json to label runs with text")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    ref = load_rgb(args.ref)
    fork = load_rgb(args.fork, ref.shape[:2])
    if ref.shape != fork.shape:
        raise SystemExit("shape mismatch %s vs %s" % (ref.shape, fork.shape))
    H, W = ref.shape[:2]
    rl, fl = luma(ref), luma(fork)
    dmap = np.abs(ref - fork).mean(axis=2)

    if args.region:
        regions = []
        for spec in args.region:
            name, _, box = spec.partition("=")
            x0, y0, x1, y1 = (int(v) for v in box.split(","))
            regions.append((name, x0, y0, x1, y1))
    else:
        regions = DEFAULT_REGIONS

    textprobe = None
    if args.textprobe and os.path.isfile(args.textprobe):
        try:
            with open(args.textprobe) as fh:
                raw = json.load(fh)
            nodes = raw.get("nodes") or raw.get("texts")
            if nodes is None:
                nodes = []
                for key in ("boxes", "all"):
                    val = raw.get(key)
                    if isinstance(val, dict):
                        for v in val.values():
                            if isinstance(v, list):
                                nodes.extend(v)
                            elif isinstance(v, dict):
                                nodes.append(v)
                # v2 dom.json: {x,y,w,h,text}
                nodes = [{"rect": [v["x"], v["y"], v["w"], v["h"]], "text": v["text"]}
                         for v in nodes if isinstance(v, dict) and "text" in v and "x" in v]
            textprobe = {"nodes": nodes}
        except Exception as exc:  # honest: probe is optional
            if not args.quiet:
                print("warning: could not parse textprobe: %s" % exc, file=sys.stderr)

    def probe_text(box):
        """Best-overlap text content from an optional v2 text node dump."""
        if not textprobe:
            return None
        nodes = textprobe.get("nodes") or textprobe.get("texts") or []
        best, best_score = None, -1.0
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        for nd in nodes:
            r = nd.get("rect") or nd.get("box")
            t = nd.get("text") or nd.get("content")
            if not r or not t:
                continue
            rx0, ry0, rx1, ry1 = r[0], r[1], r[0] + r[2], r[1] + r[3]
            if not (rx0 <= cx <= rx1 and ry0 <= cy <= ry1):
                continue  # label must contain the run centre
            area = max(1.0, r[2] * r[3])
            ox = max(0.0, min(x1, rx1) - max(x0, rx0))
            oy = max(0.0, min(y1, ry1) - max(y0, ry0))
            score = (ox * oy) / area - 1e-6 * area
            if score > best_score:
                best, best_score = str(t), score
        return best

    os.makedirs(args.out, exist_ok=True)
    cropdir = os.path.join(args.out, "crops")
    os.makedirs(cropdir, exist_ok=True)

    all_runs = []
    summaries = {}
    for name, x0, y0, x1, y1 in regions:
        x1, y1 = min(W, x1), min(H, y1)
        reg_mask = np.zeros((H, W), dtype=bool)
        reg_mask[max(0, y0):y1, max(0, x0):x1] = True
        # local background + polarity, recomputed per region for ink colour stats
        neutral = name == "canvas"
        ref_ink_sub, _ = region_ink(rl, ref, (x0, y0, x1, y1), args.thr, neutral)
        fork_ink_sub, _ = region_ink(fl, fork, (x0, y0, x1, y1), args.thr, neutral)
        ref_ink = np.zeros((H, W), dtype=bool)
        fork_ink = np.zeros((H, W), dtype=bool)
        ref_ink[y0:y1, x0:x1] = ref_ink_sub
        fork_ink[y0:y1, x0:x1] = fork_ink_sub
        max_w = 150 if neutral else 400
        refs = detect_words(rl, ref, (x0, y0, x1, y1), args.thr, args.max_glyph_h, args.merge_gap, neutral, max_w)
        forks = detect_words(fl, fork, (x0, y0, x1, y1), args.thr, args.max_glyph_h, args.merge_gap, neutral, max_w)
        pairs, used_r, used_f = match_words(refs, forks, args.max_dx, args.max_dy)
        region_mass = float(dmap[max(0, y0):y1, max(0, x0):x1].sum())
        runs = []

        def add_run(r, f, kind):
            if f is None:
                box = (r["x0"], r["y0"], r["x1"], r["y1"])
            elif r is None:
                box = (f["x0"], f["y0"], f["x1"], f["y1"])
            else:
                box = (min(r["x0"], f["x0"]), min(r["y0"], f["y0"]),
                       max(r["x1"], f["x1"]), max(r["y1"], f["y1"]))
            bx0, by0, bx1, by1 = box
            dd = dmap[by0:by1 + 1, bx0:bx1 + 1]
            diff_px = int((dd > args.thr).sum())
            mean_diff = float(dd[dd > args.thr].mean()) if diff_px else 0.0
            ink_union = ref_ink[by0:by1 + 1, bx0:bx1 + 1] | fork_ink[by0:by1 + 1, bx0:bx1 + 1]
            contribution = float(dd[ink_union].sum())
            bbox_contribution = float(dd.sum())
            dx = dy = dw = dh = 0.0
            ink_dl = 0.0
            if r is not None and f is not None:
                dx = f["cx"] - r["cx"]
                dy = f["cy"] - r["cy"]
                dw = f["w"] - r["w"]
                dh = f["h"] - r["h"]
                ink_dl = f["ink_lum_p90"] - r["ink_lum_p90"]
            cls = kind if kind else classify(dx, dy, dw, dh, ink_dl, mean_diff, diff_px)
            text = probe_text(box)
            runs.append({
                "region": name,
                "class": cls,
                "text": text,
                "ref_bbox": [r["x0"], r["y0"], r["x1"], r["y1"]] if r else None,
                "fork_bbox": [f["x0"], f["y0"], f["x1"], f["y1"]] if f else None,
                "union_bbox": [bx0, by0, bx1, by1],
                "dx": round(dx, 2), "dy": round(dy, 2),
                "dw": round(dw, 2), "dh": round(dh, 2),
                "ref_w": r["w"] if r else None, "fork_w": f["w"] if f else None,
                "ref_ink_rgb": r["ink_rgb"] if r else None,
                "fork_ink_rgb": f["ink_rgb"] if f else None,
                "ref_ink_lum_p90": r["ink_lum_p90"] if r else None,
                "fork_ink_lum_p90": f["ink_lum_p90"] if f else None,
                "ink_lum_delta": round(ink_dl, 1),
                "diff_pixels": diff_px,
                "mean_diff": round(mean_diff, 2),
                "contribution": round(contribution, 1),
                "bbox_contribution": round(bbox_contribution, 1),
                "ink_pixels": int(ink_union.sum()),
            })

        for i, j in pairs:
            add_run(refs[i], forks[j], None)
        for i, r in enumerate(refs):
            if i not in used_r:
                add_run(r, None, "MISSING")
        for j, f in enumerate(forks):
            if j not in used_f:
                add_run(None, f, "EXTRA")

        runs.sort(key=lambda r: -r["contribution"])
        all_runs.extend(runs)
        by_class = {}
        for cls in CLASSES:
            ss = [r for r in runs if r["class"] == cls]
            mass = sum(r["contribution"] for r in ss)
            by_class[cls] = {"runs": len(ss), "contribution": round(mass, 1),
                             "pct_of_region_error": round(100.0 * mass / region_mass, 2) if region_mass else 0.0}
        summaries[name] = {
            "rect": [x0, y0, x1, y1],
            "ref_runs": len(refs), "fork_runs": len(forks),
            "matched": len(pairs), "missing": len(refs) - len(pairs), "extra": len(forks) - len(pairs),
            "region_error_mass": round(region_mass, 1),
            "text_run_contribution": round(sum(r["contribution"] for r in runs), 1),
            "pct_of_region_error": round(100.0 * sum(r["contribution"] for r in runs) / region_mass, 2) if region_mass else 0.0,
            "by_class": by_class,
            "worst": runs[:5],
        }
        if not args.quiet:
            print("%-8s ref=%3d fork=%3d matched=%3d missing=%2d extra=%2d mass=%9.0f (%5.1f%%)"
                  % (name, len(refs), len(forks), len(pairs), len(refs) - len(pairs),
                     len(forks) - len(pairs), region_mass,
                     100.0 * sum(r["contribution"] for r in runs) / region_mass if region_mass else 0.0))

    all_runs.sort(key=lambda r: -r["contribution"])
    crop_paths = {}
    for rank, r in enumerate(all_runs[:args.crops], 1):
        x0, y0, x1, y1 = r["union_bbox"]
        tag = "%02d-%s-%s" % (rank, r["region"], r["class"].lower().replace("/", "-"))
        tri = Image.new("RGB", (crop(None, ref, (x0, y0, x1, y1)).width * 3, crop(None, ref, (x0, y0, x1, y1)).height), (0, 0, 0))
        cr = crop(None, ref, (x0, y0, x1, y1))
        cf = crop(None, fork, (x0, y0, x1, y1))
        diff = np.abs(ref - fork).mean(axis=2) * 4.0
        cd = crop(None, np.stack([diff] * 3, axis=-1), (x0, y0, x1, y1))
        tri.paste(cr, (0, 0)); tri.paste(cf, (cr.width, 0)); tri.paste(cd, (cr.width * 2, 0))
        p = os.path.join(cropdir, tag + ".png")
        tri.save(p)
        r["crop"] = os.path.relpath(p, args.out)
        crop_paths[rank] = p

    report = {
        "tool": "text-run-diff.py",
        "ref": os.path.abspath(args.ref),
        "fork": os.path.abspath(args.fork),
        "size": [W, H],
        "params": {"thr": args.thr, "merge_gap": args.merge_gap, "max_glyph_h": args.max_glyph_h,
                   "max_dx": args.max_dx, "max_dy": args.max_dy},
        "textprobe": os.path.abspath(args.textprobe) if textprobe else None,
        "regions": summaries,
        "runs": all_runs,
        "totals": {
            "runs": len(all_runs),
            "contribution": round(sum(r["contribution"] for r in all_runs), 1),
        },
    }
    jpath = os.path.join(args.out, "text-runs.json")
    with open(jpath, "w") as fh:
        json.dump(report, fh, indent=1)

    def fmt_run(r):
        geom = "dx=%+.1f dy=%+.1f dw=%+.1f" % (r["dx"], r["dy"], r["dw"])
        return "| %s | %s | %s | %s | %s | %s | %.0f |" % (
            r["region"], r["class"], r["union_bbox"], geom, r["ink_lum_delta"],
            r.get("text") or ("runs %dx%d" % (r["ref_w"] or 0, r["ref_w"] or 0)), r["contribution"])

    md = ["# Per-text-run delta table", "",
          "Ref: `%s`  \nFork: `%s`  \nParams: thr=%s, merge_gap=%s, max_glyph_h=%s, max_dx=%s, max_dy=%s" %
          (ref_path := os.path.abspath(args.ref), os.path.abspath(args.fork), args.thr, args.merge_gap,
           args.max_glyph_h, args.max_dx, args.max_dy), "",
          "`contribution` = sum of per-pixel mean-channel |ref-fork| **on the union of both "
          "images' ink masks** inside the run's union bbox; background/container error is excluded "
          "(see `bbox_contribution` in the JSON for the old whole-bbox number). `dx/dy` = fork bbox centre - ref bbox centre.", ""]
    for name, s in summaries.items():
        md += ["## %s — rect %s" % (name, s["rect"]), "",
               "%d ref runs, %d fork runs, %d matched, %d missing, %d extra." %
               (s["ref_runs"], s["fork_runs"], s["matched"], s["missing"], s["extra"]),
               "Region error mass %.0f; text runs account for %.1f%% of it." %
               (s["region_error_mass"], s["pct_of_region_error"]), "",
               "| class | runs | contribution | % of region error |", "|---|---|---|---|"]
        for cls in CLASSES:
            b = s["by_class"][cls]
            if b["runs"]:
                md.append("| %s | %d | %.0f | %.2f%% |" % (cls, b["runs"], b["contribution"], b["pct_of_region_error"]))
        md.append("")
    md += ["", "Contribution is ink-restricted (text-attributable); the JSON also carries bbox_contribution.", "",
           "## Top %d deltas" % min(args.top, len(all_runs)), "",
           "| # | region | class | bbox | dx/dy/dw | ink Δlum | text | contribution | crop |",
           "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(all_runs[:args.top], 1):
        geom = "%+.1f / %+.1f / %+.1f" % (r["dx"], r["dy"], r["dw"])
        text = " ".join((r.get("text") or "").split()).replace("|", "\\|")[:80]
        crop_cell = "[png](%s)" % r["crop"] if "crop" in r else ""
        md.append("| %d | %s | %s | %s | %s | %s | %s | %.0f | %s |" %
                  (i, r["region"], r["class"], r["union_bbox"], geom,
                   ("%+.1f" % r["ink_lum_delta"]) if r["ref_bbox"] and r["fork_bbox"] else "—",
                   text, r["contribution"], crop_cell))
    md += ["", "## Limitations (read before acting)", "",
           "* anti-aliasing changes every run's measured width by 1-3 px; WIDTH/SCALE is a hypothesis. "
           "A bbox-stable run can still have interior letter-spacing drift.",
           "* detection merges touching glyphs; wide spaces split a phrase into words (intended).",
           "* small icons/rules that share glyph dimensions can appear as runs; anything taller than "
           "%d px is dropped." % args.max_glyph_h,
           "* colour deltas are measured on the strongest 25%% of ink pixels, not the glyph core only; "
           "subpixel/AA differences count as COLOUR/AA.",
           "* matching is greedy on centre distance (|dx|<=%d, |dy|<=%d px); a run displaced further "
           "than that is reported MISSING+EXTRA rather than shifted." % (args.max_dx, args.max_dy),
           ""]
    mpath = os.path.join(args.out, "text-runs.md")
    with open(mpath, "w") as fh:
        fh.write("\n".join(md))

    print("\nwrote %s\n      %s\n      %s" % (jpath, mpath, cropdir))
    print("total runs=%d contribution=%.0f" % (len(all_runs), report["totals"]["contribution"]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
