#!/usr/bin/env python3
"""Throwaway spike: qualify Microsoft's official roslyn-language-server
against the same ground-truth probes used to qualify csharp-ls.

Decides:
  1. handshake/launch shape (--stdio --autoLoadProjects) and which
     server->client requests/notifications the client must handle
  2. ready signal (workspace/projectInitializationComplete?) + warmup needs
  3. definition payloads on the trap cases (XPR/OVL/EXT/LNQ/GEN/operator)
  4. how definition-into-NuGet looks (MetadataAsSource temp file? header?)
  5. hover format differences

Captures raw JSON to tests/fixtures/roslyn_spike/.
Run: .venv/bin/python scripts/roslyn_spike.py [ReceptionDesk|AdminConsole]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph_generator.lsp_client import LspClient, LspError, LspTimeout  # noqa: E402

CORPUS = os.path.abspath("test_codes/iryo-kanri-system")
MAN = json.load(open(CORPUS + "/ground_truth/expected_resolutions.json"))
CASES = {c["id"]: c for c in MAN["cases"]}
FIXTURE_DIR = "tests/fixtures/roslyn_spike"

PROBES = {
    "ReceptionDesk": ("Iryo.ReceptionDesk", "Iryo.ReceptionDesk/Flows/DailyCloseFlow.cs",
                      ["XPR-001", "OVL-001", "OVL-005", "EXT-002", "LNQ-001",
                       "GEN-008", "OVL-011", "BND-001", "UNR-002", "VAR-001"]),
    "AdminConsole": ("Iryo.AdminConsole", "Iryo.AdminConsole/DailyBatch.cs",
                     ["XPR-101", "XPR-105", "OVL-014"]),
}


def u16(line, cp):
    return len(line[:cp].encode("utf-16-le")) // 2


class RoslynProbe(LspClient):
    """LspClient + visibility: records every server->client request and
    notification method; answers workspace/workspaceFolders."""

    def __init__(self, *a, folders=None, **kw):
        super().__init__(*a, **kw)
        self.folders = folders or []
        self.seen_server_requests = {}
        self.seen_notifications = {}
        self.config_sections_asked = []
        self.init_complete = asyncio.Event()

    async def _answer_server_request(self, msg):
        method = msg["method"]
        self.seen_server_requests[method] = self.seen_server_requests.get(method, 0) + 1
        if method == "workspace/workspaceFolders":
            await self._send({"jsonrpc": "2.0", "id": msg["id"],
                              "result": [{"uri": "file://" + f, "name": os.path.basename(f)}
                                         for f in self.folders]})
            return
        if method == "client/unregisterCapability":
            await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            return
        if method == "workspace/configuration":
            items = (msg.get("params") or {}).get("items", [])
            self.config_sections_asked.extend(i.get("section", "?") for i in items)
        await super()._answer_server_request(msg)

    async def _handle(self, msg):
        if "method" in msg and "id" not in msg:
            m = msg.get("method", "?")
            self.seen_notifications[m] = self.seen_notifications.get(m, 0) + 1
            if m == "workspace/projectInitializationComplete":
                self.init_complete.set()
        await super()._handle(msg)


async def run_probe(label):
    proj_dir_rel, ready_rel, probe_ids = PROBES[label]
    proj_dir = os.path.join(CORPUS, proj_dir_rel)
    logdir = f"/tmp/roslyn-logs-{label}"
    os.makedirs(logdir, exist_ok=True)
    client = RoslynProbe(
        ["roslyn-language-server", "--stdio", "--logLevel", "Information",
         "--extensionLogDirectory", logdir, "--autoLoadProjects"],
        cwd=proj_dir, folders=[proj_dir], default_timeout=60.0)
    await client.start()
    out = {"label": label, "probes": {}}
    try:
        init_res = await client.request("initialize", {
            "processId": os.getpid(),
            "rootUri": "file://" + proj_dir,
            "workspaceFolders": [{"uri": "file://" + proj_dir, "name": proj_dir_rel}],
            "capabilities": {
                "workspace": {"configuration": True, "workspaceFolders": True},
                "window": {"workDoneProgress": True},
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {},
                },
            },
        }, timeout=60)
        out["server_capability_keys"] = sorted((init_res or {}).get("capabilities", {}).keys())
        await client.notify("initialized", {})

        # ready: wait for projectInitializationComplete, then documentSymbol probe
        try:
            await asyncio.wait_for(client.init_complete.wait(), timeout=180)
            out["ready_signal"] = "workspace/projectInitializationComplete"
        except asyncio.TimeoutError:
            out["ready_signal"] = "TIMEOUT (no projectInitializationComplete in 180s)"

        opened = set()

        async def op(path):
            if path in opened:
                return
            await client.notify("textDocument/didOpen", {
                "textDocument": {"uri": "file://" + path, "languageId": "csharp",
                                 "version": 1, "text": open(path, encoding="utf-8").read()}})
            opened.add(path)

        ready_abs = os.path.join(CORPUS, ready_rel)
        await op(ready_abs)
        for i in range(60):
            try:
                syms = await client.request("textDocument/documentSymbol",
                                            {"textDocument": {"uri": "file://" + ready_abs}},
                                            timeout=15)
                if syms:
                    break
            except (LspError, LspTimeout):
                pass
            await asyncio.sleep(2)
        out["documentSymbol_ok"] = bool(syms)

        # semantic warmup: definition on the first probe until non-null
        first = CASES[probe_ids[0]]
        fpath = os.path.join(CORPUS, first["file"])
        await op(fpath)
        raw = open(fpath, encoding="utf-8").read().splitlines()[first["line"] - 1]
        cp = raw.find(first["snippet"])
        cp = (cp + first["snippet"].index(first["token"])) if cp >= 0 else raw.index(first["token"])
        wpos = {"line": first["line"] - 1, "character": u16(raw, cp)}
        for i in range(90):
            try:
                d = await client.request("textDocument/definition",
                                         {"textDocument": {"uri": "file://" + fpath},
                                          "position": wpos}, timeout=20)
                if d:
                    break
            except (LspError, LspTimeout):
                pass
            await asyncio.sleep(2)
        out["warmup_rounds"] = i + 1

        for pid in probe_ids:
            case = CASES[pid]
            path = os.path.join(CORPUS, case["file"])
            await op(path)
            raw = open(path, encoding="utf-8").read().splitlines()[case["line"] - 1]
            cp = raw.find(case["snippet"])
            cp = (cp + case["snippet"].index(case["token"])) if cp >= 0 else raw.index(case["token"])
            pos = {"line": case["line"] - 1, "character": u16(raw, cp)}
            td = {"textDocument": {"uri": "file://" + path}, "position": pos}
            rec = {"case": pid, "site_kind": case["site_kind"], "pos": pos}
            try:
                rec["definition"] = await client.request("textDocument/definition", td, timeout=30)
            except Exception as e:
                rec["definition_error"] = f"{type(e).__name__}: {e}"
            try:
                rec["hover"] = await client.request("textDocument/hover", td, timeout=30)
            except Exception as e:
                rec["hover_error"] = f"{type(e).__name__}: {e}"

            # inspect definition target files (MetadataAsSource?)
            defs = rec.get("definition") or []
            if isinstance(defs, dict):
                defs = [defs]
            for d in defs[:1]:
                uri = d.get("uri") or d.get("targetUri") or ""
                rec["target_uri"] = uri
                p = uri[len("file://"):] if uri.startswith("file://") else None
                if p and not p.startswith(CORPUS) and os.path.exists(p):
                    head = open(p, encoding="utf-8", errors="replace").read()
                    rec["target_head"] = head[:600]
                    rec["target_len"] = len(head)
            out["probes"][pid] = rec
            os.makedirs(FIXTURE_DIR, exist_ok=True)
            with open(os.path.join(FIXTURE_DIR, f"{pid}.json"), "w") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
    finally:
        await client.stop()

    out["server_requests_seen"] = client.seen_server_requests
    out["notifications_seen"] = {k: v for k, v in client.seen_notifications.items()
                                 if not k.startswith("window/log")}
    out["config_sections"] = sorted(set(client.config_sections_asked))
    with open(os.path.join(FIXTURE_DIR, f"_session_{label}.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def summarize(out):
    print(f"\n===== {out['label']} =====")
    print("ready:", out.get("ready_signal"), "| documentSymbol:", out.get("documentSymbol_ok"),
          "| warmup rounds:", out.get("warmup_rounds"))
    print("server->client requests:", out.get("server_requests_seen"))
    print("notifications:", out.get("notifications_seen"))
    print("config sections asked:", out.get("config_sections", [])[:12])
    for pid, rec in out.get("probes", {}).items():
        d = rec.get("definition")
        n = len(d) if isinstance(d, list) else (1 if d else 0)
        uri = rec.get("target_uri", "")
        kind = "corpus" if CORPUS in uri else ("metadata-file" if rec.get("target_head") else
                                               ("none" if not uri else "other"))
        hv = rec.get("hover") or {}
        hval = ""
        if isinstance(hv, dict):
            c = hv.get("contents")
            hval = (c.get("value", "") if isinstance(c, dict) else str(c))[:90]
        print(f"  {pid:9s} defs={n} target={kind}")
        print(f"            uri={uri[:110]}")
        if rec.get("target_head"):
            print("            head: " + rec["target_head"][:160].replace("\n", " | "))
        print(f"            hover: {hval}")


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else None
    for label in ([which] if which else ["ReceptionDesk", "AdminConsole"]):
        summarize(await run_probe(label))


asyncio.run(main())
