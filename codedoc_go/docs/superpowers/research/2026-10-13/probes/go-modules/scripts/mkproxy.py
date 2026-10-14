"""Build a file:// GOPROXY tree from a spec: mkproxy.py OUTDIR
Each module version: go.mod + one package file. Pure stdlib, writes only under OUTDIR."""
import sys, os, json, zipfile, io
out = sys.argv[1]
SPEC = {
  # module path: {version: {relpath: content}}
  "example.com/dep": {
     v: {"go.mod": "module example.com/dep\n\ngo 1.21\n",
         "dep.go": f'package dep\n\nfunc Version() string {{ return "{v}" }}\n',
         "sub/sub.go": f'package sub\n\nfunc Sub() string {{ return "{v}" }}\n'}
     for v in ("v1.0.0", "v1.1.0", "v1.2.0")},
  "example.com/dep/v2": {
     "v2.0.0": {"go.mod": "module example.com/dep/v2\n\ngo 1.21\n",
                "dep.go": 'package dep\n\nfunc Version() string { return "v2.0.0" }\n'}},
  "example.com/Upper": {
     "v1.0.0": {"go.mod": "module example.com/Upper\n\ngo 1.21\n",
                "u.go": 'package upper\n\nfunc U() {}\n'}},
  "example.com/newgo": {
     "v1.0.0": {"go.mod": "module example.com/newgo\n\ngo 1.28\n",
                "n.go": 'package newgo\n\nfunc N() {}\n'}},
  "example.com/nested": {
     "v1.0.0": {"go.mod": "module example.com/nested\n\ngo 1.21\n",
                "n.go": 'package nested\n\nfunc N() {}\n'}},
  "example.com/nested/inner": {
     "v1.0.0": {"go.mod": "module example.com/nested/inner\n\ngo 1.21\n",
                "i.go": 'package inner\n\nfunc I() {}\n'}},
}
def esc(p):  # module path case-encoding
    return "".join("!" + c.lower() if c.isupper() else c for c in p)
for mod, versions in SPEC.items():
    d = os.path.join(out, esc(mod), "@v"); os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "list"), "w") as f: f.write("\n".join(sorted(versions)) + "\n")
    for v, files in versions.items():
        with open(os.path.join(d, v + ".info"), "w") as f: json.dump({"Version": v, "Time": "2026-01-01T00:00:00Z"}, f)
        with open(os.path.join(d, v + ".mod"), "w") as f: f.write(files["go.mod"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for rel, content in files.items():
                z.writestr(f"{mod}@{v}/{rel}", content)
        with open(os.path.join(d, v + ".zip"), "wb") as f: f.write(buf.getvalue())
print("ok", out)
