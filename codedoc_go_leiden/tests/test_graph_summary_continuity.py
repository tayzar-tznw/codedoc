"""Re-running --graph after --wiki keeps the wiki's summaries in Spanner (spec
§2.7, §7.4): Phase 8 rewrites the node rows with the doc comments, which stay
in graph_rows/*.jsonl for Phase 2, and then re-applies every cached summary
that still describes the sources: file and node summaries of files whose
cached sha256 is the file's, package and directory summaries whose children
hash as they did."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from graph_generator import config, graph_rows, pipeline
from graph_generator.pipeline import PipelineData, run_graph_track
from tests.conftest import FakeSpannerDb, write_tree
from tests.graph_world import (BAD, BASE, EXTRA, FEE, MAIN, REPO, cid, did, entities, fid,
                               make_data, mid, pid, resolutions)
from tests.pipeline_fakes import fake_discover, fake_scan_go_files
from tests.wiki_worlds import wiki_responder

FILE_SUMMARY = "ファイルの要約（一括）"   # wiki_responder's answers
PACKAGE_SUMMARY = "パッケージの要約"
DIR_SUMMARY = "ディレクトリの要約"


class _StatefulDb(FakeSpannerDb):
    """FakeSpannerDb that also keeps each node's current summary, applying
    upserts and updates in the order they arrive."""

    def __init__(self):
        super().__init__()
        self.summary: dict[str, dict[str, str]] = {}

    def batch(self):
        db, inner = self, super().batch()

        class _Batch:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def insert_or_update(self, table, columns, values):
                inner.insert_or_update(table, columns, values)
                db.apply(table, columns, values)

            def update(self, table, columns, values):
                inner.update(table, columns, values)
                db.apply(table, columns, values)

            def delete(self, table, keyset):
                inner.delete(table, keyset)

        return _Batch()

    def apply(self, table, columns, values):
        if "summary" in columns:
            at = columns.index("summary")
            for row in values:
                self.summary.setdefault(table, {})[row[0]] = row[at]


@pytest.fixture
def world(tmp_path, out_dir, monkeypatch, fake_vertexai):
    """The synthetic repo on disk, with Phases 1.5 and 1.6 from graph_world on
    the files' real hashes, and a stateful fake Spanner."""
    def extract(data):
        data.extracted_entities = {rel: {**ent, "sha256": data.file_hashes[rel]}
                                   for rel, ent in entities().items()}

    def resolve(data):
        files = {rel: {"sha256": data.file_hashes[rel], "calls": [], "imports": [],
                       "embeds": [], "implements": [], **frec}
                 for rel, frec in resolutions().items()}
        data.resolutions = {"version": 1, "repo": data.repo, "files": files, "stats": {}}

    monkeypatch.setattr(pipeline, "_scan_go_files", fake_scan_go_files)
    monkeypatch.setattr(pipeline, "_discover", fake_discover)
    monkeypatch.setattr(pipeline, "phase1b_extract", extract)
    monkeypatch.setattr(pipeline, "phase1c_resolve", resolve)
    db = _StatefulDb()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    repo = write_tree(tmp_path / REPO, {rel: f"package x\n\n// {rel}\n"
                                        for rel in [*entities(), BAD]})
    return repo, db


def _graph(repo):
    run_graph_track(PipelineData(target_dir=repo, repo=REPO))


def _wiki(repo):
    data = pipeline.load_graph_state(repo, REPO)
    pipeline.prepare_wiki(data, no_spanner=False)
    asyncio.run(pipeline.run_wiki_track(data, pipeline_client()))


def pipeline_client():
    from tests.conftest import FakeGenaiClient
    return FakeGenaiClient(responder=wiki_responder())


