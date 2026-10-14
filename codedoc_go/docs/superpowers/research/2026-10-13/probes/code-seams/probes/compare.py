"""Compare goextract (go/parser) and tsextract (tree-sitter) outputs file by file.

Usage: python3 -I compare.py out/go-<repo>.jsonl out/ts-<repo>.jsonl [--show N]
Prints agreement for package clause, generated header, build lines, imports,
declarations (kind, name, parent, name range, proposed qualname) and call
sites with a callee token (name, range, caller, instantiation flag), split
into files where tree-sitter reported ERROR/MISSING nodes and clean files.
"""
import json
import sys
from collections import Counter, defaultdict


def load(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        out[d["path"]] = d
    return out


def dkey(d):
    return (d["k"], d["name"], d.get("parent", ""), d["line"], d["col"], d["end_col"], d.get("q", ""),
            d.get("recv_base", ""), bool(d.get("ptr")), bool(d.get("generic")))


def ckey(c):
    return (c["name"], c["line"], c["col"], c["end_col"], c["caller"], bool(c.get("inst")))


def main():
    go, tsx = load(sys.argv[1]), load(sys.argv[2])
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 5
    assert set(go) == set(tsx), "file lists differ"
    stats = defaultdict(Counter)
    examples = defaultdict(list)
    shapes = {"go": Counter(), "ts": Counter()}
    a4_loss = Counter()
    for path in sorted(go):
        g, t = go[path], tsx[path]
        bucket = "ts_error_files" if t["nerrors"] else "clean_files"
        s = stats[bucket]
        s["files"] += 1
        s["go_error_files"] += g["nerrors"] > 0
        for field in ("pkg", "pkg_line", "pkg_col", "generated"):
            s[f"{field}_same"] += g.get(field) == t.get(field)
        s["go_build_same"] += (g.get("go_build") or None) == (t.get("go_build") or None)
        s["plus_build_same"] += (g.get("plus_build") or []) == (t.get("plus_build") or [])
        gi = {(i["path"], i.get("name", ""), i["line"], i["col"]) for i in g["imports"]}
        ti = {(i["path"], i.get("name", ""), i["line"], i["col"]) for i in t["imports"]}
        s["imports_go"] += len(gi); s["imports_ts"] += len(ti); s["imports_both"] += len(gi & ti)
        gd = Counter(dkey(d) for d in g["decls"])
        td = Counter(dkey(d) for d in t["decls"])
        s["decls_go"] += sum(gd.values()); s["decls_ts"] += sum(td.values())
        s["decls_both"] += sum((gd & td).values())
        for k in (gd - td):
            examples[f"{bucket}:decl_only_go"].append((path, k))
        for k in (td - gd):
            examples[f"{bucket}:decl_only_ts"].append((path, k))
        for d in g["decls"]:
            s[f"go_decl_{d['k']}"] += 1
        tok_g = [c for c in g["calls"] if c["shape"] in ("ident", "selector")]
        tok_t = [c for c in t["calls"] if c["shape"] in ("ident", "selector")]
        gc, tc = Counter(map(ckey, tok_g)), Counter(map(ckey, tok_t))
        s["calls_go"] += sum(gc.values()); s["calls_ts"] += sum(tc.values())
        s["calls_both"] += sum((gc & tc).values())
        for k in (gc - tc):
            examples[f"{bucket}:call_only_go"].append((path, k))
        for k in (tc - gc):
            examples[f"{bucket}:call_only_ts"].append((path, k))
        for c in g["calls"]:
            shapes["go"][c["shape"]] += 1
        for c in t["calls"]:
            shapes["ts"][c["shape"]] += 1
        if t["nerrors"]:
            first = min(e["line"] for e in t["errors"])
            a4_loss["files"] += 1
            a4_loss["decls_total"] += sum(1 for d in g["decls"] if d["k"] in ("func", "method", "type", "alias"))
            a4_loss["decls_at_or_after_first_error"] += sum(
                1 for d in g["decls"] if d["k"] in ("func", "method", "type", "alias") and d["line"] >= first)
            a4_loss["calls_total"] += len(tok_g)
            a4_loss["calls_at_or_after_first_error"] += sum(1 for c in tok_g if c["line"] >= first)
            if not path.endswith("_test.go"):
                a4_loss["non_test_files"] += 1
    for bucket, s in stats.items():
        print(f"== {bucket}")
        for k in sorted(s):
            print(f"   {k}: {s[k]}")
    print("== call shapes (all calls)")
    for side in ("go", "ts"):
        print(f"   {side}: {dict(shapes[side])}")
    if a4_loss:
        print("== spec A4 error-region loss if tree-sitter errors were treated like codedoc_python")
        for k, v in a4_loss.items():
            print(f"   {k}: {v}")
    for key, items in sorted(examples.items()):
        print(f"-- {key}: {len(items)}")
        for path, k in items[:show]:
            print(f"     {path.split('/repos/')[-1]} {k}")


if __name__ == "__main__":
    main()
