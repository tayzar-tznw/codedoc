"""Shared helpers for the engine-probe gopls drivers."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from lsp_client import LspClient, path_to_uri, uri_to_path  # noqa: E402

GOPLS = "/home/admin/scratch/codedoc-go-research/gopath/bin/gopls"

CLIENT_CAPS = {
    "general": {"positionEncodings": ["utf-8", "utf-16"]},
    "workspace": {"configuration": True, "workspaceFolders": True,
                  "didChangeConfiguration": {"dynamicRegistration": True},
                  "didChangeWatchedFiles": {"dynamicRegistration": True}},
    "window": {"workDoneProgress": True, "showMessage": {}},
    "textDocument": {
        "synchronization": {"didSave": True},
        "definition": {"linkSupport": False},
        "typeDefinition": {"linkSupport": False},
        "implementation": {"linkSupport": False},
        "declaration": {"linkSupport": False},
        "references": {},
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        "callHierarchy": {},
        "publishDiagnostics": {},
    },
}


def make_client(folder: str, settings: dict, env_over: dict | None = None,
                folders: list[str] | None = None, init_options: dict | None = None,
                stderr_path: str | None = None, request_answers: dict | None = None,
                caps: dict | None = None, gopls_args: list[str] | None = None):
    env = dict(os.environ)
    for k, v in (env_over or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    answers = request_answers or {}

    def config_answer(section, scope):
        if section in ("gopls", ""):
            return settings
        return None

    def on_request(method, params):
        if method in answers:
            a = answers[method]
            return a(params) if callable(a) else a
        return None

    folders = folders or [folder]
    client = LspClient([GOPLS] + (gopls_args or ["serve"]), cwd=folder, config_answer=config_answer,
                       default_timeout=60.0,
                       watch_notifications={"$/progress", "window/logMessage", "window/showMessage",
                                            "textDocument/publishDiagnostics"},
                       workspace_folders=folders, env=env, on_request=on_request,
                       stderr_path=stderr_path)
    client._init_params = {
        "processId": os.getpid(),
        "clientInfo": {"name": "codedoc-engine-probe", "version": "0"},
        "rootUri": path_to_uri(folders[0]),
        "workspaceFolders": [{"uri": path_to_uri(f), "name": os.path.basename(f)} for f in folders],
        "capabilities": caps if caps is not None else CLIENT_CAPS,
        "initializationOptions": init_options if init_options is not None else {},
    }
    return client


async def start(client):
    await client.start()
    t0 = time.monotonic()
    res = await client.request("initialize", client._init_params, timeout=60)
    await client.notify("initialized", {})
    return res, time.monotonic() - t0


def progress_events(client):
    """(t, token, kind, title/message) for every $/progress and workDoneProgress/create seen."""
    out = []
    for e in client.log:
        m = e["msg"]
        if e["dir"] == "in" and m.get("method") == "$/progress":
            v = m["params"].get("value", {})
            out.append((e["t"], m["params"].get("token"), v.get("kind"),
                        v.get("title") or v.get("message")))
    return out


def dump_log(client, path):
    with open(path, "w") as f:
        for e in client.log:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1


def hwm_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1
