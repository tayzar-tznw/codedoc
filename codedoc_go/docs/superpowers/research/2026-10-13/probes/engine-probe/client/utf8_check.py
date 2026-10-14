import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start
from lsp_client import path_to_uri, LspError
W = "/home/admin/scratch/codedoc-go-research/engine-probe"; P = W + "/probe"
async def main():
    c = make_client(P, {}, env_over={"XDG_CONFIG_HOME": W + "/xdgconfig-off"})
    await start(c)
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=300)
    await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=300)
    enc = P + "/app/encoding.go"
    lines = open(enc, encoding="utf-8").read().split("\n")
    l = next(i for i, x in enumerate(lines) if "//@EN01" in x); line = lines[l]
    cp = line.index("héllo()")
    for name, col in (("utf-16", len(line[:cp].encode("utf-16-le")) // 2), ("utf-8", len(line[:cp].encode("utf-8")))):
        r = await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(enc)}, "position": {"line": l, "character": col}})
        print(f"EN01 `héllo` with {name} column {col}:", [(x['uri'].rsplit('/',1)[-1], x['range']) for x in r or []])
    await c.stop()
asyncio.run(main())
