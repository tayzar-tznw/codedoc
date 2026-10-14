#!/usr/bin/env python3
"""Compare two gopls definition runs (download vs offline) per site; classes from the download-mode helper sites.
usage: defs_diff.py ROOT sites.jsonl dl_defs.jsonl off_defs.jsonl OUTDIR"""
import collections, json, os, sys
root, sites_p, a_p, b_p, outdir = sys.argv[1:6]
os.makedirs(outdir, exist_ok=True)
def load(p):
    d = {}
    for l in open(p):
        x = json.loads(l)
        d[(x["file"], x["call_off"])] = None if "error" in x else [(os.path.realpath(l_["path"]), l_["line0"], l_["char16"]) for l_ in (x.get("locs") or [])]
    return d
sites = {}
for l in open(sites_p):
    s = json.loads(l); sites[(s["file"], s["call_off"])] = s
a, b = load(a_p), load(b_p)
cat = collections.Counter(); by = collections.defaultdict(collections.Counter); ch = []
gomodcache_dl = os.path.realpath(os.environ.get("DL_MODCACHE", ""))
for k, la in a.items():
    lb = b.get(k)
    s = sites[k]; c = s["class"] + ("/" + s["sub"] if s.get("sub") else "")
    if la == lb: r = "same"
    elif la and not lb: r = "lost"
    elif lb and not la: r = "gained"
    else: r = "changed"
    cat[r] += 1; by[c][r] += 1
    if r in ("changed", "gained"):
        ch.append({"cat": r, "class": c, "file": k[0], "line": s["line"], "dl": la, "off": lb})
json.dump({"categories": dict(cat), "by_class": {k: dict(v) for k, v in sorted(by.items())}}, open(os.path.join(outdir, "gopls_offline_diff.json"), "w"), indent=2)
with open(os.path.join(outdir, "gopls_offline_changes.jsonl"), "w") as f:
    for r in ch: f.write(json.dumps(r) + "\n")
print(json.dumps(dict(cat)))
