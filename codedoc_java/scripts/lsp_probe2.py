"""Throwaway probe #2: raw def+hover for the still-failing case positions."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph_generator.lsp_client import LspClient
CORPUS = os.path.abspath("test_codes/iryo-kanri-system")
MAN = json.load(open(CORPUS + "/ground_truth/expected_resolutions.json"))
CASES = {c["id"]: c for c in MAN["cases"]}
PROBES = ["EXT-002", "EXT-003", "BLD-001", "BLD-005", "TWIN-003", "OVL-011", "GEN-008", "OVL-001"]

def u16(line, cp):
    return len(line[:cp].encode("utf-16-le")) // 2

async def main():
    sln_dir = os.path.join(CORPUS, "Iryo.ReceptionDesk")
    c = LspClient(["csharp-ls", "-s", os.path.join(sln_dir, "Iryo.ReceptionDesk.sln")],
                  cwd=sln_dir, config_sections={"csharp": {"useMetadataUris": True}})
    await c.start()
    await c.request("initialize", {"processId": os.getpid(), "rootUri": "file://" + sln_dir,
        "capabilities": {"workspace": {"configuration": True}, "window": {"workDoneProgress": True},
            "textDocument": {"hover": {"contentFormat": ["markdown"]},
                             "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
            "experimental": {"csharp.metadataUris": True, "csharp": {"metadataUris": True}}},
        "initializationOptions": {"csharp": {"useMetadataUris": True}}, "workspaceFolders": None}, timeout=30)
    await c.notify("initialized", {})
    try:
        await c.wait_progress_end(timeout=90)
    except Exception:
        pass
    opened = set()
    async def op(p):
        if p in opened: return
        await c.notify("textDocument/didOpen", {"textDocument": {"uri": "file://" + p,
            "languageId": "csharp", "version": 1, "text": open(p).read()}})
        opened.add(p)
    # readiness: retry def on OVL-001 until non-null
    case = CASES["OVL-001"]; path = os.path.join(CORPUS, case["file"]); await op(path)
    line = open(path).read().splitlines()[case["line"] - 1]
    pos = {"line": case["line"] - 1, "character": u16(line, line.index(case["token"]))}
    for i in range(45):
        d = await c.request("textDocument/definition", {"textDocument": {"uri": "file://" + path}, "position": pos})
        if d: break
        await asyncio.sleep(2)
    for pid in PROBES:
        case = CASES[pid]
        path = os.path.join(CORPUS, case["file"]); await op(path)
        raw = open(path).read().splitlines()[case["line"] - 1]
        cp = raw.find(case["snippet"]) ; cp = (cp + case["snippet"].index(case["token"])) if cp >= 0 else raw.index(case["token"])
        pos = {"line": case["line"] - 1, "character": u16(raw, cp)}
        td = {"textDocument": {"uri": "file://" + path}, "position": pos}
        try: d = await c.request("textDocument/definition", td, timeout=20)
        except Exception as e: d = f"ERR {e}"
        try: h = await c.request("textDocument/hover", td, timeout=20)
        except Exception as e: h = f"ERR {e}"
        print(f"== {pid} {case['site_kind']} tok={case['token']!r} pos={pos}")
        print("   def:", json.dumps(d, ensure_ascii=False)[:360])
        hv = (h or {}).get("contents", {}).get("value", "") if isinstance(h, dict) else h
        print("   hover:", json.dumps(hv, ensure_ascii=False)[:200])
    await c.stop()

asyncio.run(main())
