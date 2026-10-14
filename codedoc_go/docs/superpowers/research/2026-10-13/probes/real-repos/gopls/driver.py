#!/usr/bin/env python3
"""Drive gopls over stdio: textDocument/definition at every call site from the helper's sites.jsonl.

Research probe (codedoc_go, track real-repos). Usage:
  driver.py --root REPO --sites sites.jsonl --out DIR [--file-sample N --seed S] [--concurrency K]
            [--impl-sample N] [--timeout SEC] [--label L] [--settings JSON]

Writes DIR/defs.jsonl (one record per site), DIR/impls.jsonl, DIR/run.json (timings, memory, errors).
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lsp_client import LspClient, LspConnectionError, LspError, LspTimeout, path_to_uri, uri_to_path  # noqa: E402


def proc_status(pid: int) -> dict[str, int]:
    out = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("VmHWM", "VmRSS"):
                    out[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    return out


def norm_locs(result) -> list[dict]:
    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]
    locs = []
    for r in result:
        uri = r.get("uri") or r.get("targetUri")
        rng = r.get("range") or r.get("targetSelectionRange") or r.get("targetRange")
        try:
            path = uri_to_path(uri)
        except ValueError:
            path = uri
        locs.append({"path": path, "line0": rng["start"]["line"], "char16": rng["start"]["character"],
                     "end_line0": rng["end"]["line"], "end_char16": rng["end"]["character"]})
    return locs


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--sites", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--file-sample", type=int, default=0, help="sample N files (stratified by top dir); 0 = all")
    ap.add_argument("--site-classes", default="", help="only sites of these classes (comma list)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--impl-sample", type=int, default=0)
    ap.add_argument("--impl-only", action="store_true")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--ready-timeout", type=float, default=1200.0)
    ap.add_argument("--label", default="run")
    ap.add_argument("--settings", default="{}")
    ap.add_argument("--gopls", default="gopls")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    root = os.path.realpath(args.root)
    settings = json.loads(args.settings)

    sites = [json.loads(line) for line in open(args.sites)]
    if args.site_classes:
        keep = set(args.site_classes.split(","))
        sites = [s for s in sites if s["class"] in keep]
    by_file: dict[str, list[dict]] = collections.defaultdict(list)
    for s in sites:
        by_file[s["file"]].append(s)
    files = sorted(by_file)
    if args.file_sample and args.file_sample < len(files):
        rnd = random.Random(args.seed)
        strata: dict[str, list[str]] = collections.defaultdict(list)
        for f in files:
            strata[f.split("/")[0] + ("/test" if f.endswith("_test.go") else "")].append(f)
        frac = args.file_sample / len(files)
        chosen = []
        for key in sorted(strata):
            group = strata[key]
            k = max(1, round(len(group) * frac))
            chosen.extend(rnd.sample(group, min(k, len(group))))
        files = sorted(chosen)

    run: dict = {"label": args.label, "root": root, "files": len(files),
                 "sites": sum(len(by_file[f]) for f in files), "concurrency": args.concurrency,
                 "settings": settings, "progress": [], "errors": collections.Counter(), "timeouts": 0}
    t0 = time.monotonic()

    def cfg_answer(section: str, scope):
        if section == "gopls":
            return settings
        return None

    client = LspClient([args.gopls], cwd=root, config_answer=cfg_answer, default_timeout=args.timeout,
                       watch_notifications={"$/progress", "window/logMessage", "window/showMessage",
                                            "textDocument/publishDiagnostics"},
                       workspace_folders=[root], env=dict(os.environ),
                       stderr_path=os.path.join(args.out, "gopls.stderr"))
    await client.start()
    pid = client.pid
    peak = {"rss": 0}
    stop_sampler = asyncio.Event()

    async def sampler():
        while not stop_sampler.is_set():
            st = proc_status(pid)
            peak["rss"] = max(peak["rss"], st.get("VmRSS", 0))
            try:
                await asyncio.wait_for(stop_sampler.wait(), 0.5)
            except asyncio.TimeoutError:
                pass

    sampler_task = asyncio.ensure_future(sampler())
    init = await client.request("initialize", {
        "processId": os.getpid(),
        "rootUri": path_to_uri(root),
        "workspaceFolders": [{"uri": path_to_uri(root), "name": os.path.basename(root)}],
        "capabilities": {
            "general": {"positionEncodings": ["utf-16"]},
            "window": {"workDoneProgress": True},
            "workspace": {"configuration": True, "workspaceFolders": True},
            "textDocument": {
                "synchronization": {"didSave": False},
                "definition": {"linkSupport": False},
                "implementation": {"linkSupport": False},
                "publishDiagnostics": {},
            },
        },
        "initializationOptions": settings,
    }, timeout=120)
    run["server_info"] = init.get("serverInfo")
    run["position_encoding"] = init.get("capabilities", {}).get("positionEncoding")
    await client.notify("initialized", {})
    t_init = time.monotonic()

    # readiness: the first $/progress 'end' after a 'begin' (gopls's initial workspace load)
    begun: dict = {}

    def is_end(params):
        tok = params.get("token")
        val = params.get("value") or {}
        if val.get("kind") == "begin":
            begun[tok] = val.get("title")
            run["progress"].append({"t": round(time.monotonic() - t0, 2), "kind": "begin", "title": val.get("title"),
                                    "message": val.get("message")})
            return False
        if val.get("kind") == "end" and tok in begun:
            run["progress"].append({"t": round(time.monotonic() - t0, 2), "kind": "end", "title": begun[tok],
                                    "message": val.get("message")})
            return True
        return False

    ready_how = "progress-end"
    try:
        await client.wait_notification("$/progress", is_end, timeout=args.ready_timeout)
    except LspTimeout:
        ready_how = "timeout"
    t_ready = time.monotonic()
    run["ready_how"] = ready_how
    run["time_to_ready_s"] = round(t_ready - t0, 2)
    run["vm_at_ready"] = proc_status(pid)
    print(f"[{args.label}] ready after {t_ready - t0:.1f}s ({ready_how}), VmHWM {run['vm_at_ready']}", file=sys.stderr,
          flush=True)

    latencies: list[float] = []
    out_defs = open(os.path.join(args.out, "defs.jsonl"), "w")
    sem = asyncio.Semaphore(args.concurrency)

    async def one(path: str, s: dict, method: str) -> dict:
        async with sem:
            ts = time.monotonic()
            rec = {"file": s["file"], "call_off": s["call_off"], "line": s["line"], "char16": s["char16"],
                   "class": s["class"]}
            try:
                res = await client.request(method, {
                    "textDocument": {"uri": path_to_uri(path)},
                    "position": {"line": s["line"] - 1, "character": s["char16"]}}, timeout=args.timeout)
                rec["locs"] = norm_locs(res)
            except LspTimeout:
                rec["error"] = "timeout"
                run["timeouts"] += 1
            except LspError as e:
                rec["error"] = f"lsp:{e.code}:{e.message[:200]}"
                run["errors"][f"{e.code}:{e.message[:80]}"] += 1
            except LspConnectionError as e:
                rec["error"] = f"conn:{e}"
                run["errors"]["connection"] += 1
            rec["ms"] = round((time.monotonic() - ts) * 1000, 1)
            latencies.append(rec["ms"])
            return rec

    t_q0 = time.monotonic()
    n_done = 0
    if not args.impl_only:
        for i, rel in enumerate(files):
            path = os.path.join(root, rel)
            text = open(path, encoding="utf-8", errors="surrogateescape").read()
            try:
                await client.notify("textDocument/didOpen", {"textDocument": {
                    "uri": path_to_uri(path), "languageId": "go", "version": 1, "text": text}})
                recs = await asyncio.gather(*(one(path, s, "textDocument/definition") for s in by_file[rel]))
                await client.notify("textDocument/didClose", {"textDocument": {"uri": path_to_uri(path)}})
            except LspConnectionError as e:
                run["errors"]["connection-fatal"] += 1
                print(f"[{args.label}] connection error: {e}", file=sys.stderr, flush=True)
                break
            for r in recs:
                out_defs.write(json.dumps(r) + "\n")
            n_done += len(recs)
            if i % 50 == 0:
                el = time.monotonic() - t_q0
                print(f"[{args.label}] {i}/{len(files)} files, {n_done} sites, {el:.0f}s, "
                      f"{n_done / max(el, 1e-9):.0f} req/s, {proc_status(pid)}", file=sys.stderr, flush=True)
    out_defs.close()
    t_q1 = time.monotonic()
    run["definition_requests"] = n_done
    run["definition_seconds"] = round(t_q1 - t_q0, 2)
    run["definition_req_per_s"] = round(n_done / max(t_q1 - t_q0, 1e-9), 1)
    if latencies:
        ls = sorted(latencies)
        run["latency_ms"] = {"p50": ls[len(ls) // 2], "p90": ls[int(len(ls) * 0.9)], "p99": ls[int(len(ls) * 0.99)],
                             "max": ls[-1]}

    # implementation on a sample of interface-method sites
    if args.impl_sample:
        rnd = random.Random(args.seed + 1)
        iface_sites = [s for s in sites if s["class"] == "iface"]
        sample = rnd.sample(iface_sites, min(args.impl_sample, len(iface_sites)))
        by_f: dict[str, list[dict]] = collections.defaultdict(list)
        for s in sample:
            by_f[s["file"]].append(s)
        out_impl = open(os.path.join(args.out, "impls.jsonl"), "w")
        lat_impl = []
        t_i0 = time.monotonic()
        for rel in sorted(by_f):
            path = os.path.join(root, rel)
            text = open(path, encoding="utf-8", errors="surrogateescape").read()
            await client.notify("textDocument/didOpen", {"textDocument": {
                "uri": path_to_uri(path), "languageId": "go", "version": 1, "text": text}})
            for s in by_f[rel]:
                r = await one(path, s, "textDocument/implementation")
                r["iface"] = s.get("iface")
                lat_impl.append(r["ms"])
                out_impl.write(json.dumps(r) + "\n")
            await client.notify("textDocument/didClose", {"textDocument": {"uri": path_to_uri(path)}})
        out_impl.close()
        t_i1 = time.monotonic()
        ls = sorted(lat_impl)
        run["implementation"] = {"requests": len(sample), "seconds": round(t_i1 - t_i0, 2),
                                 "p50_ms": ls[len(ls) // 2] if ls else None,
                                 "p90_ms": ls[int(len(ls) * 0.9)] if ls else None,
                                 "max_ms": ls[-1] if ls else None}

    run["vm_at_end"] = proc_status(pid)
    run["peak_rss_sampled_mb"] = peak["rss"]
    run["total_s"] = round(time.monotonic() - t0, 2)
    run["diagnostics_notifications"] = client.notification_count("textDocument/publishDiagnostics")
    run["show_messages"] = [p for p in client.notification_params.get("window/showMessage", [])][:20]
    stop_sampler.set()
    await sampler_task
    await client.stop()
    run["errors"] = dict(run["errors"])
    with open(os.path.join(args.out, "run.json"), "w") as f:
        json.dump(run, f, indent=2)
    print(f"[{args.label}] done: {json.dumps({k: run[k] for k in ('time_to_ready_s', 'definition_requests', 'definition_seconds', 'definition_req_per_s', 'timeouts', 'errors', 'vm_at_end', 'total_s')})}",
          file=sys.stderr, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
