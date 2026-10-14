#!/usr/bin/env python3
"""Interface-method call sites: candidate-implementer distributions from the helper output.
usage: iface_stats.py OUTDIR (with sites.jsonl and ifaces.jsonl)"""
import collections, json, sys
d = sys.argv[1]
ifaces = {}
for l in open(f"{d}/ifaces.jsonl"):
    r = json.loads(l); ifaces[r["key"]] = r
def bucket(n):
    return "0" if n == 0 else "1" if n == 1 else "2-5" if n <= 5 else ">5"
dist = {k: collections.Counter() for k in ("repo_all", "repo_nontest_types", "std", "third", "nonrepo_any")}
by_loc = collections.defaultdict(collections.Counter)
nsites = 0
sites_nonrepo_also = 0
for l in open(f"{d}/sites.jsonl"):
    s = json.loads(l)
    if s["class"] != "iface":
        continue
    nsites += 1
    r = ifaces[s["iface"]]
    impls = r.get("repo_impls") or []
    n_all = len(impls)
    n_nt = sum(1 for i in impls if not i.get("test_only"))
    dist["repo_all"][bucket(n_all)] += 1
    dist["repo_nontest_types"][bucket(n_nt)] += 1
    dist["std"][bucket(r.get("std_impls", 0))] += 1
    dist["third"][bucket(r.get("third_impls", 0))] += 1
    nr = r.get("std_impls", 0) + r.get("third_impls", 0)
    dist["nonrepo_any"]["yes" if nr else "no"] += 1
    by_loc[s.get("sub")][bucket(n_all)] += 1
# per-interface view: in-repo interfaces (declared in a workspace module) with call sites
per_if = collections.Counter()
one_repo_plus_nonrepo = []
for k, r in ifaces.items():
    if r["loc"] not in ("same-module", "other-repo-module"):
        continue
    impls = r.get("repo_impls") or []
    nr = r.get("std_impls", 0) + r.get("third_impls", 0)
    per_if["in_repo_interfaces_with_sites"] += 1
    per_if[f"repo_impls={bucket(len(impls))}"] += 1
    if len(impls) == 1 and nr > 0:
        one_repo_plus_nonrepo.append((k, impls[0]["type"], r.get("std_impls", 0), r.get("third_impls", 0), r["sites"]))
    nt = [i for i in impls if not i.get("test_only")]
    if len(nt) == 1 and nr > 0:
        per_if["one_nontest_repo_impl_and_nonrepo"] += 1
    if len(impls) == 1:
        per_if["exactly_one_repo_impl"] += 1
    if len(impls) >= 1 and all(i.get("test_only") for i in impls):
        per_if["only_test_only_impls"] += 1
    if r.get("generic_undecided"):
        per_if["has_generic_types_matching_names"] += 1
    if r.get("fallbacks"):
        per_if["needed_string_fallback"] += 1
per_if["one_repo_impl_and_nonrepo"] = len(one_repo_plus_nonrepo)
out = {"iface_sites": nsites, "distributions": {k: dict(v) for k, v in dist.items()},
       "repo_all_by_iface_loc": {k: dict(v) for k, v in by_loc.items()},
       "per_interface": dict(per_if), "one_repo_plus_nonrepo_examples": one_repo_plus_nonrepo[:15]}
json.dump(out, open(f"{d}/iface_stats.json", "w"), indent=2)
print(json.dumps(out, indent=1))
