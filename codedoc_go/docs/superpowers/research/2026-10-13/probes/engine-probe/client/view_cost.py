"""Memory/time cost of extra views: open N build-excluded files in etcd and query one site in each."""
import asyncio, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start, rss_kb, hwm_kb
from lsp_client import path_to_uri, LspError
W = "/home/admin/scratch/codedoc-go-research/engine-probe"; E = "/home/admin/scratch/codedoc-go-research/repos/etcd"
CALL = re.compile(r"(?<![\w.])(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\(")

def first_call(path):
    for i, line in enumerate(open(path, encoding="utf-8")):
        s = line.lstrip()
        if s.startswith(("//", "func ", "package ", "import ")): continue
        m = CALL.search(line)
        if m and m.group(1) not in ("if", "for", "switch", "func", "return"):
            return i, len(line[:m.start(1)].encode("utf-16-le")) // 2
    return None

async def run(files):
    c = make_client(E, {}, env_over={"XDG_CONFIG_HOME": W + "/xdgconfig-off", "GOPROXY": "off"})
    t0 = time.monotonic()
    await start(c)
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=300)
    await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=300)
    base = E + "/server/etcdserver/server.go"
    pos = first_call(base)
    await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(base)}, "position": {"line": pos[0], "character": pos[1]}}, timeout=300)
    res = []
    for f in files:
        p = os.path.join(E, f)
        await c.notify("textDocument/didOpen", {"textDocument": {"uri": path_to_uri(p), "languageId": "go", "version": 1, "text": open(p).read()}})
        pos = first_call(p)
        if pos is None:
            res.append((f, "no call site")); continue
        try:
            r = await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(p)}, "position": {"line": pos[0], "character": pos[1]}}, timeout=300)
            res.append((f, "ok" if r else "empty"))
        except LspError as e:
            res.append((f, "error " + e.message[:60]))
    await asyncio.sleep(2)
    views = sum(1 for e in c.log if e["msg"].get("method") == "window/logMessage" and "Created View" in e["msg"]["params"]["message"])
    out = {"opened": len(files), "views": views, "secs": round(time.monotonic() - t0, 1), "rss_mb": rss_kb(c._proc.pid) // 1024, "hwm_mb": hwm_kb(c._proc.pid) // 1024, "answers": res}
    await c.stop()
    return out

async def main():
    sets = [["client/pkg/fileutil/lock_windows.go", "client/pkg/fileutil/sync_darwin.go", "client/pkg/transport/sockopt_wasm.go", "client/pkg/fileutil/lock_solaris.go", "client/pkg/transport/keepalive_listener_openbsd.go"], [], ["client/pkg/fileutil/lock_windows.go"]]
    for s in sets:
        print(json.dumps(await run(s)))

asyncio.run(main())
