"""Extra counts: receiver-owner resolution by same-file twin, dot/aliased/blank imports, cgo, main funcs."""
import json, os, sys
from collections import Counter, defaultdict
root = os.path.realpath(sys.argv[1])
recs = [json.loads(l) for l in open(sys.argv[2], encoding="utf-8")]
rel = lambda p: os.path.relpath(p, root)
types = defaultdict(list)
out = Counter()
for r in recs:
    d, pk = os.path.dirname(rel(r["path"])), r["pkg"]
    for x in r["decls"]:
        if x["k"] in ("type", "alias"):
            types[(d, pk, x["name"])].append(rel(r["path"]))
    for im in r["imports"]:
        n = im.get("name", "")
        out["imports"] += 1
        if n == ".": out["dot_imports"] += 1
        elif n == "_": out["blank_imports"] += 1
        elif n: out["aliased_imports"] += 1
        if im["path"] == "C": out["cgo_files"] += 1
    if pk == "main" and any(x["k"] == "func" and x["name"] == "main" for x in r["decls"]):
        out["func_main"] += 1
for r in recs:
    p = rel(r["path"]); d, pk = os.path.dirname(p), r["pkg"]
    for x in r["decls"]:
        if x["k"] != "method": continue
        files = types.get((d, pk, x["recv_base"]), [])
        if len(files) > 1:
            out["ambiguous_owner"] += 1
            if p in files: out["ambiguous_owner_resolved_same_file"] += 1
            else:
                inc = [f for f in files]
                out["ambiguous_owner_unresolved"] += 1
for k in sorted(out): print(f"{k}: {out[k]}")
