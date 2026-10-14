"""Smoke-probe gopls v0.23.0 with codedoc_python's lsp_client.py (copied, unchanged).

Checks: serverInfo, positionEncoding negotiation, definition at an import
path, a cross-package call, an interface method call, implementation, a
generic method call, and a call after non-ASCII text (UTF-16 vs byte column).
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from lsp_client import LspClient, path_to_uri, uri_to_path

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gp")
MAIN = os.path.join(ROOT, "app", "main.go")

def loc(r):
    out = []
    for it in (r if isinstance(r, list) else [r] if r else []):
        uri = it.get("targetUri") or it.get("uri")
        rng = it.get("targetSelectionRange") or it.get("range")
        out.append((os.path.relpath(uri_to_path(uri), ROOT), rng["start"]["line"], rng["start"]["character"], rng["end"]["character"]))
    return out

async def main():
    env = dict(os.environ)
    c = LspClient([sys.argv[1], "serve"], cwd=ROOT, default_timeout=60, env=env,
                  watch_notifications={"window/logMessage", "$/progress", "window/showMessage"},
                  workspace_folders=[ROOT])
    await c.start()
    init = await c.request("initialize", {"processId": os.getpid(), "rootUri": path_to_uri(ROOT),
        "workspaceFolders": [{"uri": path_to_uri(ROOT), "name": "gp"}],
        "capabilities": {"general": {"positionEncodings": ["utf-8", "utf-16"]},
                         "window": {"workDoneProgress": True},
                         "textDocument": {"definition": {"linkSupport": True}, "implementation": {"linkSupport": True}}}})
    print("serverInfo:", init.get("serverInfo"))
    print("capabilities.positionEncoding:", init["capabilities"].get("positionEncoding"))
    print("implementationProvider:", init["capabilities"].get("implementationProvider"))
    await c.notify("initialized", {})
    text = open(MAIN, encoding="utf-8").read()
    await c.notify("textDocument/didOpen", {"textDocument": {"uri": path_to_uri(MAIN), "languageId": "go", "version": 1, "text": text}})
    lines = text.split("\n")
    def at(line_no, needle, nth=0):
        ln = lines[line_no]
        i = -1
        for _ in range(nth + 1):
            i = ln.index(needle, i + 1)
        b = len(ln[:i].encode("utf-8")); u = len(ln[:i].encode("utf-16-le")) // 2
        return {"byte": b, "utf16": u}
    async def ask(method, line_no, col):
        return await c.request(method, {"textDocument": {"uri": path_to_uri(MAIN)}, "position": {"line": line_no, "character": col}})
    cases = [("import path", 5, "lib\""), ("lib.New call", 9, "New"), ("s.Get after 日本語 (utf16 col)", 10, "Get"),
             ("b.Val (generic type method)", 12, "Val"), ("b.Map (Go 1.27 generic method)", 13, "Map"),
             ("new(expr)", 14, "new"), ("f() closure var", 16, "f()")]
    for name, ln, needle in cases:
        cols = at(ln, needle)
        r16 = await ask("textDocument/definition", ln, cols["utf16"])
        out = {"definition@utf16": loc(r16)}
        if cols["byte"] != cols["utf16"]:
            out["definition@byte"] = loc(await ask("textDocument/definition", ln, cols["byte"]))
        print(f"{name}: cols={cols} {out}")
    cols = at(10, "Get")
    print("implementation at s.Get:", loc(await ask("textDocument/implementation", 10, cols["utf16"])))
    await c.stop()

asyncio.run(main())
