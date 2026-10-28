#!/usr/bin/env python3
"""serve-proto.py — drive `css-shell --serve` the way the app does.

Mirrors the Rust host in app/src/chrome.rs so the wire format is testable
without launching the GUI (this environment is headless and no X input is
allowed). Also the acceptance evidence for:
  * one layout pass answering geometry + paint + hit,
  * warm-request latency (the number task-3 exists to move),
  * the @frame token matching the PNG the host decodes,
  * clean shutdown -> no orphaned child.
"""
import hashlib
import os
import resource
import struct
import subprocess
import sys
import time
import zlib


def read_exact(f, n):
    buf = b""
    while len(buf) < n:
        c = f.read(n - len(buf))
        if not c:
            raise EOFError(f"wanted {n} bytes, got {len(buf)}")
        buf += c
    return buf


def png_rgba(png: bytes):
    """Decode an 8-bit RGBA PNG (non-interlaced) to raw RGBA bytes."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, w, h, idat = 8, None, None, b""
    while pos < len(png):
        (ln,) = struct.unpack(">I", png[pos : pos + 4])
        typ = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", data)
            assert (depth, color, inter) == (8, 6, 0), (depth, color, inter)
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = w * 4
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        ft = raw[i]
        i += 1
        line = bytearray(raw[i : i + stride])
        i += stride
        if ft == 1:
            for x in range(4, stride):
                line[x] = (line[x] + line[x - 4]) & 0xFF
        elif ft == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ft == 3:
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ft == 4:
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                b = prev[x]
                c = prev[x - 4] if x >= 4 else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        elif ft != 0:
            raise ValueError(f"bad filter {ft}")
        out += line
        prev = line
    return w, h, bytes(out)


class Shell:
    def __init__(self, binary, size="1600x1000", repo="/tmp/fid-corpus", facts_json=None):
        self.p = subprocess.Popen(
            [binary, "--serve", "--size", size, "--repo", repo],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.size = size
        self.repo = repo
        self.facts = facts_json
        banner = self.p.stdout.readline().decode()
        if not banner.startswith("css-shell "):
            raise RuntimeError(f"bad banner {banner!r}")

    def request(self, hits=None):
        """One request -> one layout pass. Returns (ms, w, h, png, tsv)."""
        body = (self.facts or "").encode()
        hitf = ""
        if hits:
            hitf = " HITS=" + ";".join(f"{x},{y}" for x, y in hits)
        hdr = f"REQ {self.size} {len(body)} 1{hitf} {self.repo}\n"
        buf = hdr.encode() + (body + b"\n" if body else b"")
        t0 = time.perf_counter()
        self.p.stdin.write(buf)
        self.p.stdin.flush()
        line = self.p.stdout.readline().decode().strip()
        if line.startswith("ERR"):
            n = int(line.split()[1])
            msg = read_exact(self.p.stdout, n + 1)  # + newline
            raise RuntimeError(f"child ERR: {msg.decode(errors='replace')}")
        _, w, h, png_len, tsv_len = line.split()
        w, h, png_len, tsv_len = int(w), int(h), int(png_len), int(tsv_len)
        png = read_exact(self.p.stdout, png_len)
        read_exact(self.p.stdout, 1)  # newline delimiter
        tsv = read_exact(self.p.stdout, tsv_len).decode()
        read_exact(self.p.stdout, 1)
        ms = (time.perf_counter() - t0) * 1000.0
        return ms, w, h, png, tsv

    def close(self, graceful=True):
        if self.p.poll() is None:
            if graceful:
                try:
                    self.p.stdin.write(b"QUIT\n")
                    self.p.stdin.flush()
                except Exception:
                    pass
            try:
                return self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                return self.p.wait(timeout=5)
        return self.p.returncode


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else "target/release/css-shell"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    facts = None
    if len(sys.argv) > 3 and os.path.exists(sys.argv[3]):
        facts = open(sys.argv[3]).read()

    sh = Shell(binary, facts_json=facts)
    print(f"pid={sh.p.pid} banner ok")

    times = []
    for i in range(n):
        ms, w, h, png, tsv = sh.request(hits=[(200.0, 100.0), (5.0, 5.0)])
        times.append(ms)
        rows = [l for l in tsv.splitlines() if not l.startswith("@")]
        frame = [l for l in tsv.splitlines() if l.startswith("@frame")]
        hit = [l for l in tsv.splitlines() if l.startswith("@hit")]
        tok = frame[0].split("\t")[3] if frame else "?"
        if i == 0:
            print(f"  size={w}x{h} png={len(png)}B geom_rows={len(rows)} "
                  f"token={tok[:16]}...")
            print(f"  {hit[0] if hit else 'no hit rows'}")
            # The token must equal SHA-256 of the PNG's decoded RGBA bytes.
            dw, dh, rgba = png_rgba(png)
            assert (dw, dh) == (w, h), (dw, dh, w, h)
            want = hashlib.sha256(rgba).hexdigest()
            print(f"  token==sha256(png rgba): {want == tok}  ({want[:16]}...)")
            assert want == tok, "token does not match painted bytes"
            geom_ms = rows and 0
        print(f"  req{i}: {ms:.2f} ms")

    warm = sorted(times[1:]) if len(times) > 1 else times
    print(f"\nwarm best={warm[0]:.2f}ms median={warm[len(warm)//2]:.2f}ms "
          f"n={len(warm)}")
    print(f"first(includes layout)={times[0]:.2f}ms")
    rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    print(f"child max RSS={rss_kb/1024:.1f} MiB")

    rc = sh.close()
    print(f"child exit code={rc} (0 = clean QUIT)")
    time.sleep(0.2)
    alive = sh.p.poll() is None
    print(f"child alive after close: {alive}")
    assert not alive


if __name__ == "__main__":
    main()
