"""Copy the viewer from the package to docs/ for GitHub Pages.

curldb/viewer/ is the source; docs/viewer.html and docs/viewer/ are what
curldb.ai serves. Run after editing the viewer; CI fails when they differ.
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "curldb" / "viewer"
DOCS = ROOT / "docs"


PAIRS = [(SRC / "index.html", DOCS / "viewer.html")] + [
    (SRC / name, DOCS / "viewer" / name) for name in ("app.js", "render.js", "viewer.css")]


def main() -> None:
    if "--check" in sys.argv[1:]:
        stale = [str(dst.relative_to(ROOT)) for src, dst in PAIRS
                 if not dst.exists() or not filecmp.cmp(src, dst, shallow=False)]
        if stale:
            print("docs out of date, run scripts/sync_docs.py: " + ", ".join(stale))
            sys.exit(1)
        print("docs/ matches curldb/viewer/")
        return
    (DOCS / "viewer").mkdir(parents=True, exist_ok=True)
    for src, dst in PAIRS:
        shutil.copyfile(src, dst)
    print("docs/viewer.html and docs/viewer/ synced from curldb/viewer/")


if __name__ == "__main__":
    main()
