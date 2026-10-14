#!/usr/bin/env python3
"""Compare two helper runs per site (download mode vs offline mode).

usage: offline_diff.py ROOT dl/sites.jsonl off/sites.jsonl OUTDIR
Categories per site: same, lost (offline has no target / unknown), changed (both have targets, different),
class-changed (same or no target but class differs), gained.
"""
import collections
import json
import os
import sys

root, dl_p, off_p, outdir = sys.argv[1:5]
os.makedirs(outdir, exist_ok=True)


def load(p):
    d = {}
    for line in open(p):
        s = json.loads(line)
        d[(s["file"], s["call_off"])] = s
    return d


def tkey(s):
    t = s.get("target")
    if not t:
        return None
    p = t.get("pos")
    if p:
        return (p["file"], p["line"], p["col"])
    return ("name", t.get("pkg"), t["name"])


def cls(s):
    return s["class"] + ("/" + s["sub"] if s.get("sub") else "")


_src = {}


def src(f, line):
    if f not in _src:
        _src[f] = open(os.path.join(root, f), encoding="utf-8", errors="replace").read().split("\n")
    return _src[f][line - 1].strip()[:160]


dl, off = load(dl_p), load(off_p)
cat = collections.Counter()
by_cls = collections.defaultdict(collections.Counter)
changes = []
stub_targets = 0
for k, a in dl.items():
    b = off.get(k)
    if b is None:
        cat["missing-in-offline"] += 1
        continue
    ka, kb = tkey(a), tkey(b)
    if b.get("target", {}) and (b.get("target") or {}).get("loc") == "missing":
        stub_targets += 1
    if ka == kb and a["class"] == b["class"]:
        c = "same"
    elif ka is not None and (kb is None or b["class"] == "unknown"):
        c = "lost"
    elif ka is None and kb is not None:
        c = "gained"
    elif ka is not None and kb is not None and ka != kb:
        c = "changed"
    else:
        c = "class-changed"
    cat[c] += 1
    by_cls[cls(a)][c] += 1
    if c in ("changed", "gained", "class-changed"):
        changes.append({"cat": c, "file": a["file"], "line": a["line"], "col": a["col"], "src": src(a["file"], a["line"]),
                        "dl": {"class": cls(a), "target": a.get("target")},
                        "off": {"class": cls(b), "target": b.get("target"), "note": b.get("note")}})
# sites whose offline target object lives in a stub (failed) package, whatever their category
stub = [dict(file=b["file"], line=b["line"], cat=("same-pos" if tkey(dl[k]) == tkey(b) else "diff-pos"),
             off_target=b.get("target"), dl_target=dl[k].get("target"))
        for k, b in off.items() if (b.get("target") or {}).get("loc") == "missing" and k in dl]
out = {"total": len(dl), "categories": dict(cat),
       "by_download_class": {k: dict(v) for k, v in sorted(by_cls.items())},
       "offline_targets_in_stub_packages": len(stub),
       "offline_targets_in_stub_packages_by_cat": dict(collections.Counter(s["cat"] for s in stub))}
json.dump(out, open(os.path.join(outdir, "offline_diff.json"), "w"), indent=2)
with open(os.path.join(outdir, "offline_changes.jsonl"), "w") as f:
    for r in changes:
        f.write(json.dumps(r) + "\n")
with open(os.path.join(outdir, "offline_stub_targets.jsonl"), "w") as f:
    for r in stub:
        f.write(json.dumps(r) + "\n")
print(json.dumps({"total": out["total"], "categories": out["categories"],
                  "stub_targets": out["offline_targets_in_stub_packages_by_cat"]}))
