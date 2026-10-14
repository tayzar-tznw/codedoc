"""Summarise a go list -json stream. Usage: listsummary.py LIST.json REPO_ROOT"""
import json, sys, collections, re
s = open(sys.argv[1]).read(); root = sys.argv[2].rstrip('/') + '/'
dec = json.JSONDecoder(); i = 0; pk = []
while True:
    while i < len(s) and s[i].isspace(): i += 1
    if i >= len(s): break
    o, i = dec.raw_decode(s, i); pk.append(o)
def inrepo(p): return (p.get("Dir") or "").startswith(root) or (p.get("Dir") or "") + '/' == root
uniq = {p["ImportPath"]: p for p in pk}
ir = [p for p in uniq.values() if inrepo(p)]
ext = [p for p in uniq.values() if not inrepo(p) and not p.get("Standard")]
std = [p for p in uniq.values() if p.get("Standard")]
def kind(e):
    m = e.get("Err", "")
    for k in ("module lookup disabled by GOPROXY=off", "missing go.sum entry", "no required module provides", "cannot find module providing", "build constraints exclude", "no Go files", "import cycle", "is not in std", "not in GOROOT", "use of internal package"):
        if k in m: return k
    return m[:90]
c = collections.Counter(kind(p["Error"]) for p in uniq.values() if p.get("Error"))
print(f"unique packages={len(uniq)} in-repo={len(ir)} external={len(ext)} std={len(std)}")
print(f"in-repo with Error={sum(1 for p in ir if p.get('Error'))} in-repo Incomplete={sum(1 for p in ir if p.get('Incomplete'))} in-repo with DepsErrors={sum(1 for p in ir if p.get('DepsErrors'))}")
print(f"in-repo with GoFiles={sum(1 for p in ir if p.get('GoFiles'))}  external with Error={sum(1 for p in ext if p.get('Error'))}")
print("error kinds:", dict(c.most_common(8)))
for p in [p for p in ir if p.get('Error')][:3]: print("  in-repo err:", p['ImportPath'], p['Error'].get('Err','')[:160])
