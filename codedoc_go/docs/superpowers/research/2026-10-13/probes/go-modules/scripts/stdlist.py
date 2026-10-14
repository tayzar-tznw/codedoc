"""Compute the std package list from GOROOT/src without running code.
Usage: stdlist.py GOROOT GOOS GOARCH CGO TOOLTAGS -> prints import paths (target-specific) ;
       stdlist.py GOROOT --any -> every dir with a non-_/. .go file (target-independent superset)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gobuildctx as gb
goroot = sys.argv[1]; src = os.path.join(goroot, "src")
anymode = sys.argv[2] == "--any"
if not anymode:
    goos, goarch, cgo, tooltags = sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5]
    ctx = gb.Ctx(goos, goarch, cgo, tool_tags=[t for t in tooltags.split(",") if t])
out = []
for dirpath, dirnames, filenames in os.walk(src):
    dirnames[:] = sorted(d for d in dirnames if not (d.startswith(".") or d.startswith("_") or d == "testdata"
                         or os.path.exists(os.path.join(dirpath, d, "go.mod"))))
    if dirpath == src:
        continue
    rel = os.path.relpath(dirpath, src).replace(os.sep, "/")
    if rel == "builtin":
        continue
    if anymode:
        ok = any(f.endswith(".go") and not f.startswith(("_", ".")) for f in filenames)
    else:
        if rel == "runtime/cgo" and not cgo:
            continue
        cls = gb.classify_dir(dirpath, filenames, ctx)
        ok = any(c in ("GoFiles", "CgoFiles", "TestGoFiles", "XTestGoFiles") for c in cls.values())
    if ok:
        out.append(rel)
print("\n".join(sorted(out)))
