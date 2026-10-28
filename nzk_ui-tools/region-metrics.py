#!/usr/bin/env python3
"""region-metrics.py — whole-frame and per-region fidelity, composited identically.

The shared ab.py opens both PNGs with `convert("RGB")`. When one side is RGBA
with non-opaque pixels that silently drops alpha, which inflates MAE for reasons
that have nothing to do with layout. This tool composites BOTH images over
opaque black first, so the two sides are treated the same way, then reports the
same statistics per region.

Usage:
  region-metrics.py --ref REF.png --fork FORK.png [--json OUT.json]
"""
import argparse
import json
import sys

import numpy as np
from PIL import Image

_WIN = 5
_C1 = (0.01 * 255) ** 2
_C2 = (0.03 * 255) ** 2


def _box_filter(a, win):
    pad = win // 2
    p = np.pad(a, ((pad, pad), (pad, pad)), mode="reflect")
    c = np.cumsum(np.cumsum(p, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)), mode="constant")
    h, w = a.shape
    s = (c[win:win + h, win:win + w] - c[0:h, win:win + w]
         - c[win:win + h, 0:w] + c[0:h, 0:w])
    return s / float(win * win)


def ssim(ga, gb):
    mu_a, mu_b = _box_filter(ga, _WIN), _box_filter(gb, _WIN)
    mu_aa, mu_bb, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b
    sig_aa = _box_filter(ga * ga, _WIN) - mu_aa
    sig_bb = _box_filter(gb * gb, _WIN) - mu_bb
    sig_ab = _box_filter(ga * gb, _WIN) - mu_ab
    num = (2 * mu_ab + _C1) * (2 * sig_ab + _C2)
    den = (mu_aa + mu_bb + _C1) * (sig_aa + sig_bb + _C2)
    return float((num / np.maximum(den, 1e-12)).mean())


def over_black(img):
    """Composite an RGBA image over opaque black; return float RGB."""
    a = np.asarray(img, dtype=np.float64)
    if a.ndim == 2:
        a = np.stack([a] * 3, axis=-1)
    if a.shape[2] == 3:
        return a
    alpha = a[:, :, 3:4] / 255.0
    return a[:, :, :3] * alpha


REGIONS = [
    ("whole",            0,    0, 1600, 1000),
    ("topbar(0-60)",     0,    0, 1600,   60),
    ("row2(60-110)",     0,   60, 1600,   50),
    ("body(110-1000)",   0,  110, 1600,  890),
    ("body-left",        0,  110, 1240,  890),
    ("body-right",    1240,  110,  360,  890),
    ("sidebar-full",  1240,   52,  360,  948),
    ("canvas",           0,   52, 1240,  948),
    ("canvas-toast",   880,   55,  360,   70),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--fork", required=True)
    ap.add_argument("--json")
    args = ap.parse_args()

    ref = over_black(Image.open(args.ref))
    fork = over_black(Image.open(args.fork))
    if ref.shape != fork.shape:
        print(f"size mismatch {ref.shape} vs {fork.shape}", file=sys.stderr)
        return 2

    rows = []
    for name, x, y, w, h in REGIONS:
        r = ref[y:y + h, x:x + w]
        f = fork[y:y + h, x:x + w]
        d = np.abs(r - f)
        dmax = d.max(axis=2)
        ga = np.asarray(Image.fromarray(r.astype(np.uint8)).convert("L"), dtype=np.float64)
        gb = np.asarray(Image.fromarray(f.astype(np.uint8)).convert("L"), dtype=np.float64)
        rows.append({
            "region": name,
            "x": x, "y": y, "w": w, "h": h,
            "mae": round(float(d.mean()), 4),
            "rmse": round(float(np.sqrt((d ** 2).mean())), 4),
            "ssim": round(ssim(ga, gb), 6),
            "pct_gt_16": round(float((dmax > 16).sum()) / dmax.size * 100, 4),
            "pct_identical": round(float((dmax == 0).sum()) / dmax.size * 100, 4),
        })

    print(f"{'region':18} {'MAE':>8} {'RMSE':>8} {'SSIM':>9} {'pct>16':>8} {'identical':>10}")
    for r in rows:
        print(f"{r['region']:18} {r['mae']:8.4f} {r['rmse']:8.4f} {r['ssim']:9.6f} "
              f"{r['pct_gt_16']:8.4f} {r['pct_identical']:10.4f}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
