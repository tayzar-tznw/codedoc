#!/usr/bin/env python3
"""Compare gopls textDocument/implementation answers with the helper's types.Implements sets.

usage: impl_compare.py ROOT GOROOT GOMODCACHE ifaces.jsonl sites.jsonl impls.jsonl OUTDIR
"""
import collections
import json
import os
import re
import sys

root, goroot, modcache, ifaces_p, sites_p, impls_p, outdir = sys.argv[1:8]
root = os.path.realpath(root)
os.makedirs(outdir, exist_ok=True)
RECV = re.compile(r"func\s*\(\s*(?:\w+\s+)?\*?\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\)\s*([A-Za-z_]\w*)")
_lines = {}


def line(path, l0):
    if path not in _lines:
        try:
            _lines[path] = open(path, encoding="utf-8", errors="replace").read().split("\n")
        except OSError:
            _lines[path] = []
    ls = _lines[path]
    return ls[l0] if 0 <= l0 < len(ls) else ""


def where(path):
    p = os.path.realpath(path)
    if p.startswith(root + "/"):
        return "repo", os.path.relpath(p, root)
    if p.startswith(os.path.realpath(goroot) + "/"):
        return "stdlib", p
    if p.startswith(os.path.realpath(modcache) + "/"):
        return "third-party", p
    return "other", p


ifaces = {}
for l in open(ifaces_p):
    r = json.loads(l)
    ifaces[r["key"]] = r
sites = {}
for l in open(sites_p):
    s = json.loads(l)
    sites[(s["file"], s["call_off"])] = s

stats = collections.Counter()
diffs = []
for l in open(impls_p):
    d = json.loads(l)
    s = sites[(d["file"], d["call_off"])]
    rec = ifaces.get(s["iface"])
    if "error" in d or rec is None:
        stats["error-or-no-rec"] += 1
        continue
    mname = (s.get("target") or {}).get("name", "").split(".")[-1]
    g_repo, g_std, g_third, g_other, g_abstract = set(), set(), set(), 0, 0
    for loc in d.get("locs") or []:
        w, p = where(loc["path"])
        txt = line(loc["path"], loc["line0"]).lstrip()
        if not txt.startswith("func"):
            g_abstract += 1  # an interface method (gopls also reports related interfaces)
            continue
        key = f"{p}:{loc['line0']}:{loc['char16']}"
        if w == "repo":
            g_repo.add(key)
        elif w == "stdlib":
            g_std.add(key)
        elif w == "third-party":
            g_third.add(key)
        else:
            g_other += 1
    decls = (rec.get("method_decls") or {}).get(mname, {})
    h_repo = {k: v for k, v in decls.items() if v["cls"] == "repo"}
    h_std = {k for k, v in decls.items() if v["cls"] == "stdlib"}
    h_third = {k for k, v in decls.items() if v["cls"] == "third-party"}
    only_g = g_repo - set(h_repo)
    only_h = set(h_repo) - g_repo
    stats["sites"] += 1
    stats["gopls_abstract_locs"] += g_abstract
    stats["sites_with_gopls_abstract_locs"] += 1 if g_abstract else 0
    if not only_g and not only_h:
        stats["repo-decl-sets-equal"] += 1
    if only_g:
        stats["gopls-extra-repo-decls"] += 1
    if only_h:
        stats["helper-extra-repo-decls"] += 1
        if all(h_repo[k].get("test_file") for k in only_h):
            stats["helper-extra-repo-decls-all-test-files"] += 1
    if g_std != h_std:
        stats["std-decl-sets-differ"] += 1
    if g_third != h_third:
        stats["third-decl-sets-differ"] += 1
    stats["gopls_std_decls"] += len(g_std)
    stats["gopls_third_decls"] += len(g_third)
    stats["helper_std_decls"] += len(h_std)
    stats["helper_third_decls"] += len(h_third)
    if any(h_repo[k].get("test_file") for k in g_repo & set(h_repo)):
        stats["gopls-includes-test-file-decls"] += 1
    if any(v.get("test_file") for v in h_repo.values()):
        stats["helper-has-test-file-decls"] += 1
    if only_g or only_h or g_std != h_std or g_third != h_third:
        diffs.append({"file": d["file"], "line": d["line"], "iface": s["iface"], "method": mname,
                      "only_gopls": sorted(only_g), "only_helper": sorted(only_h),
                      "only_helper_info": [h_repo[k] for k in sorted(only_h)],
                      "std_only_gopls": sorted(g_std - h_std)[:5], "std_only_helper": sorted(h_std - g_std)[:5],
                      "third_only_gopls": sorted(g_third - h_third)[:5], "third_only_helper": sorted(h_third - g_third)[:5],
                      "n": [len(g_repo), len(h_repo), len(g_std), len(h_std), len(g_third), len(h_third)]})
json.dump({"stats": dict(stats)}, open(os.path.join(outdir, "impl_compare.json"), "w"), indent=2)
with open(os.path.join(outdir, "impl_diffs.jsonl"), "w") as f:
    for r in diffs:
        f.write(json.dumps(r) + "\n")
print(json.dumps(dict(stats)))
