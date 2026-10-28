#!/usr/bin/env python3
"""Chromium box oracle for the nzk_ui chrome.

Builds the *exact* document nzk_ui lays out (from its Rust skeleton), drops
the Blitz-only `compat.css` shim so the page is pure `style.css` + the three
JS-injected sheets, lays it out in headless Chromium (the same engine the saved
v2 reference was captured in) and dumps every element's box.  Also runs
nzk_ui's own `--dump-boxes` and prints the per-element delta.

Usage:
  tools/oracle.py --repo /10tb/TODO/Decompiled_EnncryptedNames \
                  --status "files 18  ·  parse 2,466ms  ·  nodes 668  ·  edges 1,615" \
                  --out evidence/v3launch/r38-lead/oracle

Outputs, under --out:
  blitz-all.tsv     node  tag  id  class  x  y  w  h      (nzk_ui layout)
  chromium-all.tsv  same columns, from the Chromium probe
  diff.tsv          key, blitz box, chromium box, deltas (only mismatched)
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import html as htmlmod

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)              # native-csviewer
CSS_SHELL = os.path.join(ROOT, "nzk_ui")
BIN = os.path.join(CSS_SHELL, "target", "release", "nzk_ui")

PROBE_JS = r"""
<script>
(function(){
  var out = [];
  var els = document.querySelectorAll('*');
  for (var i = 0; i < els.length; i++) {
    var e = els[i];
    var r = e.getBoundingClientRect();
    out.push([e.tagName.toLowerCase(), e.id || '', e.className || '',
              +r.x.toFixed(2), +r.y.toFixed(2), +r.width.toFixed(2), +r.height.toFixed(2)]);
  }
  document.body.insertAdjacentHTML('beforeend',
    '<pre id="dumpjson">' + JSON.stringify(out) + '</pre>');
})();
</script>
</body></html>
"""


def strip_compat(html: str) -> str:
    """Drop the compat <style> element (the Blitz-only engine-gap shim)."""
    out = html
    # <style ...> ... </style> blocks whose text mentions the shim banner
    pat = re.compile(r"<style[^>]*>(?:(?!</style>).)*?nzk_ui engine-gap shim.*?</style>", re.S)
    out, n = pat.subn("", out)
    if n == 0:
        print("oracle: WARNING: no compat <style> block found", file=sys.stderr)
    else:
        print(f"oracle: dropped {n} compat <style> block(s)", file=sys.stderr)
    return out


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


def html_safe(html: str) -> str:
    """Blitz's outer_html serialises EMPTY elements XHTML-style (`<div />`).

    An HTML parser reads `<div />` as a plain start tag, so every following
    sibling would become a child of that empty div and the Chromium oracle tree
    would not match the document nzk_ui laid out. Expand them to explicit
    open+close pairs, and drop the external `<script src=... />` tags (they point
    at /static JS that a file:// probe cannot load and that Blitz does not run).
    """
    html = re.sub(r"<script[^>]*/>", "", html)
    def fix(m):
        tag, attrs = m.group(1), m.group(2)
        if tag.lower() in VOID:
            return m.group(0)
        return f"<{tag}{attrs}></{tag}>"
    return re.sub(r"<([a-zA-Z][\w-]*)([^>]*?)\s*/>", fix, html)


def run(cmd, **kw):
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--status", default="")
    ap.add_argument("--facts", default="", help="production dom::Facts JSON (from CSVIEWER_DUMP_FACTS)")
    ap.add_argument("--size", default="1600x1000")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="csviewer-oracle-")
    try:
        raw_html = os.path.join(tmp, "app.html")
        blitz_all = os.path.join(outdir, "blitz-all.tsv")
        cmd = [BIN, "--repo", args.repo, "--size", args.size,
               "--dump-html", raw_html, "--dump-boxes", blitz_all]
        if args.status:
            cmd += ["--status", args.status]
        if args.facts:
            cmd += ["--facts", os.path.abspath(args.facts)]
        run(cmd, cwd=CSS_SHELL, stdout=subprocess.DEVNULL)

        src = open(raw_html, encoding="utf-8").read()
        page = html_safe(strip_compat(src))
        page = page[: page.rfind("</body>")] + PROBE_JS
        probe = os.path.join(tmp, "probe.html")
        open(probe, "w", encoding="utf-8").write(page)

        # Chromium needs a writable HOME and (apparently) a space-free URL.
        home = os.path.join(tmp, "home")
        os.makedirs(home, exist_ok=True)
        env = dict(os.environ, HOME=home)
        dom = subprocess.run(
            ["chromium", "--headless", "--no-sandbox", "--disable-gpu",
             "--ozone-platform=headless", "--user-data-dir=" + os.path.join(tmp, "chrome"),
             "--virtual-time-budget=8000", "--window-size=" + args.size.replace("x", ","),
             "--dump-dom", "file://" + probe],
            capture_output=True, text=True, env=env, check=True,
        ).stdout
        m = re.findall(r'<pre id="dumpjson">(.*?)</pre>', dom, re.S)
        if not m:
            print("oracle: ERROR: no Chromium dump", file=sys.stderr)
            return 1
        data = __import__("json").loads(htmlmod.unescape(m[-1]))
        chrom = os.path.join(outdir, "chromium-all.tsv")
        with open(chrom, "w") as f:
            f.write("node\ttag\tid\tclass\tx\ty\tw\th\n")
            for i, row in enumerate(data):
                f.write("\t".join([str(i)] + [str(c) for c in row]) + "\n")

        # ---- diff -----------------------------------------------------------
        def load(path):
            rows = []
            with open(path) as f:
                next(f)
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    rows.append((p[1], p[2], p[3],
                                 tuple(float(x) for x in p[4:8])))
            return rows

        bl = load(blitz_all)
        ch = load(chrom)
        # occurrence key: (tag,id,class, nth-with-that-key)
        def index(rows):
            seen = {}
            out = []
            for tag, id_, cls, box in rows:
                k = (tag, id_, cls)
                n = seen.get(k, 0)
                seen[k] = n + 1
                out.append((k + (n,), box))
            return dict(out)
        bi, ci = index(bl), index(ch)
        diffs = []
        for key, b in bi.items():
            c = ci.get(key)
            if c is None:
                continue
            d = tuple(round(c[i] - b[i], 2) for i in range(4))
            if any(abs(x) >= 0.01 for x in d):
                diffs.append((key, b, c, d))
        diff_path = os.path.join(outdir, "diff.tsv")
        with open(diff_path, "w") as f:
            f.write("tag\tid\tclass\tnth\tb_x\tb_y\tb_w\tb_h\t"
                    "c_x\tc_y\tc_w\tc_h\tdx\tdy\tdw\tdh\n")
            for (tag, id_, cls, n), b, c, d in diffs:
                f.write("\t".join([tag, id_, cls, str(n)]
                                  + [f"{v:.2f}" for v in b]
                                  + [f"{v:.2f}" for v in c]
                                  + [f"{v:.2f}" for v in d]) + "\n")
        print(f"oracle: {len(bi)} blitz / {len(ci)} chromium elements, "
              f"{len(diffs)} with box deltas -> {diff_path}", file=sys.stderr)
        # Short summary: biggest |dy| first
        diffs.sort(key=lambda r: -abs(r[3][1]))
        for (tag, id_, cls, n), b, c, d in diffs[:30]:
            print(f"  {tag:8s} {id_ or '-':24s} {cls[:28]:28s} "
                  f"b=({b[0]:.1f},{b[1]:.1f},{b[2]:.1f},{b[3]:.1f}) "
                  f"c=({c[0]:.1f},{c[1]:.1f},{c[2]:.1f},{c[3]:.1f}) "
                  f"d=({d[0]:+.1f},{d[1]:+.1f},{d[2]:+.1f},{d[3]:+.1f})")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
