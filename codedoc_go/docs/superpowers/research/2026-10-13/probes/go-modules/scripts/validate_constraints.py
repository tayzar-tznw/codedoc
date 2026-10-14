"""Compare gobuildctx.py against `go list -e -json` file classification.

Usage: validate_constraints.py CORPUS_ROOT GOLIST_JSON GOOS GOARCH CGO TOOLTAGS(comma) BUILDTAGS(comma) MODE
MODE = module (./... rules: skip vendor, testdata, _x, .x, nested modules, go.mod ignore) | std | cmd
Writes nothing; prints a summary and up to 15 mismatches.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gobuildctx as gb  # noqa: E402

root, golist, goos, goarch, cgo, tooltags, buildtags, mode = sys.argv[1:9]
ctx = gb.Ctx(goos, goarch, cgo == "1", tool_tags=[t for t in tooltags.split(",") if t],
             build_tags=[t for t in buildtags.split(",") if t])
CATS = ["GoFiles", "CgoFiles", "TestGoFiles", "XTestGoFiles", "IgnoredGoFiles", "InvalidGoFiles"]

# --- go list side
s = open(golist).read()
dec = json.JSONDecoder()
i = 0
golist_dirs = {}
while True:
    while i < len(s) and s[i].isspace():
        i += 1
    if i >= len(s):
        break
    o, i = dec.raw_decode(s, i)
    d = o.get("Dir")
    if not d:
        continue
    m = {}
    for c in CATS:
        for f in o.get(c) or []:
            m[f] = c
    golist_dirs[os.path.realpath(d)] = m


def ignore_dirs(modroot):
    """go.mod ignore directive entries (./x relative to root; bare names at any depth)."""
    rel, anywhere = set(), set()
    try:
        txt = open(os.path.join(modroot, "go.mod")).read()
    except OSError:
        return rel, anywhere
    inblock = False
    for line in txt.splitlines():
        line = line.split("//")[0].strip()
        if line.startswith("ignore ("):
            inblock = True
            continue
        if inblock and line == ")":
            inblock = False
            continue
        val = line[len("ignore "):].strip() if line.startswith("ignore ") else (line if inblock else None)
        if not val:
            continue
        val = val.strip('"')
        if val.startswith("./"):
            rel.add(os.path.normpath(os.path.join(modroot, val)))
        else:
            anywhere.add(val.strip("/"))
    return rel, anywhere


# --- python side: walk like ./...
py_dirs = {}
rel_ign, any_ign = ignore_dirs(root)
for dirpath, dirnames, filenames in os.walk(root):
    keep = []
    for d in dirnames:
        full = os.path.join(dirpath, d)
        if d.startswith(".") or d.startswith("_") or d == "testdata":
            continue
        if d == "vendor" and mode == "module":
            continue
        if os.path.exists(os.path.join(full, "go.mod")):
            continue  # nested module
        if os.path.normpath(full) in rel_ign:
            continue
        relp = os.path.relpath(full, root).replace(os.sep, "/")
        if any(relp == a or relp.endswith("/" + a) for a in any_ign):
            continue
        keep.append(d)
    dirnames[:] = keep
    cls = gb.classify_dir(dirpath, filenames, ctx)
    real = {f: c for f, c in cls.items() if c != "skip"}
    included = [f for f, c in real.items() if c in ("GoFiles", "CgoFiles", "TestGoFiles", "XTestGoFiles")]
    if included or any(c == "InvalidGoFiles" for c in real.values()):
        py_dirs[os.path.realpath(dirpath)] = real

# --- compare
files_cmp = 0
mism = []
for d, gm in golist_dirs.items():
    pm = py_dirs.get(d)
    if pm is None:
        if any(c != "IgnoredGoFiles" for c in gm.values()):
            mism.append(("dir-missing-in-python", d, ""))
        continue
    for f in set(gm) | set(pm):
        files_cmp += 1
        if gm.get(f) != pm.get(f):
            mism.append((d, f, f"golist={gm.get(f)} python={pm.get(f)}"))
extra = [d for d in py_dirs if d not in golist_dirs]
print(f"{mode} {goos}/{goarch} cgo={cgo} tags={buildtags or '-'}: golist dirs={len(golist_dirs)} python dirs={len(py_dirs)} "
      f"files compared={files_cmp} mismatches={len(mism)} python-only dirs={len(extra)}")
for m in mism[:15]:
    print("   MISMATCH", m)
for d in extra[:5]:
    print("   PY-ONLY", d)
