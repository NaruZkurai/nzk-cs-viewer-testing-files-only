#!/usr/bin/env python3
"""textshare.py — how much of a reference-vs-fork pixel error is TEXT?

Inputs (all 1600x1000 PNGs):
  --ref     the reference frame (Chromium-rendered original)
  --notext  the SAME reference page with glyph ink removed
            (textprobe.mjs writes <out>.notext.png)
  --fork    the fork frame (same size)
  --rects   textprobe.mjs JSON: every text node's bounding box
  --out     directory for masks / report

Definitions used in the report:
  ink mask        pixels where |ref - notext| > 8 on any channel  == where the
                  reference's glyphs actually put ink (backgrounds unaffected)
  text-box mask   union of every DOM text node's bounding rect
  error mass      sum over pixels of mean-channel |ref-fork|; shares are
                  mass-in-mask / mass-total, so they are exact decompositions
                  (they sum to 100% across a mask and its complement)

No third-party image library: PNG decode/encode via the `image`/PIL only if
present, else a self-contained decoder is used.
"""
import argparse
import json
import os
import struct
import sys
import zlib

import numpy as np

try:  # fast path; ab.py already depends on Pillow
    from PIL import Image as _PILImage
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False


def _read_png_pil(path):
    return np.asarray(_PILImage.open(path).convert("RGB"), dtype=np.uint8)


