"""definition on import paths: one location per file of the imported package (variant-dependent?)."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start
from lsp_client import path_to_uri
W = "/home/admin/scratch/codedoc-go-research/engine-probe"; P = W + "/probe"
async def main():
    c = make_client(P, {}, env_over={"XDG_CONFIG_HOME": W + "/xdgconfig-off"})
    await start(c)
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=300)
    await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=300)
    for rel, needle in (("app/main.go", '"example.com/lib/testy"'), ("lib/testy/testy_ext_test.go", '"example.com/lib/testy"'),
                        ("app/pkgcalls.go", '"example.com/lib"'), ("app/third.go", '"github.com/google/uuid"')):
        path = P + "/" + rel
        lines = open(path, encoding="utf-8").read().split("\n")
        l = next(i for i, x in enumerate(lines) if needle in x)
        ch = lines[l].index(needle) + 5
        r = await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(path)}, "position": {"line": l, "character": ch}})
        print(f"{rel} import {needle} -> {len(r or [])} locations:", [x['uri'].split('/')[-1] + ':' + str(x['range']['start']['line'] + 1) for x in r or []])
    await c.stop()
asyncio.run(main())
