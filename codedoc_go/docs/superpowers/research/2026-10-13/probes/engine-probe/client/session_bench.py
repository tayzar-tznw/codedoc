"""Session benchmark: readiness time, RSS, and definition latency on sampled call sites.

usage: session_bench.py FOLDER OUT_JSON [--files N] [--per-file K] [--settings JSON] [--env K=V]... [--open]
"""
import argparse, asyncio, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start, dump_log, progress_events, rss_kb, hwm_kb
from lsp_client import path_to_uri, LspError, LspTimeout

CALL = re.compile(r"(?<![\w.])(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\(")
KEYWORDS = {"func", "if", "for", "switch", "return", "go", "defer", "select", "case", "range", "map", "chan", "interface", "struct", "type", "var", "const", "import", "package"}

def sample_sites(root, nfiles, per_file):
    files = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in ("vendor", "testdata", "node_modules", ".git") and not d.startswith((".", "_")))
        for fn in sorted(fns):
            if fn.endswith(".go") and not fn.endswith("_test.go"):
                files.append(os.path.join(dp, fn))
    step = max(1, len(files) // nfiles)
    picked = files[::step][:nfiles]
    sites = []
    for p in picked:
        n = 0
        for i, line in enumerate(open(p, encoding="utf-8", errors="replace")):
            s = line.lstrip()
            if s.startswith(("//", "func ", "package ", "import ")):
                continue
            for m in CALL.finditer(line):
                if m.group(1) in KEYWORDS:
                    continue
                col16 = len(line[:m.start(1)].encode("utf-16-le")) // 2
                sites.append((p, i, col16, m.group(1)))
                n += 1
                break
            if n >= per_file:
                break
    return files, sites

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder"); ap.add_argument("out")
    ap.add_argument("--files", type=int, default=100); ap.add_argument("--per-file", type=int, default=5)
    ap.add_argument("--settings", default="{}"); ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    env = {}
    for kv in a.env:
        k, v = kv.split("=", 1); env[k] = v
    files, sites = sample_sites(a.folder, a.files, a.per_file)
    c = make_client(a.folder, json.loads(a.settings), env_over=env, stderr_path=a.out + ".stderr")
    t0 = time.monotonic()
    res, init_dt = await start(c)
    begins, ends = set(), set()
    def setup_begin(p):
        v = p.get("value", {})
        if v.get("kind") == "begin" and v.get("title") == "Setting up workspace":
            begins.add(p["token"])
        return False
    # wait for the first setup progress to begin, then for every begun setup token to end
    await c.wait_notification("$/progress", lambda p: p.get("value", {}).get("title") == "Setting up workspace", timeout=600)
    while True:
        for e in list(c.log):
            m = e["msg"]
            if m.get("method") == "$/progress":
                v = m["params"].get("value", {})
                if v.get("kind") == "begin" and v.get("title") == "Setting up workspace":
                    begins.add(m["params"]["token"])
                elif v.get("kind") == "end":
                    ends.add(m["params"]["token"])
        if begins and begins <= ends:
            break
        await asyncio.sleep(0.05)
        if time.monotonic() - t0 > 900:
            break
    t_ready = time.monotonic() - t0
    rss_ready = rss_kb(c._proc.pid)
    lat, empty, errors, n_ok = [], 0, 0, 0
    t1 = time.monotonic()
    first_ms = None
    for (p, line0, col16, name) in sites:
        if a.open:
            await c.notify("textDocument/didOpen", {"textDocument": {"uri": path_to_uri(p), "languageId": "go", "version": 1,
                                                                      "text": open(p, encoding="utf-8", errors="replace").read()}})
        ts = time.monotonic()
        try:
            r = await c.request("textDocument/definition", {"textDocument": {"uri": path_to_uri(p)},
                                                             "position": {"line": line0, "character": col16}}, timeout=120)
            ms = (time.monotonic() - ts) * 1000
            lat.append(ms)
            if first_ms is None: first_ms = ms
            if not r: empty += 1
            else: n_ok += 1
        except (LspError, LspTimeout) as e:
            errors += 1
    t_sweep = time.monotonic() - t1
    out = {"folder": a.folder, "files_total": len(files), "sites": len(sites), "ready_s": round(t_ready, 2),
           "rss_ready_mb": round(rss_ready / 1024), "sweep_s": round(t_sweep, 2), "first_def_ms": round(first_ms or -1),
           "p50_ms": round(sorted(lat)[len(lat)//2], 1) if lat else None,
           "p95_ms": round(sorted(lat)[int(len(lat)*0.95)], 1) if lat else None,
           "max_ms": round(max(lat), 1) if lat else None,
           "nonempty": n_ok, "empty": empty, "errors": errors,
           "rss_after_mb": round(rss_kb(c._proc.pid) / 1024), "hwm_mb": round(hwm_kb(c._proc.pid) / 1024),
           "setup_tokens": len(begins),
           "loads": [re.sub(r"\s+", " ", e["msg"]["params"]["message"])[20:400] for e in c.log
                     if e["msg"].get("method") == "window/logMessage" and "packages.Load" in e["msg"]["params"]["message"]][:5]}
    await c.stop()
    dump_log(c, a.out + ".log.jsonl")
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "loads"}))

if __name__ == "__main__":
    asyncio.run(main())
