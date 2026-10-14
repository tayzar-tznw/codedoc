"""Fakes for the pipeline's hand-off points.

With them the graph and wiki tracks run end to end without the work packages
that fill those points later: discovery (WP-D), the helper phases 1.5 and 1.6
(WP-B2), the builders (WP-B3) and the wiki phases 2, 3 and 10 (WP-B4).
"""
from __future__ import annotations

import asyncio
import os
import types

from graph_generator import pipeline
from tests.conftest import FakeGenaiClient

SKIPPED_DIR_NAMES = ("testdata", "vendor")

# Column lists the fake rows use; the real ones come from NODE_SPEC / EDGE_SPEC.
FAKE_COLUMNS = {
    "Files": ["file_id", "path", "repo"],
    "Directories": ["dir_id", "name", "repo"],
    "DirContainsFile": ["edge_id", "dir_id", "file_id"],
}


def fake_scan_go_files(root: str, exclude=()) -> tuple[list[str], dict[str, int]]:
    """A small stand-in for go_units.scan_go_files: .go files, minus the
    directories the go command skips (testdata, vendor, _x, .x)."""
    rels: list[str] = []
    skipped: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for name in sorted(dirnames):
            reason = ("dot" if name.startswith(".") else "underscore" if name.startswith("_")
                      else name if name in SKIPPED_DIR_NAMES else None)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name.endswith(".go"):
                rels.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return sorted(rels), skipped


def _dirs(rels):
    out = {""}
    for rel in rels:
        parts = rel.split("/")[:-1]
        out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return sorted(out)


def fake_discover(data):
    data.discovery = types.SimpleNamespace(units=[], groups=[], builds=[], warnings=[],
                                           toolchain="go1.27.2", mode=data.mode,
                                           module_source="none")


def fake_extract(data):
    data.extracted_entities = {rel: {"sha256": sha, "types": [], "funcs": []}
                               for rel, sha in data.file_hashes.items()}


def fake_resolve(data):
    data.resolutions = {
        "version": 1, "repo": data.repo,
        "files": {rel: {"sha256": sha, "calls": []} for rel, sha in data.file_hashes.items()},
        "stats": {"by_status": {"resolved": 2, "external": 1}}}


def fake_node_index(data):
    return {"files": {rel: pipeline._make_id("file", data.repo, rel, "")
                      for rel in data.file_hashes},
            "dirs": {d: pipeline._make_id("dir", data.repo, d, "")
                     for d in _dirs(data.file_hashes)}}


def fake_node_rows(data):
    idx = fake_node_index(data)
    return {
        "Files": [{"file_id": fid, "path": rel, "repo": data.repo}
                  for rel, fid in sorted(idx["files"].items())],
        "Directories": [{"dir_id": did, "name": d.rpartition("/")[2], "repo": data.repo}
                        for d, did in sorted(idx["dirs"].items())],
    }


def fake_edge_rows(data, node_index):
    rows = []
    for rel, fid in sorted(node_index["files"].items()):
        did = node_index["dirs"][rel.rpartition("/")[0]]
        rows.append({"edge_id": pipeline._edge_id("DirContainsFile", did, fid),
                     "dir_id": did, "file_id": fid})
    return {"DirContainsFile": rows}


def recorder(calls: list, name: str, fn):
    """fn, appending *name* to *calls* on every call (async-aware)."""
    if asyncio.iscoroutinefunction(fn):
        async def wrapped(*args, **kwargs):
            calls.append(name)
            return await fn(*args, **kwargs)
    else:
        def wrapped(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
    return wrapped


def install_graph_fakes(monkeypatch, calls: list | None = None) -> list:
    """Replace the graph track's hand-off points; returns the call log."""
    calls = [] if calls is None else calls
    monkeypatch.setattr(pipeline, "_scan_go_files", fake_scan_go_files)
    monkeypatch.setattr(pipeline, "_discover", recorder(calls, "discover", fake_discover))
    monkeypatch.setattr(pipeline, "phase1b_extract", recorder(calls, "phase1b_extract", fake_extract))
    monkeypatch.setattr(pipeline, "phase1c_resolve", recorder(calls, "phase1c_resolve", fake_resolve))
    monkeypatch.setattr(pipeline, "build_node_rows", recorder(calls, "build_node_rows", fake_node_rows))
    monkeypatch.setattr(pipeline, "_node_index", fake_node_index)
    monkeypatch.setattr(pipeline, "derive_edge_rows",
                        recorder(calls, "derive_edge_rows", fake_edge_rows))
    monkeypatch.setattr(pipeline, "_columns", lambda table: list(FAKE_COLUMNS[table]))
    return calls


def install_wiki_fakes(monkeypatch, calls: list | None = None, client=None) -> list:
    """Replace Phases 2, 3 and 10 and the Gemini client; Phases 4-6 stay real."""
    calls = [] if calls is None else calls

    async def phase2(data, client):
        data.file_summaries = {rel: f"{rel} の要約です。" for rel in data.file_hashes}

    async def phase3(data, client):
        data.dir_summaries = {"": "リポジトリ全体の要約です。"}

    monkeypatch.setattr(pipeline, "phase2_node_summaries", recorder(calls, "phase2", phase2))
    monkeypatch.setattr(pipeline, "phase3_package_dir_summaries", recorder(calls, "phase3", phase3))
    monkeypatch.setattr(pipeline, "phase10_write_back", recorder(calls, "phase10", lambda data: None))
    monkeypatch.setattr(pipeline, "_genai_client", lambda: client or FakeGenaiClient())
    return calls
