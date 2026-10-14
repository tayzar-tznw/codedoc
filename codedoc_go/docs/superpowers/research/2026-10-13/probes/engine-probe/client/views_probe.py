"""Open given files one at a time (each in a fresh session) and report the views gopls creates.

usage: views_probe.py FOLDER SETTINGS_JSON ENVJSON file1 [file2 ...]
A file argument "a.go+b.go" opens both in the same session.
"""
import asyncio, json, os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start
from lsp_client import path_to_uri

def views(c):
    out = []
    for e in c.log:
        m = e["msg"]
        if m.get("method") == "window/logMessage" and "Created View" in m["params"]["message"]:
            v = m["params"]["message"]
            g = lambda pat: (re.search(pat, v, re.S).group(1) if re.search(pat, v, re.S) else None)
            out.append({"id": g(r"Created View \(#(\d+)\)"), "type": g(r'view_type="(\w+)"'), "root": g(r'root_dir="file://([^"]+)"'),
                        "overlay": g(r"env_overlay=(\[.*?\])"), "gowork": g(r"ExplicitGOWORK:(\S*)")})
    return out

async def one(folder, settings, env, files):
    c = make_client(folder, settings, env_over=env)
    await start(c)
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=300)
    await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=300)
    for f in files:
        await c.notify("textDocument/didOpen", {"textDocument": {"uri": path_to_uri(f), "languageId": "go", "version": 1, "text": open(f).read()}})
    await asyncio.sleep(1.5)
    v = views(c)
    await c.stop()
    return v

async def main():
    folder, settings, env = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
    for arg in sys.argv[4:]:
        files = [os.path.join(folder, x) for x in arg.split("+")] if arg != "-" else []
        v = await one(folder, settings, env, files)
        print(arg, "->", json.dumps(v))

asyncio.run(main())
