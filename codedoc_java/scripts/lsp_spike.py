#!/usr/bin/env python3
"""Throwaway spike: drive csharp-ls against the iryo-kanri corpus.

Decides/proves, per the approved plan:
  1. init handshake incl. answering the 3 server->client requests
  2. metadataUris enablement (experimental capability + workspace/configuration)
  3. solution-load ready detection (workDone progress end + documentSymbol retry)
  4. XPR divergence: byte-identical lines in ReceptionDesk vs AdminConsole
     resolve into different assemblies
  5. overload precision (OVL-001 vs OVL-005) via definition + hover
  6. csharp/metadata round-trip {assemblyName, symbolName, source}
  7. GO/NO-GO probes for spike-gated site kinds:
     operator (OVL-011/014), query_clause (LNQ-001/002), indexer (GEN-008),
     conversion (OVL-012)

Captures raw JSON per probe into tests/fixtures/spike/<CASE_ID>.json.

Run: .venv/bin/python scripts/lsp_spike.py
"""

import asyncio
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO, "test_codes", "iryo-kanri-system")
MANIFEST = os.path.join(CORPUS, "ground_truth", "expected_resolutions.json")
FIXTURE_DIR = os.path.join(REPO, "tests", "fixtures", "spike")

RECEPTION_PROBES = [
    "XPR-001", "XPR-004", "XPR-005",
    "OVL-001", "OVL-005", "OVL-011", "OVL-012",
    "GEN-005", "GEN-008",
    "EXT-002", "BND-001", "DI-001", "DLG-001", "BLD-004",
    "EIF-001", "VAR-001", "UNR-002",
    "LNQ-001", "LNQ-002", "LNQ-005",
]
ADMIN_PROBES = ["XPR-101", "XPR-105", "OVL-014"]


def utf16_col(line: str, cp_index: int) -> int:
    """Code-point index -> UTF-16 code-unit index (LSP position.character)."""
    return len(line[:cp_index].encode("utf-16-le")) // 2


class LspProc:
    """Minimal stdio LSP client for the spike (prototype for lsp_client.py)."""

    def __init__(self, cmd: list[str], cwd: str):
        self.cmd = cmd
        self.cwd = cwd
        self.proc = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.notifications: list[dict] = []
        self.server_requests: list[dict] = []
        self.progress_ends = 0
        self._reader_task = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self):
        try:
            while True:
                headers = b""
                while not headers.endswith(b"\r\n\r\n"):
                    chunk = await self.proc.stdout.read(1)
                    if not chunk:
                        return
                    headers += chunk
                length = 0
                for hline in headers.decode("ascii", "replace").split("\r\n"):
                    if hline.lower().startswith("content-length:"):
                        length = int(hline.split(":", 1)[1].strip())
                body = b""
                while len(body) < length:
                    chunk = await self.proc.stdout.read(length - len(body))
                    if not chunk:
                        return
                    body += chunk
                msg = json.loads(body.decode("utf-8"))
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, msg: dict):
        if "id" in msg and "method" in msg:  # server -> client request
            self.server_requests.append({"method": msg["method"]})
            result = None
            if msg["method"] == "workspace/configuration":
                items = msg.get("params", {}).get("items", [])
                result = [{"useMetadataUris": True} for _ in items]
            # client/registerCapability, window/workDoneProgress/create -> null
            await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        elif "id" in msg:  # response
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(json.dumps(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
        else:  # notification
            if msg.get("method") == "$/progress":
                if msg.get("params", {}).get("value", {}).get("kind") == "end":
                    self.progress_ends += 1
            self.notifications.append({"method": msg.get("method")})

    async def _send(self, msg: dict):
        body = json.dumps(msg).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        self.proc.stdin.write(frame)
        await self.proc.stdin.drain()

    async def request(self, method: str, params, timeout: float = 60.0):
        self._id += 1
        rid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout)

    async def notify(self, method: str, params):
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def stop(self):
        try:
            await asyncio.wait_for(self.request("shutdown", None, timeout=5), timeout=6)
            await self.notify("exit", None)
        except Exception:
            pass
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                self.proc.kill()


def uri_of(path: str) -> str:
    return "file://" + path


async def open_file(lsp: LspProc, path: str, opened: set):
    if path in opened:
        return
    text = open(path, encoding="utf-8").read()
    await lsp.notify("textDocument/didOpen", {
        "textDocument": {"uri": uri_of(path), "languageId": "csharp", "version": 1, "text": text}
    })
    opened.add(path)


async def wait_ready(lsp: LspProc, probe_file: str, opened: set, timeout_s: float = 240.0):
    """Progress-end + documentSymbol retry until non-empty."""
    await open_file(lsp, probe_file, opened)
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_err = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            syms = await lsp.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri_of(probe_file)}}, timeout=15)
            if syms:
                return {"ready": True, "progress_ends": lsp.progress_ends,
                        "server_requests": sorted({r["method"] for r in lsp.server_requests})}
        except Exception as e:  # not ready yet
            last_err = str(e)
        await asyncio.sleep(2.0)
    return {"ready": False, "last_err": last_err, "progress_ends": lsp.progress_ends}


