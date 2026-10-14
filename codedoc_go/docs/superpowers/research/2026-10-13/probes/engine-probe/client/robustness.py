"""Robustness: odd requests, kill -9 mid-session, restart, shutdown/exit code."""
import asyncio, json, os, signal, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start, rss_kb
from lsp_client import path_to_uri, LspError, LspTimeout, LspConnectionError

W = "/home/admin/scratch/codedoc-go-research/engine-probe"
P = W + "/probe"
ENV = {"XDG_CONFIG_HOME": W + "/xdgconfig-off"}

async def ready(c):
    b = await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=300)
    await c.wait_notification("$/progress", lambda p: p.get("token") == b["token"] and p["value"].get("kind") == "end", timeout=300)

async def define(c, path, line, ch):
    try:
        r = await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(path)}, "position": {"line": line, "character": ch}}, timeout=30)
        return ("ok", [ (x["uri"].rsplit("/",1)[-1], x["range"]["start"]) for x in (r or [])])
    except LspError as e:
        return ("error", f"{e.code}: {e.message}"[:160])
    except LspTimeout as e:
        return ("timeout", str(e))
    except LspConnectionError as e:
        return ("connection", str(e))

async def main():
    c = make_client(P, {}, env_over=ENV)
    await start(c); await ready(c)
    pk = P + "/app/pkgcalls.go"
    cases = [
        ("valid PK02", pk, 18, 5),
        ("line beyond EOF", pk, 999, 0),
        ("char beyond EOL", pk, 18, 400),
        ("negative-free odd: whitespace", pk, 18, 0),
        ("comment", pk, 18, 20),
        ("nonexistent file", P + "/app/nope.go", 1, 1),
        ("non-Go file (go.mod)", P + "/app/go.mod", 0, 8),
        ("file outside workspace folder", W + "/outside/x.go", 2, 22),
        ("asm .s file", P + "/app/internal/asm/asm_amd64.s", 3, 7),
    ]
    for name, path, l, ch in cases:
        print(f"{name:32} -> {await define(c, path, l, ch)}")
    print("alive after odd requests:", c.is_alive)
    pid = c._proc.pid
    os.kill(pid, signal.SIGKILL)
    t0 = time.monotonic()
    res = await define(c, pk, 18, 5)
    print(f"after SIGKILL: {res} detected in {time.monotonic()-t0:.3f}s; is_alive={c.is_alive}")
    await c.stop()
    # restart
    t0 = time.monotonic()
    c2 = make_client(P, {}, env_over=ENV)
    await start(c2); await ready(c2)
    print(f"restart to ready: {time.monotonic()-t0:.3f}s; definition: {await define(c2, pk, 18, 5)}")
    # graceful shutdown + exit code
    await c2.request("shutdown", None, timeout=5)
    await c2.notify("exit", None)
    rc = await asyncio.wait_for(c2._proc.wait(), 5)
    print("exit code after shutdown+exit:", rc)
    await c2.stop()
    # exit without shutdown
    c3 = make_client(P, {}, env_over=ENV)
    await start(c3); await ready(c3)
    await c3.notify("exit", None)
    rc = await asyncio.wait_for(c3._proc.wait(), 5)
    print("exit code after exit without shutdown:", rc)
    await c3.stop()
    # stdin closed
    c4 = make_client(P, {}, env_over=ENV)
    await start(c4); await ready(c4)
    c4._proc.stdin.close()
    try:
        rc = await asyncio.wait_for(c4._proc.wait(), 10)
        print("exit code after stdin EOF:", rc)
    except asyncio.TimeoutError:
        print("gopls did not exit within 10s of stdin EOF")
    await c4.stop()

asyncio.run(main())
