"""Phases 8 and 9 with the real builders (C6, C7): graph_rows/*.jsonl and the
Spanner rows carry the same values in write_columns order, BOOL columns are
JSON booleans, and the graph track completes its checkpoint."""
from __future__ import annotations

import json
import os

from graph_generator import pipeline
from graph_generator.pipeline import PipelineData, run_graph_track
from graph_generator.setup_spanner_graph import write_columns
from tests.conftest import write_tree
from tests.graph_world import (BAD, EDGE_TABLES, FEE, MAIN, NODE_TABLES, REPO, entities, fid,
                               make_data, mid, resolutions)
from tests.pipeline_fakes import fake_discover, fake_scan_go_files

BOOL_COLUMNS = {"Files": ("generated", "build_included"), "Methods": ("has_body",),
                "ClassEmbeds": ("pointer",), "ClassImplements": ("pointer_receiver",),
                "PossiblyCalls": ("open_world",)}


def _jsonl(out_dir: str) -> dict[str, list[dict]]:
    rows_dir = os.path.join(out_dir, "graph_rows")
    out = {}
    for name in sorted(n for n in os.listdir(rows_dir) if n.endswith(".jsonl")):
        with open(os.path.join(rows_dir, name), encoding="utf-8") as f:
            out[name.removesuffix(".jsonl")] = [json.loads(line) for line in f]
    return out


def _write_both(out_dir: str, fake_spanner) -> dict[str, list[dict]]:
    """Phases 8-9 with --no-spanner, then to (fake) Spanner; jsonl is written
    both times (R20) and must not differ."""
    local = make_data()
    local.no_spanner = True
    pipeline.phase8_write_nodes(local)
    pipeline.phase9_write_edges(local)
    local_jsonl = _jsonl(out_dir)
    spanner = make_data()
    pipeline.phase8_write_nodes(spanner)
    pipeline.phase9_write_edges(spanner)
    assert local.graph_counts == spanner.graph_counts
    assert _jsonl(out_dir) == local_jsonl
    return local_jsonl


def test_jsonl_lines_and_spanner_rows_are_identical(out_dir, fake_spanner):
    jsonl = _write_both(out_dir, fake_spanner)
    assert set(jsonl) == set(NODE_TABLES) | set(EDGE_TABLES)
    for table, rows in jsonl.items():
        columns = write_columns(table)
        assert rows, table
        assert all(list(row) == columns for row in rows), table
        assert fake_spanner.columns[table] == columns
        assert fake_spanner.mutations[table] == [[row[c] for c in columns] for row in rows]


def test_bool_columns_are_json_booleans(out_dir, fake_spanner):
    jsonl = _write_both(out_dir, fake_spanner)
    for table, columns in BOOL_COLUMNS.items():
        for row in jsonl[table]:
            assert all(type(row[c]) is bool for c in columns), (table, row)
    for table, rows in fake_spanner.mutations.items():
        for column in BOOL_COLUMNS.get(table, ()):
            at = write_columns(table).index(column)
            assert all(type(row[at]) is bool for row in rows), (table, column)
    with open(os.path.join(out_dir, "graph_rows", "PossiblyCalls.jsonl"), encoding="utf-8") as f:
        assert '"open_world": true' in f.read()


def _graph_track_fakes(monkeypatch):
    """Phases 1.5 and 1.6 from the synthetic world, on the hashes of real files."""
    def extract(data):
        data.extracted_entities = {rel: {**ent, "sha256": data.file_hashes[rel]}
                                   for rel, ent in entities().items()}

    def resolve(data):
        files = {rel: {"sha256": data.file_hashes[rel], "calls": [], "imports": [],
                       "embeds": [], "implements": [], **frec}
                 for rel, frec in resolutions().items()}
        data.resolutions = {"version": 1, "repo": data.repo, "files": files,
                            "stats": {"by_status": {"resolved": 6}}}

    monkeypatch.setattr(pipeline, "_scan_go_files", fake_scan_go_files)
    monkeypatch.setattr(pipeline, "_discover", fake_discover)
    monkeypatch.setattr(pipeline, "phase1b_extract", extract)
    monkeypatch.setattr(pipeline, "phase1c_resolve", resolve)


def test_the_graph_track_writes_every_table_and_completes(tmp_path, out_dir, monkeypatch):
    _graph_track_fakes(monkeypatch)
    repo = write_tree(tmp_path / REPO, {rel: "package x\n" for rel in [*entities(), BAD]})
    data = PipelineData(target_dir=repo, repo=REPO, no_spanner=True)
    run_graph_track(data)
    with open(os.path.join(out_dir, "graph_checkpoint.json"), encoding="utf-8") as f:
        checkpoint = json.load(f)
    assert (checkpoint["complete"], checkpoint["phase"], checkpoint["spanner"]) == (
        True, "phase9", None)
    jsonl = _jsonl(out_dir)
    assert {t: len(rows) for t, rows in jsonl.items()} == data.graph_counts
    calls = {(r["caller_method"], r["callee_method"]) for r in jsonl["MethodCalls"]}
    assert (mid(MAIN, "main"), mid(FEE, "New")) in calls
    assert {r["file_id"] for r in jsonl["Files"]} == {fid(rel) for rel in [*entities(), BAD]}


def test_phase_9_reports_and_saves_what_it_counted(out_dir, fake_spanner, capsys):
    data = make_data()
    std = {"module": "std", "version": "", "package": "", "qualname": "", "peer": False}
    data.resolutions["files"][MAIN]["imports"] += [
        {"line": 7, "col": 2, "import_path": "strings", "status": "external", "target": None,
         "external": std, "reason": None},
        {"line": 8, "col": 2, "import_path": "example.com/thirdparty/textfmt",
         "status": "external", "target": None, "reason": None,
         "external": {**std, "module": "example.com/thirdparty/textfmt"}}]
    data.no_spanner = True
    pipeline.phase8_write_nodes(data)
    pipeline.phase9_write_edges(data)
    out = capsys.readouterr().out
    assert "[Phase 9] External imports by module: std 2, example.com/thirdparty/textfmt 1" in out
    with open(os.path.join(out_dir, "graph_rows", "stats.json"), encoding="utf-8") as f:
        stats = json.load(f)
    assert stats["external_imports"] == {"std": 2, "example.com/thirdparty/textfmt": 1}
    assert stats["not_edged"]["calls_external"] == 2
    assert stats["not_edged"]["imports_external"] == 3


def test_phase_9_lists_at_most_five_modules(out_dir, fake_spanner, capsys):
    data = make_data()
    data.resolutions["files"][MAIN]["imports"] = [
        {"line": 10 + i, "col": 2, "import_path": f"example.com/m{i}/x", "status": "external",
         "target": None, "reason": None,
         "external": {"module": f"example.com/m{i}", "version": "", "package": "",
                      "qualname": "", "peer": False}} for i in range(7)]
    data.no_spanner = True
    pipeline.phase8_write_nodes(data)
    pipeline.phase9_write_edges(data)
    line = [ln for ln in capsys.readouterr().out.splitlines() if "External imports" in ln][0]
    assert line.count("example.com/m") == 5 and line.endswith("and 2 more modules")
