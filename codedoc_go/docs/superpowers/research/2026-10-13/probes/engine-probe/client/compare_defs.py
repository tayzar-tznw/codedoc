"""Compare definition answers on sampled sites between two gopls sessions (env variants)."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start
from lsp_client import path_to_uri, LspError
from session_bench import sample_sites

async def run(folder, env, sites):
    c = make_client(folder, {}, env_over=env)
    await start(c)
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=600)
    end = await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=600)
    out = []
    for (p, l, ch, name) in sites:
        try:
            r = await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(p)}, "position": {"line": l, "character": ch}}, timeout=120)
            out.append(sorted(f"{x['uri']}:{x['range']['start']['line']}:{x['range']['start']['character']}" for x in (r or [])))
        except LspError as e:
            out.append("ERR " + e.message[:60])
    views = [e["msg"]["params"]["message"].split("\n")[0:4] for e in c.log if e["msg"].get("method") == "window/logMessage" and "Created View" in e["msg"]["params"]["message"]]
    await c.stop()
    return out, end["value"].get("message"), views

async def main():
    folder = sys.argv[1]; gowork = sys.argv[2]; outp = sys.argv[3]
    _, sites = sample_sites(folder, 100, 3)
    base_env = {"GOPROXY": "off", "GOSUMDB": "off", "XDG_CONFIG_HOME": "/home/admin/scratch/codedoc-go-research/engine-probe/xdgconfig-off"}
    a, ea, va = await run(folder, base_env, sites)
    b, eb, vb = await run(folder, dict(base_env, GOWORK=gowork), sites)
    diff = [(s[0], s[1] + 1, s[3], x, y) for s, x, y in zip(sites, a, b) if x != y]
    res = {"sites": len(sites), "differ": len(diff), "end_repo": ea, "end_gen": eb, "nonempty_repo": sum(1 for x in a if x and not isinstance(x, str)),
           "nonempty_gen": sum(1 for x in b if x and not isinstance(x, str)), "diffs": diff[:10], "views_gen": vb}
    json.dump(res, open(outp, "w"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k not in ("diffs", "views_gen")}))
    for d in diff[:5]: print("  DIFF", d)

asyncio.run(main())
