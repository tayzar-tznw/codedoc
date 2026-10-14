#!/usr/bin/env python3
"""Compare helper (approach B) targets with gopls (approach A) definition answers, per site.

usage: compare.py ROOT sites.jsonl defs.jsonl OUTDIR [GOROOT]
Writes OUTDIR/compare.json (counts) and OUTDIR/disagreements.jsonl.
"""
import collections
import json
import os
import sys

root, sites_p, defs_p, outdir = sys.argv[1:5]
goroot = sys.argv[5] if len(sys.argv) > 5 else os.environ.get("GOROOT", "")
os.makedirs(outdir, exist_ok=True)

_lines: dict[str, list[str]] = {}


def line_text(path: str, line0: int) -> str:
    if path not in _lines:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                _lines[path] = f.read().split("\n")
        except OSError:
            _lines[path] = []
    ls = _lines[path]
    return ls[line0] if 0 <= line0 < len(ls) else ""


def utf16_slice(s: str, start: int, end: int | None = None) -> str:
    # s is a str; convert utf-16 offsets to python indices
    b = s.encode("utf-16-le")
    return b[start * 2:(end * 2 if end is not None else None)].decode("utf-16-le", errors="replace")


def ident_at(loc: dict) -> str:
    t = line_text(loc["path"], loc["line0"])
    if loc["end_line0"] == loc["line0"]:
        return utf16_slice(t, loc["char16"], loc["end_char16"])
    return utf16_slice(t, loc["char16"])[:40]


def b_target_key(s: dict):
    t = s.get("target")
    if not t:
        return None
    p = t.get("pos")
    if p:
        path = p["file"] if p["file"].startswith("/") else os.path.join(root, p["file"])
        return ("pos", os.path.realpath(path), p["line"] - 1, p["char16"])
    # universe / builtin: compare by name
    name = t["name"].split(".")[-1]
    return ("name", name)


def a_keys(locs: list[dict]):
    keys = []
    for l in locs:
        path = os.path.realpath(l["path"])
        keys.append(("pos", path, l["line0"], l["char16"]))
    return keys


def a_is_builtin_file(l: dict) -> bool:
    p = l["path"]
    return p.endswith("/src/builtin/builtin.go") or p.endswith("/src/unsafe/unsafe.go")


sites = {}
for line in open(sites_p):
    s = json.loads(line)
    sites[(s["file"], s["call_off"])] = s

counts = collections.Counter()
by_class = collections.defaultdict(collections.Counter)
dis = []
multi = collections.Counter()
for line in open(defs_p):
    d = json.loads(line)
    s = sites.get((d["file"], d["call_off"]))
    if s is None:
        counts["missing-site"] += 1
        continue
    cls = s["class"] + ("/" + s["sub"] if s.get("sub") else "")
    if "error" in d:
        cat = "A-error"
    else:
        locs = d.get("locs") or []
        if len(locs) > 1:
            multi[cls] += 1
        bk = b_target_key(s)
        if not locs and bk is None:
            cat = "both-none"
        elif not locs:
            cat = "A-empty"
        elif bk is None:
            cat = "B-none"
        else:
            if bk[0] == "pos":
                cat = "same" if bk in a_keys(locs) else "different"
            else:
                # universe/builtin: A must point into builtin.go/unsafe.go at the same name
                ok = any(a_is_builtin_file(l) and ident_at(l) == bk[1] for l in locs)
                cat = "same" if ok else "different"
    counts[cat] += 1
    by_class[cls][cat] += 1
    if cat not in ("same", "both-none"):
        rec = {"cat": cat, "class": cls, "file": s["file"], "line": s["line"], "col": s["col"],
               "src": line_text(os.path.join(root, s["file"]), s["line"] - 1).strip()[:160],
               "B": s.get("target"), "A": d.get("locs"), "A_err": d.get("error"),
               "note": s.get("note"), "fun_node": s.get("fun_node")}
        if d.get("locs"):
            rec["A_ident"] = [ident_at(l) for l in d["locs"]]
            rec["A_line"] = [line_text(l["path"], l["line0"]).strip()[:160] for l in d["locs"]]
        dis.append(rec)

out = {"total": sum(counts.values()), "counts": dict(counts),
       "by_class": {k: dict(v) for k, v in sorted(by_class.items())},
       "multi_location_answers_by_class": dict(multi)}
with open(os.path.join(outdir, "compare.json"), "w") as f:
    json.dump(out, f, indent=2)
with open(os.path.join(outdir, "disagreements.jsonl"), "w") as f:
    for r in dis:
        f.write(json.dumps(r) + "\n")
print(json.dumps({"total": out["total"], "counts": out["counts"]}, indent=None))