def _jsonl(out_dir, table):
    with open(os.path.join(out_dir, "graph_rows", f"{table}.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_graph_after_wiki_keeps_the_summaries_of_unchanged_files(world, out_dir, capsys):
    repo, db = world
    _graph(repo)
    calc = mid(FEE, "FeeCalculator.Calculate")
    assert db.summary["Methods"][calc] == "Calculate returns the fee."   # the doc comment
    _wiki(repo)
    wiki = {t: dict(s) for t, s in db.summary.items()}
    assert wiki["Methods"][calc] == "FeeCalculator.Calculate の要約"
    assert wiki["Files"][fid(FEE)] == FILE_SUMMARY
    assert wiki["Packages"][pid("libs/billing", "billing")] == PACKAGE_SUMMARY
    assert wiki["Directories"][did("libs/billing")] == DIR_SUMMARY

    with open(os.path.join(repo, MAIN), "a", encoding="utf-8") as f:   # one file changes
        f.write("\nfunc helper() {}\n")
    capsys.readouterr()
    _graph(repo)
    assert "wiki summaries from the summary cache" in capsys.readouterr().out
    now = db.summary
    for table in ("Files", "Classes", "Methods"):            # unchanged files keep theirs
        for node_id, summary in wiki[table].items():
            if node_id not in (fid(MAIN), cid(MAIN, "<package>"), mid(MAIN, "main")):
                assert now[table][node_id] == summary, (table, node_id)
    assert now["Packages"][pid("libs/billing", "billing")] == PACKAGE_SUMMARY
    for d in ("libs/billing", "libs"):
        assert now["Directories"][did(d)] == DIR_SUMMARY, d
    # the changed file and everything summarized from it fall back to Phase 8's values
    assert now["Files"][fid(MAIN)] == "" and now["Methods"][mid(MAIN, "main")] == ""
    assert now["Packages"][pid("cmd/reception", "main")] == ""
    assert now["Directories"][did("cmd/reception")] == ""
    assert now["Directories"][did("")] == ""     # one of its children changed
    # graph_rows/*.jsonl keeps the doc comments, which Phase 2 reads
    methods = {r["method_id"]: r for r in _jsonl(out_dir, "Methods")}
    assert methods[calc]["summary"] == "Calculate returns the fee."


def test_no_spanner_writes_nothing_to_spanner(world, out_dir):
    repo, db = world
    run_graph_track(PipelineData(target_dir=repo, repo=REPO, no_spanner=True))
    assert db.summary == {} and db.updates == {}


# ── cached_summaries ───────────────────────────────────────────────

def _cache(out_dir, rel, sha, file_summary, nodes):
    path = os.path.join(out_dir, "summaries", "nodes", f"{rel}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sha256": sha, "context_hash": "old", "mode": "batched", "model": "m",
                   "prompt_version": 0, "file": file_summary, "nodes": nodes}, f)


def _updates(data):
    rows = graph_rows.build_node_rows(data)
    return {t: dict(pairs) for t, pairs in graph_rows.cached_summaries(data, rows).items()}


def test_cached_summaries_need_the_files_sha256(out_dir):
    data = make_data()
    _cache(out_dir, FEE, data.file_hashes[FEE], "fee の要約",
           {"FeeCalculator.Calculate": "計算の要約", "Totaler": "合計の要約"})
    _cache(out_dir, BASE, "an older hash", "base の要約", {"Base.ID": "ID の要約"})
    updates = _updates(data)
    assert updates["Files"] == {fid(FEE): "fee の要約"}
    assert updates["Classes"] == {cid(FEE, "<package>"): "fee の要約",
                                  cid(FEE, "Totaler"): "合計の要約"}
    assert updates["Methods"] == {mid(FEE, "FeeCalculator.Calculate"): "計算の要約"}
    assert updates["Packages"] == {} and updates["Directories"] == {}


def test_package_and_directory_summaries_need_their_children_hash(out_dir, monkeypatch):
    data = make_data()
    _cache(out_dir, EXTRA, data.file_hashes[EXTRA], "extra の要約", {})
    children = [(EXTRA, "extra の要約")]
    digest = pipeline._children_hash(children)
    with open(os.path.join(out_dir, "summaries", "packages.json"), "w", encoding="utf-8") as f:
        json.dump({"libs/billing|billing": {"hash": digest, "summary": PACKAGE_SUMMARY},
                   "cmd/reception|main": {"hash": digest, "summary": "古い要約"}}, f)
    dir_digest = pipeline._children_hash([("fee_extra.go", "extra の要約")])
    with open(os.path.join(out_dir, "summaries", "dirs.json"), "w", encoding="utf-8") as f:
        json.dump({"libs/billing": {"hash": dir_digest, "summary": DIR_SUMMARY},
                   "libs": {"hash": pipeline._children_hash([("billing/", DIR_SUMMARY)]),
                            "summary": "libs の要約"},
                   "": {"hash": "stale", "summary": "ルートの要約"}}, f)
    updates = _updates(data)
    assert updates["Packages"] == {pid("libs/billing", "billing"): PACKAGE_SUMMARY}
    assert updates["Directories"] == {did("libs/billing"): DIR_SUMMARY, did("libs"): "libs の要約"}
    monkeypatch.setattr(config, "MODEL", "another-model")    # the children hash names the model
    updates = _updates(data)
    assert updates["Packages"] == {} and updates["Directories"] == {}
    assert updates["Files"] == {fid(EXTRA): "extra の要約"}


def test_cached_summaries_read_the_shared_cache_first(out_dir, tmp_path, monkeypatch):
    data = make_data()
    monkeypatch.setattr(config, "WIKI_SUMMARY_CACHE", str(tmp_path / "shared"))
    shared = os.path.join(str(tmp_path / "shared"), REPO)
    path = os.path.join(shared, "nodes", f"{FEE}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sha256": data.file_hashes[FEE], "file": "共有の要約", "nodes": {}}, f)
    assert _updates(data)["Files"] == {fid(FEE): "共有の要約"}


def test_cached_summaries_are_cut_like_phase_10(out_dir):
    data = make_data()
    _cache(out_dir, FEE, data.file_hashes[FEE], "長" * 5000, {})
    assert len(_updates(data)["Files"][fid(FEE)]) == pipeline._SUMMARY_LIMIT


def test_malformed_cache_records_are_ignored(out_dir):
    data = make_data()
    _cache(out_dir, FEE, data.file_hashes[FEE], "", {"New": "x"})      # no file summary
    path = os.path.join(out_dir, "summaries", "nodes", f"{BASE}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert all(not pairs for pairs in _updates(data).values())
