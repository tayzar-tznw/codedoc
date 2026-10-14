"""Misc checks: incoming calls for package-level initializers, UTF-8 offsets, vulncheck prompt."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start
from lsp_client import path_to_uri, LspError

W = "/home/admin/scratch/codedoc-go-research/engine-probe"
P = W + "/probe"
ENV = {"XDG_CONFIG_HOME": W + "/xdgconfig-off"}

async def ready(c):
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=300)
    await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=300)

def find(path, needle, occ=1):
    for i, line in enumerate(open(path, encoding="utf-8")):
        if needle in line:
            occ -= 1
            if occ == 0:
                return i, line.index(needle), line
    raise KeyError(needle)

async def main():
    c = make_client(P, {}, env_over=ENV)
    await start(c); await ready(c)
    lib = P + "/lib/lib.go"
    l, ch, _ = find(lib, "func Hello")
    items = await c.request("textDocument/prepareCallHierarchy", {"textDocument": {"uri": path_to_uri(lib)}, "position": {"line": l, "character": ch + 5}})
    inc = await c.request("callHierarchy/incomingCalls", {"item": items[0]})
    print("incomingCalls(lib.Hello):")
    for x in inc:
        f = x["from"]
        print("   from", f["name"], f.get("detail"), f["uri"].split("/probe/")[1], f["selectionRange"]["start"], "kind", f["kind"], "fromRanges", [r["start"]["line"] + 1 for r in x["fromRanges"]])
    nc = P + "/app/notcalls.go"
    l, ch, _ = find(nc, "var initVal")
    try:
        r = await c.request("textDocument/prepareCallHierarchy", {"textDocument": {"uri": path_to_uri(nc)}, "position": {"line": l, "character": ch + 4}})
        print("prepareCallHierarchy at `var initVal`:", r)
    except LspError as e:
        print("prepareCallHierarchy at `var initVal`: error", e)
    l, ch, _ = find(nc, "func init()")
    r = await c.request("textDocument/prepareCallHierarchy", {"textDocument": {"uri": path_to_uri(nc)}, "position": {"line": l, "character": ch + 5}})
    print("prepareCallHierarchy at first `func init`:", [(x["name"], x.get("detail"), x["selectionRange"]["start"]) for x in r])
    # UTF-8 vs UTF-16 offsets on the astral-char line (EN02)
    enc = P + "/app/encoding.go"
    l, _, line = find(enc, "//@EN02")
    cp = line.index("𝔊()")
    u16 = len(line[:cp].encode("utf-16-le")) // 2
    u8 = len(line[:cp].encode("utf-8"))
    for name, col in (("utf-16 offset", u16), ("utf-8 offset", u8), ("code-point offset", cp)):
        try:
            r = await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(enc)}, "position": {"line": l, "character": col}})
            print(f"EN02 definition with {name} ({col}):", [(x['uri'].rsplit('/',1)[-1], x['range']['start']) for x in r or []])
        except LspError as e:
            print(f"EN02 definition with {name} ({col}): error {e}")
    # vulncheck prompt: open + didSave go.mod
    gm = P + "/app/go.mod"
    await c.notify("textDocument/didOpen", {"textDocument": {"uri": path_to_uri(gm), "languageId": "go.mod", "version": 1, "text": open(gm).read()}})
    await c.notify("textDocument/didSave", {"textDocument": {"uri": path_to_uri(gm)}})
    await asyncio.sleep(3)
    reqs = [e for e in c.log if e["dir"] == "in" and "id" in e["msg"] and e["msg"].get("method") == "window/showMessageRequest"]
    print("showMessageRequest after didSave(go.mod):", [json.dumps(e["msg"]["params"])[:300] for e in reqs])
    replies = [e for e in c.log if e["dir"] == "out" and reqs and e["msg"].get("id") == reqs[0]["msg"]["id"] and "result" in e["msg"]]
    print("our reply:", [r["msg"] for r in replies])
    later = [e["msg"]["method"] for e in c.log if e["dir"] == "in" and reqs and e["t"] > reqs[0]["t"] and "method" in e["msg"]]
    print("server messages after the reply:", sorted(set(later)))
    await c.stop()

asyncio.run(main())
