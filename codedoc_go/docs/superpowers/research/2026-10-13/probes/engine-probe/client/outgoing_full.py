import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start
from lsp_client import path_to_uri
W = "/home/admin/scratch/codedoc-go-research/engine-probe"; P = W + "/probe"
async def main():
    c = make_client(P, {}, env_over={"XDG_CONFIG_HOME": W + "/xdgconfig-off"})
    await start(c)
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=300)
    await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=300)
    for rel, fn in (("app/funcvals.go", "func funcValues"), ("app/methods.go", "func methods"), ("app/generics.go", "func generics")):
        path = P + "/" + rel
        lines = open(path, encoding="utf-8").read().split("\n")
        l = next(i for i, x in enumerate(lines) if x.startswith(fn))
        items = await c.request("textDocument/prepareCallHierarchy", {"textDocument": {"uri": path_to_uri(path)}, "position": {"line": l, "character": 6}})
        out = await c.request("callHierarchy/outgoingCalls", {"item": items[0]})
        print(f"outgoingCalls({fn[5:]}):")
        for x in sorted(out, key=lambda x: x["fromRanges"][0]["start"]["line"]):
            for r in x["fromRanges"]:
                ln = r["start"]["line"]
                print(f"   L{ln+1:<3} {x['to']['name']:<12} kind={x['to']['kind']:<2} | {lines[ln].strip()[:70]}")
    await c.stop()
asyncio.run(main())
