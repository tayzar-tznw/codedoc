"""Check every probe site's `expect` against the runtime trace (lib/trace records).

usage: oracle_check.py PROBE_ROOT SITES_PY TRACE.jsonl [TRACE2.jsonl ...]
Prints one line per site: confirmed / mismatch / not-executed / n/a (oracle != trace).
"""
import importlib.util, json, os, re, sys

def norm(q):
    return re.sub(r"\[[^\]]*\]", "", q)

root, sites_py, traces = sys.argv[1], sys.argv[2], sys.argv[3:]
spec = importlib.util.spec_from_file_location("p", sites_py); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
markers = {}
for dp, _, fns in os.walk(root):
    for fn in fns:
        if fn.endswith(".go"):
            p = os.path.join(dp, fn)
            for i, line in enumerate(open(p, encoding="utf-8"), 1):
                for mm in re.finditer(r"//@([A-Z]{2}\d\d\w*)", line):
                    markers[mm.group(1)] = (os.path.realpath(p), i)
recs = []
for t in traces:
    recs += [json.loads(l) for l in open(t)]
# funclit runtime names -> source line (from callee_line)
def callee_key(r):
    c = norm(r["callee"])
    if re.search(r"\.func\d+$", c) or "-range" in c:
        return f"{c.rsplit('.', 1)[0] if '-range' not in c else c}@{os.path.basename(r['callee_file'])}:{r['callee_line']}"
    return c
summary = {}
for s in m.SITES:
    if s.get("oracle") != "trace":
        print(f"{s['id']:6} n/a ({s.get('oracle')})"); summary.setdefault("n/a", 0); summary["n/a"] += 1; continue
    f, line = markers[s.get("marker", s["id"])]
    hits = [r for r in recs if os.path.realpath(r["caller_file"]) == f and r["caller_line"] == line]
    note = ""
    if not hits and s["id"] in ("FV08", "FV09"):   # deferred: runs at function exit, attributed to the closing line
        hits = [r for r in recs if os.path.realpath(r["caller_file"]) == f and r["caller_func"] == "main.funcValues" and r["caller_line"] > line]
        note = " (deferred: trace line = function end)"
    if not hits and s["id"] in ("FV10", "FV11"):   # goroutine: caller is runtime.goexit
        hits = [r for r in recs if r["caller_func"] == "runtime.goexit"]
        note = " (goroutine: caller runtime.goexit)"
    got = sorted({norm(r["callee"]) + (f"@{os.path.basename(r['callee_file'])}:{r['callee_line']}" if re.search(r"(func\d+|-range\d+)$", r["callee"]) else "") for r in hits})
    exp = s["expect"]
    if not hits:
        status = "not-executed"
    else:
        ok = []
        for e in exp:
            if e.startswith("loopbody@"):
                ok.append(any("-range" in g for g in got))
            elif ".func@" in e:
                ln = e.rsplit("@", 1)[1]
                ok.append(any(g.endswith(":" + ln) for g in got))
            elif e == "none":
                ok.append(False)
            else:
                ok.append(norm(e) in [g.split("@")[0] for g in got])
        if s["id"] in ("FV10", "FV11"):
            status = "confirmed" if any(ok) else "mismatch"
        else:
            status = "confirmed" if all(ok) and len(got) == len(exp) else ("partial" if any(ok) else "mismatch")
        if exp == ["none"]:
            status = "mismatch"
    summary[status] = summary.get(status, 0) + 1
    print(f"{s['id']:6} {status:12} expect={exp} got={got}{note}")
print("SUMMARY", summary)