# ---------------------------------------------------------------- minimal PNG
def read_png(path):
    if _HAVE_PIL:
        return _read_png_pil(path)
    """Decode an 8-bit PNG (colour type 0/2/4/6, filters 0-4) to HxWxC uint8."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path}: not a PNG")
    pos = 8
    idat = bytearray()
    w = h = bitdepth = ctype = None
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bitdepth, ctype = struct.unpack(">IIBB", body[:10])
            if bitdepth != 8:
                raise SystemExit(f"{path}: only 8-bit supported (got {bitdepth})")
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(bytes(idat))
    stride = w * nch
    out = np.empty((h, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.int32)
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        line = np.frombuffer(raw[p:p + stride], dtype=np.uint8).astype(np.int32)
        p += stride
        if ft == 0:
            cur = line
        elif ft == 1:
            cur = line.copy()
            for i in range(nch, stride):
                cur[i] = (cur[i] + cur[i - nch]) & 0xFF
        elif ft == 2:
            cur = (line + prev) & 0xFF
        elif ft == 3:
            cur = line.copy()
            for i in range(stride):
                left = cur[i - nch] if i >= nch else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            cur = line.copy()
            for i in range(stride):
                a = cur[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 0xFF
        else:
            raise SystemExit(f"{path}: bad filter {ft} on row {y}")
        out[y] = cur.astype(np.uint8)
        prev = cur.astype(np.int32)
    return out.reshape(h, w, nch)


def to_rgb(a):
    if a.shape[2] == 1:
        return np.repeat(a, 3, axis=2)
    if a.shape[2] == 2:
        return np.repeat(a[:, :, :1], 3, axis=2)
    return a[:, :, :3]


def rgb_delta(a, b):
    """Mean-channel absolute difference per pixel (HxW float)."""
    return np.abs(a.astype(np.int32) - b.astype(np.int32)).mean(axis=2)


def write_mask_png(path, mask, ref_rgb):
    """Tint the reference red where mask is true, grey elsewhere."""
    h, w = mask.shape
    g = ref_rgb.mean(axis=2).astype(np.uint8)
    out = np.stack([g, g, g], axis=2)
    out[mask] = (0.25 * out[mask] + 0.75 * np.array([255, 0, 0])).astype(np.uint8)
    if _HAVE_PIL:
        _PILImage.fromarray(out, "RGB").save(path)
        return
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        rows += out[y].tobytes()
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


# --------------------------------------------------------------------- report
def stats(delta, mask=None):
    if mask is None:
        d = delta.ravel()
        n = d.size
        mass = float(d.sum())
        return {"pixels": int(n), "share_of_frame_pct": 100.0,
                "mass_share_pct": 100.0,
                "mae": round(float(d.mean()), 4),
                "pct_gt16": round(float((d > 16).mean()) * 100, 4),
                "error_mass": round(mass, 1)}
    m = mask.ravel()
    d = delta.ravel()[m]
    n = int(m.sum())
    tot_px = int(m.size)
    mass = float(d.sum())
    return {"pixels": n, "share_of_frame_pct": round(n / tot_px * 100, 4),
            "mae": round(float(d.mean()), 4) if n else 0.0,
            "pct_gt16": round(float((d > 16).mean()) * 100, 4) if n else 0.0,
            "error_mass": round(mass, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--notext", required=True)
    ap.add_argument("--fork", required=True)
    ap.add_argument("--rects", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ink-threshold", type=int, default=8)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    ref = to_rgb(read_png(args.ref))
    notext = to_rgb(read_png(args.notext))
    fork = to_rgb(read_png(args.fork))
    if ref.shape != fork.shape:
        raise SystemExit(f"size mismatch ref={ref.shape} fork={fork.shape}")
    h, w = ref.shape[:2]

    err = rgb_delta(ref, fork)
    ink = (np.abs(ref.astype(np.int32) - notext.astype(np.int32)).max(axis=2)
           > args.ink_threshold)

    probe = json.load(open(args.rects))
    box = np.zeros((h, w), dtype=bool)
    for r in probe.get("textRects", []):
        x0 = max(0, int(r["x"])); y0 = max(0, int(r["y"]))
        x1 = min(w, int(r["x"]) + int(r["w"])); y1 = min(h, int(r["y"]) + int(r["h"]))
        if x1 > x0 and y1 > y0:
            box[y0:y1, x0:x1] = True

    total_mass = float(err.sum())
    report = {
        "ref": args.ref, "notext": args.notext, "fork": args.fork,
        "size": [w, h],
        "whole": stats(err),
        "text_ink": stats(err, ink),
        "nontext_ink": stats(err, ~ink),
        "text_box": stats(err, box),
        "nontext_box": stats(err, ~box),
    }
    report["text_ink"]["mass_share_pct"] = round(ink.sum() and
        (err[ink].sum() / total_mass * 100) or 0.0, 4)
    report["text_box"]["mass_share_pct"] = round(box.sum() and
        (err[box].sum() / total_mass * 100) or 0.0, 4)
    report["nontext_ink"]["mass_share_pct"] = round(
        err[~ink].sum() / total_mass * 100, 4)
    report["nontext_box"]["mass_share_pct"] = round(
        err[~box].sum() / total_mass * 100, 4)

    # ---- regions -----------------------------------------------------------
    regions = {
        "topbar_y0_60": np.zeros((h, w), bool),
        "banner_y60_110": np.zeros((h, w), bool),
        "body_y110_1000": np.zeros((h, w), bool),
        "canvas_x_lt_1240": np.zeros((h, w), bool),
        "sidebar_x_ge_1240": np.zeros((h, w), bool),
    }
    regions["topbar_y0_60"][0:60, :] = True
    regions["banner_y60_110"][60:110, :] = True
    regions["body_y110_1000"][110:h, :] = True
    regions["canvas_x_lt_1240"][:, 0:1240] = True
    regions["sidebar_x_ge_1240"][:, 1240:w] = True
    rep_regions = {}
    for name, m in regions.items():
        s = stats(err, m)
        # how much of the region's own error mass is text ink
        rm = float(err[m].sum())
        im = float(err[m & ink].sum())
        s["own_error_from_text_ink_pct"] = round(im / rm * 100, 2) if rm else 0.0
        rep_regions[name] = s
    report["regions"] = rep_regions

    # ---- the canvas: the one surface both renderers draw -------------------
    cy, cx = 60, 1240
    canvas = np.zeros((h, w), bool)
    canvas[cy:h, 0:cx] = True
    cm = stats(err, canvas)
    cm["ink"] = stats(err, canvas & ink)
    cm["box"] = stats(err, canvas & box)
    cm["nonink"] = stats(err, canvas & ~ink)
    report["canvas_both_draw"] = cm

    with open(os.path.join(args.out, "textshare.json"), "w") as f:
        json.dump(report, f, indent=2)
    write_mask_png(os.path.join(args.out, "ink-mask.png"), ink, ref)
    write_mask_png(os.path.join(args.out, "box-mask.png"), box, ref)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    sys.exit(main())
