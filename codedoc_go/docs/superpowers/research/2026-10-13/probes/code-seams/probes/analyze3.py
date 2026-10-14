"""Share of selector calls whose operand is a file-level import name (package-qualified calls).

The import name is the alias, else the imported package's clause name when the package is in
the repo (from goextract records), else the last path element without a /vN suffix (a guess,
counted separately). Local variables shadowing an import name are not detected (upper bound).
"""
import json, os, re, sys
from collections import Counter
root = os.path.realpath(sys.argv[1]); mods = {}
recs = [json.loads(l) for l in open(sys.argv[2])]
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != ".git"]
    if "go.mod" in filenames:
        m = re.search(r"^\s*module\s+(\S+)", open(os.path.join(dirpath, "go.mod")).read(), re.M)
        mods[os.path.relpath(dirpath, root) if dirpath != root else ""] = m.group(1)
def ipath(reldir):
    best = max((m for m in mods if m == "" or reldir == m or reldir.startswith(m + "/")), key=len)
    rest = reldir[len(best):].lstrip("/") if best else reldir
    return mods[best] + ("/" + rest if rest else "")
pkgname = {}
for r in recs:
    d = os.path.dirname(os.path.relpath(r["path"], root))
    if not r["pkg"].endswith("_test") and r["pkg"] != "main":
        pkgname.setdefault(ipath(d), r["pkg"])
c = Counter()
for r in recs:
    names = {}
    for im in r["imports"]:
        if im.get("name") in ("_", "."):
            continue
        if im.get("name"):
            names[im["name"]] = "alias"
        elif im["path"] in pkgname:
            names[pkgname[im["path"]]] = "in-repo"
        else:
            parts = im["path"].split("/")
            last = parts[-1]
            if re.fullmatch(r"v\d+", last) and len(parts) > 1:
                last = parts[-2]
            names[last.replace("-", "_").replace(".", "_")] = "guess"
    for call in r["calls"]:
        if call["shape"] not in ("ident", "selector"):
            continue
        c["token_calls"] += 1
        if call["shape"] == "ident":
            c["ident_calls"] += 1
            continue
        c["selector_calls"] += 1
        how = names.get(call.get("recv", ""))
        if how:
            c[f"pkg_qualified:{how}"] += 1
for k in sorted(c): print(f"{k}: {c[k]}")
