"""Compare gomodparse.parse_mod against `go mod edit -json` for many go.mod files.
Usage: validate_gomod.py LISTFILE OUTDIR   (LISTFILE: one absolute .mod path per line)"""
import json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gomodparse as gp
paths = [l.strip() for l in open(sys.argv[1]) if l.strip()]
env = dict(os.environ, GOWORK="off", GOPROXY="off", GOFLAGS="", GOTOOLCHAIN="local")
agree = disagree = both_err = 0
mism = []
def strip(d):
    d = json.loads(json.dumps(d))
    d.get("Module", {}).pop("Deprecated", None)
    for r in d.get("Retract", []) or []:
        r.pop("Rationale", None)
    return {k: v for k, v in d.items() if v not in (None, [], "")}
for p in paths:
    r = subprocess.run(["go", "mod", "edit", "-json", p], capture_output=True, text=True, env=env, cwd=sys.argv[2])
    go_ok = r.returncode == 0
    try:
        py = gp.parse_mod(open(p, encoding="utf-8", errors="surrogateescape").read(), strict=True); py_ok = True
    except gp.ModError as e:
        py_ok = False; pyerr = str(e)
    if go_ok and py_ok:
        g = strip(json.loads(r.stdout)); q = strip(py)
        if g == q: agree += 1
        else: disagree += 1; mism.append((p, "diff", json.dumps(g)[:300], json.dumps(q)[:300]))
    elif not go_ok and not py_ok:
        both_err += 1
    else:
        disagree += 1; mism.append((p, "go_ok=%s py_ok=%s" % (go_ok, py_ok), r.stderr.strip()[:200], "" if py_ok else pyerr))
print(f"files={len(paths)} identical={agree} both-rejected={both_err} disagreements={disagree}")
for m in mism[:10]: print("  ", m)
