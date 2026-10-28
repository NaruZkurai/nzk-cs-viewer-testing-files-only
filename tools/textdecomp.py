#!/usr/bin/env python3
"""textdecomp.py — independent text-vs-nontext decomposition of an A/B diff.

Deliberately separate from tools/textshare.py (native-css's shared tool) so the
two decompositions can be compared: same inputs, same definitions, independent
implementation. Where they disagree, the disagreement is the result.

Definitions
-----------
ink mask      pixels where |ref - notext| > `--mask-threshold` on any channel.
              `notext` is the SAME page captured with glyph ink made transparent
              (tools/textprobe.mjs `<out>.notext.png`), so this is the reference's
              own glyph coverage, not a guess.
error mass    sum over pixels of mean-channel |ref - fork|. Shares are
              mass-in-mask / mass-total, which is an exact decomposition.
MAE           mean per-pixel mean-channel |ref - fork| over a mask.

Usage
-----
  tools/textdecomp.py --ref evidence/ab/reference-1600x1000.png \
      --notext /tmp/tp/textprobe.notext.png \
      --fork evidence/ab/fork-1600x1000.png --out /tmp/tp/decomp \
      [--mask-threshold 8] [--ref-crop X,Y,W,H] [--fork-crop X,Y,W,H]
"""
import argparse
import json
import os
import sys

import numpy as np

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover
    print(f"textdecomp: Pillow is required ({exc})", file=sys.stderr)
    sys.exit(2)


def load(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.int16)


def crop(a, spec):
    if not spec:
        return a
    x, y, w, h = (int(v) for v in spec.replace(",", " ").split())
    return a[y:y + h, x:x + w]


def metrics(ref, fork, mask):
    diff = np.abs(ref - fork).mean(axis=2)          # per-pixel mean channel
    m = mask.astype(bool)
    total = float(diff.sum())
    inside = float(diff[m].sum()) if m.any() else 0.0
    outside = total - inside
    return {
        "text_pixels": int(m.sum()),
        "text_coverage_pct": round(100.0 * float(m.mean()), 4),
        "mae_text": round(float(diff[m].mean()), 4) if m.any() else 0.0,
        "mae_nontext": round(float(diff[~m].mean()), 4) if (~m).any() else 0.0,
        "mae_all": round(float(diff.mean()), 4),
        "pct_gt16_text": round(100.0 * float((diff[m] > 16).mean()), 4) if m.any() else 0.0,
        "pct_gt16_nontext": round(100.0 * float((diff[~m] > 16).mean()), 4) if (~m).any() else 0.0,
        "err_mass_total": round(total, 2),
        "err_mass_text": round(inside, 2),
        "err_mass_nontext": round(outside, 2),
        "text_share_of_error_pct": round(100.0 * inside / total, 4) if total else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--notext", required=True)
    ap.add_argument("--fork", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mask-threshold", type=int, default=8)
    ap.add_argument("--ref-crop")
    ap.add_argument("--fork-crop")
    ap.add_argument("--label", default="full")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    ref = crop(load(args.ref), args.ref_crop)
    notext = crop(load(args.notext), args.ref_crop)
    fork = crop(load(args.fork), args.fork_crop)

    if ref.shape != fork.shape:
        print(f"textdecomp: shape mismatch ref{ref.shape} fork{fork.shape}", file=sys.stderr)
        sys.exit(2)

    d = np.abs(ref - notext)
    mask = d.max(axis=2) > args.mask_threshold

    result = {
        "label": args.label,
        "ref": args.ref, "notext": args.notext, "fork": args.fork,
        "ref_crop": args.ref_crop, "fork_crop": args.fork_crop,
        "shape": list(ref.shape),
        "mask_threshold": args.mask_threshold,
        "method": {
            "ink_mask": "|ref - notext| > threshold on any channel",
            "notext": "tools/textprobe.mjs <out>.notext.png (same page, glyph ink transparent)",
            "error_mass": "sum of mean-channel |ref - fork|; shares are exact mass shares",
        },
    }
    result["threshold_sensitivity"] = {
        str(t): metrics(ref, fork, d.max(axis=2) > t)["text_share_of_error_pct"]
        for t in (4, 8, 16, 32)
    }
    result.update(metrics(ref, fork, mask))

    # Evidence images: the ink mask itself and the error tinted by mask.
    Image.fromarray((mask.astype(np.uint8) * 255)).save(
        os.path.join(args.out, f"{args.label}-ink-mask.png"))
    tint = np.zeros_like(ref, dtype=np.uint8)
    diff = np.abs(ref - fork).astype(np.uint8)
    tint[..., 0] = np.where(mask, np.clip(diff[..., 0] * 4, 0, 255), 0)
    tint[..., 1] = np.where(~mask, np.clip(diff[..., 1] * 4, 0, 255), 0)
    Image.fromarray(tint).save(os.path.join(args.out, f"{args.label}-error-mask-tint.png"))

    path = os.path.join(args.out, f"{args.label}-textdecomp.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))
    print(f"textdecomp: wrote {path}")


if __name__ == "__main__":
    main()
