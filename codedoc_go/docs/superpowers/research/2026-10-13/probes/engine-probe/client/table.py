"""Compact per-site table of all answers from a probe_sites.py result."""
import json, re, sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from summarize import verdict, norm

def q(x):
    if isinstance(x, str):
        return "ERR(" + x[:60] + ")"
    if not x:
        return "[]"
    out = []
    for d in x:
        n = norm(d.get("qname")) or (d.get("text") or "").strip()[:30]
        n = n.replace("example.com/lib/", "").replace("example.com/lib.", "lib.").replace("example.com/app/internal/", "")
        if "/gocache-shared/" in d["path"]:
            n = "GOCACHE:" + n
        out.append(n)
    return "{" + ", ".join(out) + "}"

def hov(h):
    if not h or not isinstance(h, str):
        return str(h)
    m = re.search(r"```go\n(.*?)\n```", h, re.S)
    return (m.group(1) if m else h).split("\n")[0][:90]

r = json.load(open(sys.argv[1]))
ids = set(sys.argv[2].split(",")) if len(sys.argv) > 2 and sys.argv[2] else None
for rec in r["sites"]:
    if ids and rec["id"] not in ids:
        continue
    if "definition" not in rec:
        print(rec["id"], rec.get("error")); continue
    print(f"{rec['id']} [{verdict(rec)}] def={q(rec['definition'])} | tdef={q(rec.get('typeDefinition'))} | hover=`{hov(rec.get('hover'))}`")
    extra = f"   impl@site={q(rec.get('implementation'))}"
    if "implementation_at_decl" in rec: extra += f" impl@decl={q(rec['implementation_at_decl'])}"
    if "implementation_paren" in rec: extra += f" impl@paren={q(rec['implementation_paren'])}"
    oc = rec.get("outgoing")
    extra += f" | outgoing={q(oc) if not isinstance(oc, str) else oc[:50]}"
    refs = rec.get("references")
    if isinstance(refs, list): extra += f" | refs={len(refs)}"
    print(extra)
