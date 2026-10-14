"""Go concept mapping and qualname/ID collision analysis over goextract output.

Usage: python3 -I analyze.py <repo-root> out/go-<repo>.jsonl
Measures, from files only (go.mod + goextract JSON):
  - modules (deepest go.mod owns a file) and import paths;
  - package identity: package clause vs last path element, package main,
    external test packages, directories with several non-test package names;
  - proposed qualname collisions inside one file (the ID scheme includes the
    relpath), init and blank functions, build-tag twins, methods declared
    outside their receiver type's file, ambiguous receiver owners;
  - closures, package-level initializer closures, callers;
  - files excluded for linux/amd64 and the call sites in them;
  - generated files and their share of call sites;
  - positions whose UTF-16 column differs from the byte column.
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.M)
MAJOR = re.compile(r"^v\d+$")


def main():
    root = os.path.realpath(sys.argv[1])
    recs = [json.loads(line) for line in open(sys.argv[2], encoding="utf-8")]
    rel = lambda p: os.path.relpath(p, root).replace(os.sep, "/")
    # modules
    mods = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if "go.mod" in filenames:
            text = open(os.path.join(dirpath, "go.mod"), encoding="utf-8").read()
            m = MODULE_RE.search(text)
            mods[rel(dirpath) if dirpath != root else ""] = m.group(1).strip('"') if m else None
    def owner(relpath):
        d = os.path.dirname(relpath)
        best = None
        for mdir in mods:
            if mdir == "" or d == mdir or d.startswith(mdir + "/"):
                if best is None or len(mdir) > len(best):
                    best = mdir
        return best
    out = Counter()
    ex = defaultdict(list)
    pkgs = defaultdict(lambda: defaultdict(list))   # dir -> pkg name -> files
    by_pkg_q = defaultdict(list)                     # (dir, pkgname, q) -> files
    type_decl = defaultdict(list)                    # (dir, pkgname, type) -> files
    methods = []                                     # (dir, pkgname, base, file, q)
    sites_by_file = {}
    for r in recs:
        p = rel(r["path"])
        d = os.path.dirname(p)
        pk = r["pkg"]
        pkgs[d][pk].append(p)
        out["files"] += 1
        calls = [c for c in r["calls"] if c["shape"] in ("ident", "selector")]
        sites_by_file[p] = len(calls)
        out["call_sites_token"] += len(calls)
        out["call_sites_all"] += len(r["calls"])
        if r.get("generated"):
            out["generated_files"] += 1
            out["generated_call_sites"] += len(calls)
            out["generated_bytes"] += r["bytes"]
        out["bytes"] += r["bytes"]
        if r.get("match_linux_amd64") is False:
            out["excluded_linux_amd64_files"] += 1
            out["excluded_linux_amd64_call_sites"] += len(calls)
            ex["excluded"].append((p, r.get("go_build")))
        if r.get("go_build"):
            out["go_build_files"] += 1
        if r.get("plus_build"):
            out["plus_build_files"] += 1
            if not r.get("go_build"):
                out["plus_build_only_files"] += 1
        out["funclits"] += r["funclits"]
        out["pkgvar_funclits"] += r["pkgvar_funclits"]
        out["local_types"] += r.get("local_types", 0)
        out["init_funcs"] += r["inits"]
        if r["inits"] > 1:
            out["files_with_2plus_init"] += 1
            ex["multi_init"].append((p, r["inits"]))
        out["blank_funcs"] += r["blank_funcs"]
        if r["blank_funcs"] > 1:
            out["files_with_2plus_blank_funcs"] += 1
        q_in_file = Counter()
        for dd in r["decls"]:
            k = dd["k"]
            out[f"decl_{k}"] += 1
            if dd.get("generic"):
                out[f"generic_{k}"] += 1
            if k in ("func", "method", "type", "alias", "imethod"):
                q_in_file[(k if k in ("type", "alias") else "member", dd["q"])] += 1
            if k in ("func", "method"):
                by_pkg_q[(d, pk, dd["q"])].append(p)
            if k in ("type", "alias"):
                type_decl[(d, pk, dd["name"])].append(p)
            if k == "method":
                methods.append((d, pk, dd["recv_base"], p, dd["q"]))
                if dd.get("ptr"):
                    out["methods_ptr_receiver"] += 1
            if k == "embed":
                out["struct_embeds"] += 1
            if k == "iembed":
                out["interface_embeds"] += 1
        # naive grammar: init and _ without ordinal
        naive = Counter((k, q.split("#")[0] if q.startswith(("init#", "_#")) or "._#" in q else q)
                        for (k, q), n in q_in_file.items() for _ in range(n))
        dup = {kq: n for kq, n in q_in_file.items() if n > 1}
        if dup:
            out["files_with_dup_qualname_proposed"] += 1
            ex["dup_proposed"].append((p, dup))
        if any(n > 1 for n in naive.values()):
            out["files_with_dup_qualname_naive"] += 1
        for c in calls:
            if ".func" in c["caller"]:
                out["call_sites_in_closures"] += 1
            if c["caller"].startswith("<init>"):
                out["call_sites_in_pkgvar_init"] += 1
            if c["caller"].startswith("init#"):
                out["call_sites_in_init_funcs"] += 1
            if c.get("inst"):
                out["call_sites_index_or_inst"] += 1
    # package identity
    for d, names in pkgs.items():
        base = [n for n in names if not n.endswith("_test")]
        tests = [n for n in names if n.endswith("_test")]
        out["dirs"] += 1
        if tests:
            out["dirs_with_xtest_pkg"] += 1
        if len(base) > 1:
            out["dirs_with_2plus_nontest_pkgnames"] += 1
            ex["multi_pkg_dirs"].append((d, {n: len(f) for n, f in names.items()}))
        if "main" in base:
            out["dirs_main"] += 1
        mdir = owner(d + "/x.go" if d else "x.go")
        mpath = mods.get(mdir)
        reldir = d[len(mdir):].lstrip("/") if mdir else d
        ipath = (mpath + ("/" + reldir if reldir else "")) if mpath else None
        last = ipath.rsplit("/", 1)[-1] if ipath else os.path.basename(d)
        for n in base:
            if n == "main":
                continue
            if n != last:
                out["pkgname_ne_last_elem"] += 1
                kind = "major-version suffix" if MAJOR.match(last) else ("dash/dot in dir" if re.search(r"[-.]", last) else "other")
                out[f"pkgname_ne_last_elem:{kind}"] += 1
                ex["pkgname_ne_last"].append((ipath, n))
    # build-tag twins: same (dir, pkg, qualname) declared in >= 2 files
    for (d, pk, q), files in by_pkg_q.items():
        if len(files) > 1 and not q.startswith(("init#", "_#")) and "._#" not in q:
            out["twin_qualnames"] += 1
            ex["twins"].append((d, pk, q, sorted(files)))
    for (d, pk, t), files in type_decl.items():
        if len(files) > 1:
            out["twin_types"] += 1
            ex["twin_types"].append((d, pk, t, sorted(files)))
    # methods vs receiver type file
    for d, pk, base, p, q in methods:
        files = type_decl.get((d, pk, base), [])
        if not files:
            out["methods_receiver_type_not_found"] += 1
            ex["recv_not_found"].append((p, q))
        elif len(files) > 1:
            out["methods_receiver_type_ambiguous"] += 1
        elif files[0] != p:
            out["methods_in_other_file_than_type"] += 1
        out["methods"] += 1
    # UTF-16 vs byte columns at call sites and declaration names
    lines_cache = {}
    nonascii_lines = 0
    diff_sites = 0
    for r in recs:
        with open(r["path"], "rb") as f:
            data = f.read()
        lines = data.split(b"\n")
        nonascii_lines += sum(1 for ln in lines if not ln.isascii())
        for c in r["calls"]:
            if c["shape"] not in ("ident", "selector"):
                continue
            ln = lines[c["line"] - 1]
            if not ln.isascii():
                prefix = ln[:c["col"]]
                if len(prefix.decode("utf-8", "replace").encode("utf-16-le")) // 2 != c["col"]:
                    diff_sites += 1
    out["lines_with_nonascii"] = nonascii_lines
    out["call_sites_utf16_ne_byte_col"] = diff_sites
    out["modules"] = len(mods)
    print(json.dumps({"root": root, "modules": mods}, indent=1))
    for k in sorted(out):
        print(f"{k}: {out[k]}")
    for k, items in ex.items():
        print(f"-- {k}: {len(items)}")
        for it in items[:8]:
            print("    ", it)


if __name__ == "__main__":
    main()