async def probe_case(lsp: LspProc, case: dict, opened: set) -> dict:
    rel = case["file"]
    path = os.path.join(CORPUS, rel)
    lines = open(path, encoding="utf-8").read().splitlines()
    line_no = case["line"]  # 1-based
    line_text = lines[line_no - 1]
    if line_text.strip() != case["source_line"]:
        return {"error": f"source_line mismatch at {rel}:{line_no}"}
    token = case["token"]
    cp = line_text.index(token)
    pos = {"line": line_no - 1, "character": utf16_col(line_text, cp)}
    await open_file(lsp, path, opened)
    td = {"textDocument": {"uri": uri_of(path)}, "position": pos}

    out = {"case": case["id"], "site_kind": case["site_kind"], "pos": pos, "file": rel}
    try:
        out["definition"] = await lsp.request("textDocument/definition", td)
    except Exception as e:
        out["definition_error"] = str(e)
    try:
        out["hover"] = await lsp.request("textDocument/hover", td)
    except Exception as e:
        out["hover_error"] = str(e)

    # metadata round-trip for csharp:/ URIs
    defs = out.get("definition") or []
    if isinstance(defs, dict):
        defs = [defs]
    for d in defs:
        uri = d.get("uri") or d.get("targetUri", "")
        if uri.startswith("csharp:"):
            for shape_name, params in (
                ("textDocument", {"textDocument": {"uri": uri}}),
                ("flat", {"uri": uri}),
            ):
                try:
                    md = await lsp.request("csharp/metadata", params, timeout=30)
                    if md:
                        out["metadata_shape"] = shape_name
                        out["metadata"] = {
                            "projectName": md.get("projectName"),
                            "assemblyName": md.get("assemblyName"),
                            "symbolName": md.get("symbolName"),
                            "source_head": (md.get("source") or "")[:400],
                            "source_len": len(md.get("source") or ""),
                        }
                        # keep full source for fixture use
                        out["metadata_source"] = md.get("source")
                        break
                except Exception as e:
                    out.setdefault("metadata_errors", []).append(
                        {"shape": shape_name, "err": str(e)[:200]})
            break
    return out


async def run_session(sln_rel: str, ready_file_rel: str, probe_ids: list[str], cases: dict) -> dict:
    sln_dir = os.path.join(CORPUS, os.path.dirname(sln_rel))
    sln_path = os.path.join(CORPUS, sln_rel)
    lsp = LspProc(["csharp-ls", "-s", sln_path], cwd=sln_dir)
    await lsp.start()
    results: dict = {"sln": sln_rel}
    opened: set = set()
    try:
        init_result = await lsp.request("initialize", {
            "processId": os.getpid(),
            "rootUri": uri_of(sln_dir),
            "capabilities": {
                "workspace": {"configuration": True, "workspaceFolders": False},
                "window": {"workDoneProgress": True},
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                },
                "experimental": {
                    "csharp.metadataUris": True,
                    "csharp": {"metadataUris": True},
                },
            },
            "initializationOptions": {"csharp": {"useMetadataUris": True}},
            "workspaceFolders": None,
        }, timeout=30)
        results["server_capabilities_keys"] = sorted((init_result or {}).get("capabilities", {}).keys())
        await lsp.notify("initialized", {})
        results["ready"] = await wait_ready(lsp, os.path.join(CORPUS, ready_file_rel), opened)
        if not results["ready"]["ready"]:
            return results
        probes = {}
        for pid in probe_ids:
            probes[pid] = await probe_case(lsp, cases[pid], opened)
        results["probes"] = probes
    finally:
        await lsp.stop()
    return results


def summarize(all_results: list[dict], cases: dict):
    print("\n" + "=" * 72)
    print("SPIKE SUMMARY")
    print("=" * 72)
    gated = {"operator": [], "query_clause": [], "indexer": [], "conversion": []}
    xpr = {}
    for res in all_results:
        for pid, p in (res.get("probes") or {}).items():
            kind = p.get("site_kind")
            has_def = bool(p.get("definition"))
            md = p.get("metadata") or {}
            tag = "def" if has_def else ("ERR" if p.get("definition_error") else "none")
            line = f"  {pid:9s} {kind:18s} definition={tag}"
            if md:
                line += f"  metadata: asm={md.get('assemblyName')} sym={md.get('symbolName')}"
            elif p.get("hover"):
                line += "  hover=yes"
            print(line)
            if kind in gated:
                gated[kind].append(has_def or bool(md))
            if pid in ("XPR-001", "XPR-101"):
                xpr[pid] = md.get("assemblyName")
    print("-" * 72)
    for kind, oks in gated.items():
        verdict = "GO" if oks and all(oks) else ("PARTIAL" if any(oks) else "NO-GO")
        print(f"  gate {kind:14s}: {verdict}")
    if "XPR-001" in xpr and "XPR-101" in xpr:
        ok = xpr["XPR-001"] != xpr["XPR-101"] and xpr["XPR-001"] and xpr["XPR-101"]
        print(f"  XPR divergence: {xpr.get('XPR-001')} vs {xpr.get('XPR-101')} -> "
              + ("PROVEN" if ok else "NOT PROVEN"))
    print("=" * 72)


async def main():
    cases = {c["id"]: c for c in json.load(open(MANIFEST))["cases"]}
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    all_results = []
    for sln_rel, ready_rel, probes in (
        ("Iryo.ReceptionDesk/Iryo.ReceptionDesk.sln", "Iryo.ReceptionDesk/Flows/DailyCloseFlow.cs", RECEPTION_PROBES),
        ("Iryo.AdminConsole/Iryo.AdminConsole.sln", "Iryo.AdminConsole/DailyBatch.cs", ADMIN_PROBES),
    ):
        print(f"[spike] session: {sln_rel}")
        res = await run_session(sln_rel, ready_rel, probes, cases)
        print(f"[spike]   ready: {res.get('ready')}")
        all_results.append(res)
        for pid, p in (res.get("probes") or {}).items():
            fx = dict(p)
            with open(os.path.join(FIXTURE_DIR, f"{pid}.json"), "w") as f:
                json.dump(fx, f, ensure_ascii=False, indent=1)
    with open(os.path.join(FIXTURE_DIR, "_sessions.json"), "w") as f:
        json.dump([{k: v for k, v in r.items() if k != "probes"} for r in all_results], f, indent=1)
    summarize(all_results, cases)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
