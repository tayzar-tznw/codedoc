#!/usr/bin/env python3
"""Join CHA/VTA callee counts with the helper's per-site implementer declarations (non-test files only).
usage: cg_compare.py helper_out_dir cg.jsonl"""
import collections, json, sys
d, cgp = sys.argv[1:3]
ifaces = {}
for l in open(f"{d}/ifaces.jsonl"):
    r = json.loads(l); ifaces[r["key"]] = r
sites = {}
for l in open(f"{d}/sites.jsonl"):
    s = json.loads(l)
    if s["class"] == "iface" and not s.get("test_file"):
        sites[(s["file"], s["call_off"])] = s
def bucket(n): return "0" if n == 0 else "1" if n == 1 else "2-5" if n <= 5 else ">5"
dist = {k: collections.Counter() for k in ("helper_all_nontest", "helper_repo_nontest", "cha_all", "cha_repo", "vta_all", "vta_repo")}
rel = collections.Counter(); joined = 0; missing = 0
ex = []
for l in open(cgp):
    c = json.loads(l)
    s = sites.get((c["file"], c["call_off"]))
    if s is None:
        missing += 1; continue
    joined += 1
    m = s["target"]["name"].split(".")[-1]
    decls = (ifaces[s["iface"]].get("method_decls") or {}).get(m, {})
    nt = {k: v for k, v in decls.items() if not v.get("test_file")}
    h_all = len(nt); h_repo = sum(1 for v in nt.values() if v["cls"] == "repo")
    for k, v in (("helper_all_nontest", h_all), ("helper_repo_nontest", h_repo), ("cha_all", c["cha"]), ("cha_repo", c["cha_repo"]), ("vta_all", c["vta"]), ("vta_repo", c["vta_repo"])):
        dist[k][bucket(v)] += 1
    rel["vta<=cha"] += c["vta"] <= c["cha"]
    rel["vta_repo<helper_repo"] += c["vta_repo"] < h_repo
    rel["vta_repo==helper_repo"] += c["vta_repo"] == h_repo
    rel["vta_repo>helper_repo"] += c["vta_repo"] > h_repo
    rel["cha_repo>=helper_repo"] += c["cha_repo"] >= h_repo
    rel["vta_zero"] += c["vta"] == 0
    if c["vta_repo"] > h_repo and len(ex) < 5:
        ex.append((s["file"], s["line"], s["iface"], m, h_repo, c["vta_repo"], c["cha_repo"]))
print(json.dumps({"joined_nontest_invoke_sites": joined, "cg_sites_not_in_helper_iface_nontest": missing,
                  "dist": {k: dict(v) for k, v in dist.items()}, "relations": dict(rel), "examples_vta_gt_helper": ex}, indent=1))
