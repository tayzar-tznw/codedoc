"""Drive gopls over the probe sites and record every answer.

usage: probe_sites.py FOLDER SITES_PY OUT_JSON [--settings JSON] [--env K=V]...
       [--open all|none|sitefiles] [--only ID,ID] [--declidx F.jsonl]...
       [--extra-folder DIR]... [--no-refs] [--no-calls]
"""
from __future__ import annotations

import argparse, asyncio, importlib.util, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start, dump_log, progress_events, rss_kb, hwm_kb
from lsp_client import path_to_uri, uri_to_path, LspError, LspTimeout


def load_sites(path):
    spec = importlib.util.spec_from_file_location("probes", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.SITES


def scan_markers(root):
    markers = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ("vendor",)] if False else dns
        for fn in fns:
            if fn.endswith(".go"):
                p = os.path.join(dp, fn)
                with open(p, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        for m in re.finditer(r"//@([A-Z]{2}\d\d\w*)", line):
                            markers.setdefault(m.group(1), []).append((p, i, line.rstrip("\n")))
    return markers


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


_lines_cache = {}


def file_lines(path):
    if path not in _lines_cache:
        try:
            with open(path, encoding="utf-8") as f:
                _lines_cache[path] = f.read().split("\n")
        except (OSError, UnicodeDecodeError):
            _lines_cache[path] = []
    return _lines_cache[path]


def lsp_to_bytecol(path, line0, ch16):
    lines = file_lines(path)
    if line0 >= len(lines):
        return None
    text = lines[line0]
    units = 0
    for idx, c in enumerate(text):
        if units >= ch16:
            return len(text[:idx].encode("utf-8")) + 1
        units += 2 if ord(c) > 0xFFFF else 1
    return len(text.encode("utf-8")) + 1


class DeclIndex:
    def __init__(self, paths):
        self.by_pos = {}
        self.funcs = {}
        for p in paths:
            with open(p) as f:
                for l in f:
                    d = json.loads(l)
                    key = (os.path.realpath(d["file"]), d["line"], d["col"])
                    self.by_pos.setdefault(key, d)
                    if d["kind"] in ("func", "method") and d.get("end_line"):
                        self.funcs.setdefault(os.path.realpath(d["file"]), []).append(d)

    def at(self, path, line1, bytecol):
        return self.by_pos.get((os.path.realpath(path), line1, bytecol))

    def enclosing_func(self, path, line1):
        best = None
        for d in self.funcs.get(os.path.realpath(path), []):
            if d["line"] <= line1 <= d["end_line"]:
                if best is None or d["line"] > best["line"]:
                    best = d
        return best


def describe_loc(idx, loc, root):
    uri = loc.get("uri") or loc.get("targetUri")
    rng = loc.get("range") or loc.get("targetSelectionRange")
    path = uri_to_path(uri)
    l0, c16 = rng["start"]["line"], rng["start"]["character"]
    bc = lsp_to_bytecol(path, l0, c16)
    d = idx.at(path, l0 + 1, bc) if bc else None
    rel = os.path.relpath(path, root) if path.startswith(root + "/") else path
    out = {"loc": f"{rel}:{l0 + 1}:{c16 + 1}", "path": path}
    if d:
        out["qname"], out["kind"] = d["qname"], d["kind"]
        if "has_body" in d:
            out["has_body"] = d["has_body"]
    else:
        lines = file_lines(path)
        out["text"] = lines[l0][max(0, c16 - 10):c16 + 30] if l0 < len(lines) else None
    return out


def as_list(res):
    if res is None:
        return []
    if isinstance(res, dict):
        return [res]
    return res


async def req(c, method, params, timeout=30):
    t0 = time.monotonic()
    try:
        r = await c.request(method, params, timeout=timeout)
        return {"ok": True, "result": r, "ms": round((time.monotonic() - t0) * 1000, 1)}
    except LspError as e:
        return {"ok": False, "error": f"{e.code}: {e.message}", "ms": round((time.monotonic() - t0) * 1000, 1)}
    except LspTimeout as e:
        return {"ok": False, "error": f"timeout: {e}"}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder"); ap.add_argument("sites"); ap.add_argument("out")
    ap.add_argument("--settings", default="{}")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--open", default="sitefiles")
    ap.add_argument("--only", default="")
    ap.add_argument("--declidx", action="append", default=[])
    ap.add_argument("--extra-folder", action="append", default=[])
    ap.add_argument("--marker-root", default=None)
    ap.add_argument("--no-refs", action="store_true")
    ap.add_argument("--no-calls", action="store_true")
    ap.add_argument("--settle", type=float, default=0.0)
    ap.add_argument("--init-options", default=None)
    ap.add_argument("--config-null", action="store_true")
    a = ap.parse_args()
    root = os.path.realpath(a.folder)
    settings = json.loads(a.settings)
    env_over = {}
    for kv in a.env:
        k, v = kv.split("=", 1)
        env_over[k] = None if v == "<unset>" else v
    sites = load_sites(a.sites)
    if a.only:
        keep = set(a.only.split(","))
        sites = [s for s in sites if s["id"] in keep]
    markers = scan_markers(a.marker_root or root)
    idx = DeclIndex(a.declidx)
    folders = [root] + [os.path.realpath(f) for f in a.extra_folder]
    c = make_client(root, None if a.config_null else settings, env_over=env_over, folders=folders, stderr_path=a.out + ".stderr",
                    init_options=json.loads(a.init_options) if a.init_options else None)
    t_start = time.monotonic()
    init_res, init_dt = await start(c)
    # readiness: the first "Setting up workspace" progress must end
    begin = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("kind") == "begin"
                                      and p["value"].get("title") == "Setting up workspace", timeout=600)
    tok = begin["token"]
    await c.wait_notification("$/progress", lambda p: p.get("token") == tok and p.get("value", {}).get("kind") == "end",
                              timeout=600)
    t_ready = time.monotonic() - t_start
    result = {"folder": root, "settings": settings, "env": env_over, "init_s": round(init_dt, 3),
              "ready_s": round(t_ready, 3), "sites": []}
    located = []
    for s in sites:
        mk = s.get("marker", s["id"])
        if mk not in markers:
            result["sites"].append({"id": s["id"], "error": "marker not found"}); continue
        path, line1, text = markers[mk][0]
        code = text.split("//@")[0]
        pos, occ = -1, s.get("occ", 1)
        for _ in range(occ):
            pos = code.find(s["token"], pos + 1)
        if s["token"] == "()":
            pos = pos  # the "(" itself
        located.append((s, path, line1, utf16_len(code[:pos]), code, pos))
    opened = set()
    if a.open in ("all", "sitefiles"):
        files = sorted({p for _, p, *_ in located}) if a.open == "sitefiles" else sorted(
            {p for v in markers.values() for p, _, _ in v})
        for p in files:
            with open(p, encoding="utf-8") as f:
                txt = f.read()
            await c.notify("textDocument/didOpen", {"textDocument": {"uri": path_to_uri(p), "languageId": "go",
                                                                      "version": 1, "text": txt}})
            opened.add(p)
    if a.settle:
        await asyncio.sleep(a.settle)
    for s, path, line1, ch16, code, pos in located:
        uri = path_to_uri(path)
        tdp = {"textDocument": {"uri": uri}, "position": {"line": line1 - 1, "character": ch16}}
        rec = {"id": s["id"], "cat": s.get("cat"), "file": os.path.relpath(path, root), "line": line1,
               "col16": ch16 + 1, "token": s["token"], "expect": s["expect"], "alt": s.get("alt", []),
               "expect_file": s.get("expect_file"), "oracle": s.get("oracle"), "note": s.get("note", ""),
               "opened": path in opened}
        r = await req(c, "textDocument/definition", tdp)
        rec["definition"] = [describe_loc(idx, l, root) for l in as_list(r.get("result"))] if r["ok"] else r["error"]
        rec["definition_ms"] = r.get("ms")
        r = await req(c, "textDocument/typeDefinition", tdp)
        rec["typeDefinition"] = [describe_loc(idx, l, root) for l in as_list(r.get("result"))] if r["ok"] else r["error"]
        r = await req(c, "textDocument/hover", tdp)
        if r["ok"]:
            h = r.get("result")
            rec["hover"] = h["contents"]["value"] if h and isinstance(h.get("contents"), dict) else h
        else:
            rec["hover"] = r["error"]
        r = await req(c, "textDocument/implementation", tdp)
        rec["implementation"] = [describe_loc(idx, l, root) for l in as_list(r.get("result"))] if r["ok"] else r["error"]
        # implementation at the "(" of the call (signature-based, v0.19+)
        end = pos + len(s["token"])
        if s["token"] != "()" and end < len(code) and code[end] == "(":
            tdp2 = {"textDocument": {"uri": uri}, "position": {"line": line1 - 1, "character": utf16_len(code[:end])}}
            r = await req(c, "textDocument/implementation", tdp2)
            rec["implementation_paren"] = [describe_loc(idx, l, root) for l in as_list(r.get("result"))] if r["ok"] else r["error"]
        # implementation at the declaration, when definition names an interface method or a func-typed value
        if isinstance(rec["definition"], list) and len(rec["definition"]) == 1:
            d0 = rec["definition"][0]
            if d0.get("kind") in ("ifacemethod", "field", "var", "local", "embedded"):
                dl = d0["loc"].rsplit(":", 2)
                dpath = d0["path"]
                tdp3 = {"textDocument": {"uri": path_to_uri(dpath)},
                        "position": {"line": int(dl[1]) - 1, "character": int(dl[2]) - 1}}
                r = await req(c, "textDocument/implementation", tdp3)
                rec["implementation_at_decl"] = [describe_loc(idx, l, root) for l in as_list(r.get("result"))] if r["ok"] else r["error"]
        if not a.no_refs:
            r = await req(c, "textDocument/references", dict(tdp, context={"includeDeclaration": False}))
            rec["references"] = [describe_loc(idx, l, root)["loc"] for l in as_list(r.get("result"))] if r["ok"] else r["error"]
        if not a.no_calls:
            enc = idx.enclosing_func(path, line1)
            if enc is None:
                rec["outgoing"] = "no enclosing func decl"
            else:
                ep = {"textDocument": {"uri": uri}, "position": {"line": enc["line"] - 1, "character": enc["col"] - 1}}
                r = await req(c, "textDocument/prepareCallHierarchy", ep)
                items = r.get("result") if r["ok"] else None
                if not items:
                    rec["outgoing"] = f"prepare failed: {r.get('error') or r.get('result')}"
                else:
                    r2 = await req(c, "callHierarchy/outgoingCalls", {"item": items[0]})
                    hits = []
                    if r2["ok"]:
                        for oc in r2.get("result") or []:
                            for fr in oc["fromRanges"]:
                                if fr["start"]["line"] == line1 - 1 and fr["start"]["character"] <= ch16 <= fr["end"]["character"]:
                                    to = oc["to"]
                                    dd = describe_loc(idx, {"uri": to["uri"], "range": to["selectionRange"]}, root)
                                    dd["name"] = to["name"]; dd["detail"] = to.get("detail")
                                    hits.append(dd)
                        rec["outgoing"] = hits
                        rec["outgoing_caller"] = enc["qname"]
                    else:
                        rec["outgoing"] = r2["error"]
        result["sites"].append(rec)
    result["rss_kb"] = rss_kb(c._proc.pid)
    result["hwm_kb"] = hwm_kb(c._proc.pid)
    result["total_s"] = round(time.monotonic() - t_start, 3)
    result["progress"] = progress_events(c)
    result["views"] = [e["msg"]["params"]["message"] for e in c.log
                       if e["msg"].get("method") == "window/logMessage" and "Created View" in e["msg"]["params"]["message"]]
    result["loads"] = [e["msg"]["params"]["message"] for e in c.log
                       if e["msg"].get("method") == "window/logMessage" and "packages.Load" in e["msg"]["params"]["message"]]
    result["server_requests"] = sorted({e["msg"]["method"] for e in c.log if e["dir"] == "in" and "id" in e["msg"] and "method" in e["msg"]})
    result["diagnostics"] = {}
    for e in c.log:
        m = e["msg"]
        if m.get("method") == "textDocument/publishDiagnostics":
            result["diagnostics"][os.path.relpath(uri_to_path(m["params"]["uri"]), root)] = [
                (d["range"]["start"]["line"] + 1, d.get("source"), d["message"][:200]) for d in m["params"]["diagnostics"]]
    await c.stop()
    dump_log(c, a.out + ".log.jsonl")
    with open(a.out, "w") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    print(f"ready {result['ready_s']}s total {result['total_s']}s sites {len(result['sites'])} rss {result['rss_kb']}kB hwm {result['hwm_kb']}kB")


asyncio.run(main())
