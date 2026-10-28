#!/usr/bin/env python3
"""make-dense-corpus.py — write the 18-namespace / 90-node A/B corpus.

    tools/make-dense-corpus.py /tmp/fid-dense

The default `ab-corpus` is 2 files / 9 nodes, which makes the canvas ~96% flat.
This one gives a real graph (18 namespaces, 18 classes, 54 methods, 84 edges) so
canvas-structural MAE is measured on ink, not background. It is deterministic.
"""
import os
import sys

ROOTS = ["Core", "Net", "IO", "Auth", "Data", "Ui"]


def main() -> int:
    dest = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fid-dense"
    src = os.path.join(dest, "src")
    os.makedirs(src, exist_ok=True)
    n = 0
    for root in ROOTS:
        for sub in ("", "Sub", "Deep"):
            ns = root if not sub else f"{root}.{sub}"
            cls = f"{root}{sub or 'Main'}"
            body = "\n".join(f"    public void M{i}() {{ }}" for i in range(3))
            text = f"namespace {ns} {{\n  public class {cls} {{\n{body}\n  }}\n}}\n"
            with open(os.path.join(src, f"{cls}.cs"), "w") as f:
                f.write(text)
            n += 1
    print(f"wrote {n} namespaces to {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
