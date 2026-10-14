"""Summarize a probe_sites.py result: one row per site with an automatic verdict."""
import json, re, sys

def norm(q):
    return re.sub(r"\[[^\]]*\]", "", q or "")

def verdict(rec):
    d = rec.get("definition")
    exp = rec["expect"]
    if isinstance(d, str):
        return "error"
    if not d:
        return "empty"
    qs = [x.get("qname") for x in d]
    kinds = [x.get("kind") for x in d]
    e0 = exp[0]
    if e0.startswith("conv:"):
        return "type(conv)" if all(k == "type" or (x.get("path","").endswith("builtin.go")) for k, x in zip(kinds, d)) else "CHECK"
    if e0.startswith("builtin:"):
        return "builtin" if all("/src/builtin/" in x["path"] or "/src/unsafe/" in x["path"] for x in d) else "CHECK"
    if e0 == "none":
        return "ref-only"
    if len(d) > 1:
        return "multi"
    x = d[0]
    q, k = norm(x.get("qname")), x.get("kind")
    if k in ("func", "method"):
        if any(norm(e) == q for e in exp):
            if rec.get("expect_file") and not x["loc"].startswith(rec["expect_file"]):
                return "WRONG-file"
            if len(exp) > 1:
                return "partial"
            if x.get("has_body") is False:
                return "bodyless"
            return "correct"
        if any(norm(e).endswith(q.split(".", 1)[-1]) for e in exp) and rec.get("alt"):
            return "alt?"
        return "WRONG"
    if k == "ifacemethod":
        return "abstract(iface)"
    if k in ("field", "var", "local", "embedded"):
        return f"value({k})"
    if k == "funclit":
        return "correct" if any(norm(e) == q for e in exp) else "WRONG"
    if k == "type":
        return "type"
    return f"other({k or x.get('text')})"

def short(x):
    if isinstance(x, str):
        return x
    return "; ".join(f"{d['loc']} [{d.get('kind','?')}] {norm(d.get('qname')) or d.get('text','')}" for d in x)

def main():
    r = json.load(open(sys.argv[1]))
    verbose = "-v" in sys.argv
    print(f"# {sys.argv[1]} ready={r['ready_s']}s total={r['total_s']}s rss={r.get('rss_kb')}kB")
    for rec in r["sites"]:
        if "error" in rec and "definition" not in rec:
            print(rec["id"], "ERROR", rec["error"]); continue
        v = verdict(rec)
        print(f"{rec['id']:6} {v:16} {rec['file']}:{rec['line']} `{rec['token']}` -> {short(rec['definition'])}")
        if verbose:
            print(f"        expect={rec['expect']} alt={rec.get('alt')}")
            print(f"        typeDef: {short(rec.get('typeDefinition'))}")
            h = rec.get("hover")
            print(f"        hover: {json.dumps(h)[:300] if h else h}")
            print(f"        impl@site: {short(rec.get('implementation'))}")
            if "implementation_paren" in rec: print(f"        impl@paren: {short(rec['implementation_paren'])}")
            if "implementation_at_decl" in rec: print(f"        impl@decl: {short(rec['implementation_at_decl'])}")
            print(f"        outgoing({rec.get('outgoing_caller')}): {short(rec.get('outgoing')) if not isinstance(rec.get('outgoing'), str) else rec.get('outgoing')}")
            if "references" in rec: print(f"        refs({len(rec['references']) if isinstance(rec['references'], list) else rec['references']}): {rec['references'][:6] if isinstance(rec['references'], list) else ''}")

if __name__ == "__main__":
    main()
