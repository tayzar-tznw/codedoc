"""R48: re-ingest removes the rows the code no longer has. Phase 8 records the
primary keys of the previous rows (graph_prune.json) when graph_meta.json
names this repo and this run's Spanner target and the repo has rows there;
after the Phase 9 upserts the keys this run did not write are deleted (edges,
then nodes), with the FileBelongsToTopic rows of deleted files."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from graph_generator import config, pipeline
from graph_generator.graph_rows import EDGE_ROW_TABLES, NODE_ROW_TABLES
from graph_generator.pipeline import PipelineData, run_graph_track
from graph_generator.setup_spanner_graph import EDGE_SPEC, NODE_SPEC, write_columns
from tests.conftest import FakeGenaiClient, FakeSpannerDb, write_tree
from tests.graph_world import BAD, BASE, EXTRA, FEE, REPO, entities, fid, mid, resolutions
from tests.pipeline_fakes import fake_discover, fake_scan_go_files
from tests.wiki_worlds import wiki_responder

TABLES = (*NODE_ROW_TABLES, *EDGE_ROW_TABLES)


def _pk(table: str) -> str:
    return (NODE_SPEC.get(table) or EDGE_SPEC[table])[0]


class SpannerState(FakeSpannerDb):
    """FakeSpannerDb that keeps rows per table: upserts and inserts add or
    merge them, updates merge into existing rows, KeySet deletes and the
    topic DML of Phase 10 remove them; it answers the queries of Phases 8-9."""

    def __init__(self):
        super().__init__()
        self.rows: dict[str, dict[str, dict]] = {}

    def _put(self, table, columns, values, *, must_exist=False):
        pk = _pk(table)
        for value in values:
            row = dict(zip(columns, value))
            current = self.rows.setdefault(table, {}).get(row[pk])
            if must_exist and current is None:
                raise KeyError(f"update of a missing {table} row {row[pk]}")
            self.rows[table][row[pk]] = {**(current or {}), **row}

    def batch(self):
        db, inner = self, super().batch()

        class _Batch:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def insert_or_update(self, table, columns, values):
                inner.insert_or_update(table, columns, values)
                db._put(table, columns, values)

            def update(self, table, columns, values):
                inner.update(table, columns, values)
                db._put(table, columns, values, must_exist=True)

            def delete(self, table, keyset):
                inner.delete(table, keyset)
                for key in keyset.keys:
                    db.rows.get(table, {}).pop(key[0], None)

        return _Batch()

    def run_in_transaction(self, fn, *args, **kwargs):
        db = self

        class _Txn:
            def execute_update(self, sql, params=None, param_types=None):
                db.dml.append((sql, dict(params or {})))
                for table in ("FileBelongsToTopic", "Topics"):
                    if sql.startswith(f"DELETE FROM {table} "):
                        db.rows.pop(table, None)    # one repo in these tests
                return 0

            def insert(self, table, columns, values):
                db._put(table, columns, values)

        self.transactions += 1
        return fn(_Txn(), *args, **kwargs)

    def sql_result(self, sql, params):
        if "FROM Files WHERE repo" in sql:
            return [[1]] if any(r.get("repo") == params["repo"]
                                for r in self.rows.get("Files", {}).values()) else []
        if "FROM FileBelongsToTopic WHERE file_id IN UNNEST" in sql:
            ids = set(params["ids"])
            return [[k] for k, r in self.rows.get("FileBelongsToTopic", {}).items()
                    if r.get("file_id") in ids]
        return super().sql_result(sql, params)

    def keys(self, table):
        return set(self.rows.get(table, {}))

    def summary(self, table, key):
        return self.rows[table][key]["summary"]


@pytest.fixture
def world(tmp_path, out_dir, monkeypatch, fake_vertexai):
    """The graph_world repo on disk with Phases 1.5 and 1.6 from graph_world
    on the files' real hashes; `edits` removes files and calls from them."""
    edits = {"deleted": set(), "drop_calls": set()}

    def extract(data):
        data.extracted_entities = {rel: {**ent, "sha256": data.file_hashes[rel]}
                                   for rel, ent in entities().items()
                                   if rel in data.file_hashes}

    def resolve(data):
        files = {}
        for rel, frec in resolutions().items():
            if rel not in data.file_hashes:
                continue
            calls = [c for c in frec.get("calls", [])
                     if (rel, c["site"]["name"], c["site"]["line"]) not in edits["drop_calls"]]
            files[rel] = {"sha256": data.file_hashes[rel], "imports": [], "embeds": [],
                          "implements": [], **frec, "calls": calls}
        data.resolutions = {"version": 1, "repo": data.repo, "files": files, "stats": {}}

    monkeypatch.setattr(pipeline, "_scan_go_files", fake_scan_go_files)
    monkeypatch.setattr(pipeline, "_discover", fake_discover)
    monkeypatch.setattr(pipeline, "phase1b_extract", extract)
    monkeypatch.setattr(pipeline, "phase1c_resolve", resolve)
    db = SpannerState()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    repo = write_tree(tmp_path / REPO, {rel: f"package x\n\n// {rel}\n"
                                        for rel in [*entities(), BAD]})

    def delete(rel):
        os.remove(os.path.join(repo, rel))
        edits["deleted"].add(rel)

    return {"repo": repo, "db": db, "edits": edits, "delete": delete}


def _graph(world, **kw):
    run_graph_track(PipelineData(target_dir=world["repo"], repo=REPO, **kw))


def _wiki(world):
    base = wiki_responder()

    def respond(model, contents, cfg):   # one topic that links BASE, to be deleted later
        out = base(model, contents, cfg)
        if out.startswith('[{"name":"料金計算"'):
            return json.dumps([{"name": "料金計算", "files": [FEE, EXTRA, BASE],
                                "subtopics": []}], ensure_ascii=False)
        return out

    data = pipeline.load_graph_state(world["repo"], REPO)
    pipeline.prepare_wiki(data, no_spanner=False)
    asyncio.run(pipeline.run_wiki_track(data, FakeGenaiClient(responder=respond)))


def _jsonl_keys(out_dir, table):
    with open(os.path.join(out_dir, "graph_rows", f"{table}.jsonl"), encoding="utf-8") as f:
        return {json.loads(line)[_pk(table)] for line in f}


def _prune_path(out_dir):
    return os.path.join(out_dir, "graph_prune.json")


def test_reingest_removes_what_the_code_no_longer_has(world, out_dir, capsys):
    db = world["db"]
    _graph(world)
    _wiki(world)
    base_topics = {k for k, r in db.rows["FileBelongsToTopic"].items() if r["file_id"] == fid(BASE)}
    assert base_topics and fid(BASE) in db.keys("Files")
    calc = mid(FEE, "FeeCalculator.Calculate")
    assert db.summary("Methods", calc) == "FeeCalculator.Calculate の要約"
    topics_before = db.keys("Topics")

    # remove a call, delete a file, change a file
    world["edits"]["drop_calls"].add((EXTRA, "mk", 10))
    world["delete"](BASE)
    with open(os.path.join(world["repo"], EXTRA), "a", encoding="utf-8") as f:
        f.write("\nfunc extra() {}\n")
    capsys.readouterr()
    _graph(world)
    out = capsys.readouterr().out

    for table in TABLES:      # no key of the previous rows survives unless written again
        assert db.keys(table) == _jsonl_keys(out_dir, table), table
    assert fid(BASE) not in db.keys("Files")
    assert not db.keys("FileBelongsToTopic") & base_topics
    assert {r["file_id"] for r in db.rows["FileBelongsToTopic"].values()} == {fid(FEE), fid(EXTRA)}
    assert db.keys("Topics") == topics_before            # topics belong to the wiki track
    round_ = mid(EXTRA, "FeeCalculator.Round")
    assert (round_, mid(FEE, "New")) not in {(r["caller_method"], r["callee_method"])
                                             for r in db.rows["MethodCalls"].values()}
    assert "[Phase 9] Removed " in out and "stale rows: " in out and "Files 1" in out
    assert db.summary("Methods", calc) == "FeeCalculator.Calculate の要約"   # unchanged file
    assert db.summary("Methods", round_) == ""                                # changed file
    assert db.summary("Files", fid(EXTRA)) == ""
    assert not os.path.exists(_prune_path(out_dir))


def test_a_crash_after_phase_8_still_prunes_on_resume(world, out_dir, monkeypatch, capsys):
    db = world["db"]
    _graph(world)
    world["delete"](BASE)
    original = pipeline.phase9_write_edges
    monkeypatch.setattr(pipeline, "phase9_write_edges",
                        lambda data: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _graph(world)
    assert os.path.exists(_prune_path(out_dir))
    assert fid(BASE) in db.keys("Files")                   # not pruned yet
    monkeypatch.setattr(pipeline, "phase9_write_edges", original)
    capsys.readouterr()
    _graph(world)
    out = capsys.readouterr().out
    assert "Skipping Phase 8" in out and "[Phase 9] Removed " in out
    for table in TABLES:
        assert db.keys(table) == _jsonl_keys(out_dir, table), table
    assert not os.path.exists(_prune_path(out_dir))


def test_a_crash_inside_phase_8_loses_nothing(world, out_dir, monkeypatch):
    db = world["db"]
    _graph(world)
    world["delete"](BASE)
    original = pipeline._write_rows

    def crash_after_clearing(data, rows, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(pipeline, "_write_rows", crash_after_clearing)
    with pytest.raises(KeyboardInterrupt):
        _graph(world)                       # graph_rows/ is cleared, the record holds the keys
    monkeypatch.setattr(pipeline, "_write_rows", original)
    _graph(world)
    for table in TABLES:
        assert db.keys(table) == _jsonl_keys(out_dir, table), table


def test_another_target_is_neither_pruned_nor_written_partially(world, out_dir, monkeypatch):
    db = world["db"]
    _graph(world)
    world["delete"](BASE)
    monkeypatch.setattr(config, "SPANNER_DATABASE", "codedoc-go-other")
    db.mutations.clear()
    _graph(world)
    assert db.deletes == {} and not os.path.exists(_prune_path(out_dir))
    assert fid(BASE) in db.keys("Files")                   # the other target keeps its rows
    for table in NODE_ROW_TABLES:                          # full rows, summary included
        assert db.columns[table] == write_columns(table)


def test_no_spanner_writes_and_records_nothing(world, out_dir, monkeypatch):
    _graph(world)
    world["delete"](BASE)
    monkeypatch.setattr(pipeline, "_get_spanner_db",
                        lambda: pytest.fail("--no-spanner must not open Spanner"))
    _graph(world, no_spanner=True)
    assert not os.path.exists(_prune_path(out_dir))


def test_a_target_without_rows_of_the_repo_is_not_pruned(world, out_dir):
    db = world["db"]
    _graph(world)
    world["delete"](BASE)
    db.rows.clear()                                        # e.g. a recreated database
    _graph(world)
    assert db.deletes == {} and not os.path.exists(_prune_path(out_dir))
    assert db.keys("Files") == _jsonl_keys(out_dir, "Files")


def _record(out_dir, target, tables):
    with open(_prune_path(out_dir), "w", encoding="utf-8") as f:
        json.dump({"version": 2, "repo": REPO, "targets": [{"spanner": target, "tables": tables}]},
                  f)


def test_a_leftover_record_of_the_same_target_is_merged(world, out_dir):
    db = world["db"]
    _graph(world)
    stale = pipeline._make_id("method", REPO, "gone/old.go", "Old")
    db.rows.setdefault("Methods", {})[stale] = {"method_id": stale, "repo": REPO}
    _record(out_dir, pipeline._spanner_target(False), {"Methods": [stale]})
    world["delete"](BASE)
    _graph(world)
    assert stale not in db.keys("Methods") and fid(BASE) not in db.keys("Files")


def test_a_leftover_record_of_another_target_waits_for_its_target(world, out_dir, monkeypatch,
                                                                  capsys):
    db = world["db"]
    _graph(world)
    other = {**pipeline._spanner_target(False), "database": "x"}
    keep = pipeline._make_id("method", REPO, "gone/old.go", "Old")
    db.rows.setdefault("Methods", {})[keep] = {"method_id": keep, "repo": REPO}
    _record(out_dir, other, {"Methods": [keep]})
    world["delete"](BASE)
    _graph(world)                                          # prunes this target only
    assert keep in db.keys("Methods") and fid(BASE) not in db.keys("Files")
    with open(_prune_path(out_dir), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["targets"] == [{"spanner": other, "tables": {"Methods": [keep]}}]
    monkeypatch.setattr(config, "SPANNER_DATABASE", "x")   # back on that target
    _graph(world)
    assert keep not in db.keys("Methods") and not os.path.exists(_prune_path(out_dir))


def test_keys_of_another_id_prefix_are_never_recorded_or_deleted(world, out_dir):
    db = world["db"]
    _graph(world)
    foreign = "b_" + "0" * 16                              # a side-by-side graph's row
    db.rows["Files"][foreign] = {"file_id": foreign, "repo": REPO}
    with open(os.path.join(out_dir, "graph_rows", "Files.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"file_id": foreign}) + "\n")
    _record(out_dir, pipeline._spanner_target(False), {"Files": [foreign]})
    world["delete"](BASE)
    _graph(world)
    assert foreign in db.keys("Files") and fid(BASE) not in db.keys("Files")


def _use(monkeypatch, prefix, graph):
    monkeypatch.setattr(config, "ID_PREFIX", prefix)
    monkeypatch.setattr(config, "GRAPH_NAME", graph)


def test_side_by_side_graphs_survive_a_crash_inside_phase_8(world, out_dir, monkeypatch):
    # review probe: graph b's run dies during its node upserts, after graph_rows/
    # holds b's keys; the next run of graph a must not delete b's rows
    db = world["db"]
    _use(monkeypatch, "b", "graph_b")
    _graph(world)
    b_files = {k for k in db.keys("Files") if k.startswith("b_")}
    _use(monkeypatch, "a", "graph_a")
    _graph(world)
    _use(monkeypatch, "b", "graph_b")
    original = pipeline._batch_insert

    def die(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(pipeline, "_batch_insert", die)
    with pytest.raises(KeyboardInterrupt):
        _graph(world)
    monkeypatch.setattr(pipeline, "_batch_insert", original)
    _use(monkeypatch, "a", "graph_a")
    _graph(world)
    assert {k for k in db.keys("Files") if k.startswith("b_")} == b_files


def test_a_no_spanner_run_of_another_prefix_cannot_misdirect_the_next_prune(world, out_dir,
                                                                            monkeypatch):
    db = world["db"]
    _use(monkeypatch, "b", "graph_b")
    _graph(world)
    b_files = {k for k in db.keys("Files") if k.startswith("b_")}
    _use(monkeypatch, "a", "graph_a")
    _graph(world)
    _use(monkeypatch, "b", "graph_b")
    original = pipeline._write_rows

    def die_after_the_jsonl(data, rows, **kw):   # graph_rows/ now holds graph b's keys
        original(data, rows, **kw)
        raise KeyboardInterrupt()
    monkeypatch.setattr(pipeline, "_write_rows", die_after_the_jsonl)
    with pytest.raises(KeyboardInterrupt):
        _graph(world, no_spanner=True)
    monkeypatch.setattr(pipeline, "_write_rows", original)
    _use(monkeypatch, "a", "graph_a")
    _graph(world)
    assert {k for k in db.keys("Files") if k.startswith("b_")} == b_files


def test_an_unreadable_jsonl_at_prune_time_fails_the_run_and_the_next_run_prunes(
        world, out_dir, monkeypatch):
    db = world["db"]
    _graph(world)
    world["delete"](BASE)
    original = pipeline._write_rows

    def write_then_break(data, rows, **kw):
        counts = original(data, rows, **kw)
        if "MethodCalls" in rows:   # Phase 9: a written file becomes unreadable
            with open(os.path.join(out_dir, "graph_rows", "MethodCalls.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write("{not json\n")
        return counts
    monkeypatch.setattr(pipeline, "_write_rows", write_then_break)
    with pytest.raises(pipeline.PruneError, match="graph_rows/MethodCalls.jsonl"):
        _graph(world)
    assert db.deletes == {} and os.path.exists(_prune_path(out_dir))
    monkeypatch.setattr(pipeline, "_write_rows", original)
    _graph(world)                            # resumes at Phase 9 (incomplete checkpoint)
    assert fid(BASE) not in db.keys("Files") and not os.path.exists(_prune_path(out_dir))


def test_a_table_whose_jsonl_is_missing_is_not_pruned(world, out_dir, monkeypatch):
    db = world["db"]
    _graph(world)
    world["delete"](BASE)
    original = pipeline._write_rows

    def drop_one(data, rows, **kw):
        counts = original(data, rows, **kw)
        path = os.path.join(out_dir, "graph_rows", "ClassEmbeds.jsonl")
        if os.path.exists(path):
            os.remove(path)
        return counts
    monkeypatch.setattr(pipeline, "_write_rows", drop_one)
    _graph(world)
    assert "ClassEmbeds" not in db.deletes and "Files" in db.deletes
    with open(_prune_path(out_dir), encoding="utf-8") as f:   # its keys wait for the next run
        kept = json.load(f)["targets"][0]["tables"]
    assert list(kept) == ["ClassEmbeds"]
    monkeypatch.setattr(pipeline, "_write_rows", original)
    with open(os.path.join(world["repo"], FEE), "a", encoding="utf-8") as f:
        f.write("\n// touched\n")                            # new inputs: Phases 8-9 run
    _graph(world)
    assert db.keys("ClassEmbeds") == _jsonl_keys(out_dir, "ClassEmbeds")
    assert not os.path.exists(_prune_path(out_dir))


# ── _batch_delete ──────────────────────────────────────────────────

def test_batch_delete_uses_keysets_within_the_mutation_limit(monkeypatch):
    db = FakeSpannerDb()
    monkeypatch.setattr(config, "SPANNER_BATCH_SIZE", 3)
    pipeline._batch_delete(db, "MethodCalls", [f"k{i}" for i in range(7)])
    assert db.deletes["MethodCalls"] == [[f"k{i}"] for i in range(7)]
    sizes = []

    class _Counting(FakeSpannerDb):
        def batch(self):
            parent = super().batch()

            class _B:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def delete(self, table, keyset):
                    sizes.append(len(keyset.keys))
                    parent.delete(table, keyset)
            return _B()

    pipeline._batch_delete(_Counting(), "MethodCalls", [f"k{i}" for i in range(7)])
    assert sizes == [3, 3, 1]
    monkeypatch.setattr(config, "SPANNER_BATCH_SIZE", 50_000)
    monkeypatch.setattr(pipeline, "_secondary_index_count", lambda table: 3)
    sizes.clear()
    pipeline._batch_delete(_Counting(), "MethodCalls", [f"k{i}" for i in range(25_000)])
    assert sizes == [10_000, 10_000, 5_000]               # 40,000 // (1 + 3 indexes)


def test_batch_delete_retries_transient_errors(instant_sleeps):
    from google.api_core.exceptions import Aborted

    class _Flaky(FakeSpannerDb):
        failures = 1

        def batch(self):
            if _Flaky.failures:
                _Flaky.failures -= 1
                raise Aborted("try again")
            return super().batch()

    db = _Flaky()
    pipeline._batch_delete(db, "Files", ["a", "b"])
    assert db.deletes["Files"] == [["a"], ["b"]] and len(instant_sleeps) == 1


def test_no_secondary_indexes_in_the_schema():
    assert all(pipeline._secondary_index_count(t) == 0 for t in TABLES)


def test_batch_delete_gives_up_after_its_retries(instant_sleeps, out_dir):
    from google.api_core.exceptions import Aborted

    class _Dead(FakeSpannerDb):
        def batch(self):
            raise Aborted("down")

    with pytest.raises(Aborted):
        pipeline._batch_delete(_Dead(), "Files", ["a"], max_retries=3)
    assert len(instant_sleeps) == 2


def test_an_unreadable_previous_jsonl_records_only_the_readable_tables(world, out_dir, capsys):
    db = world["db"]
    _graph(world)
    with open(os.path.join(out_dir, "graph_rows", "Methods.jsonl"), "a", encoding="utf-8") as f:
        f.write("{not json\n")
    world["delete"](BASE)
    capsys.readouterr()
    _graph(world)
    assert "cannot read graph_rows/Methods.jsonl" in capsys.readouterr().out
    assert fid(BASE) not in db.keys("Files")
    assert mid(BASE, "Base.ID") in db.keys("Methods")   # not recorded: fewer deletes, no wrong one
    assert not os.path.exists(_prune_path(out_dir))


def test_an_unreadable_prune_record_is_reported_and_replaced(world, out_dir, capsys):
    db = world["db"]
    _graph(world)
    with open(_prune_path(out_dir), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "repo": REPO, "spanner": {}, "tables": {}}, f)   # old format
    world["delete"](BASE)
    capsys.readouterr()
    _graph(world)
    assert "ignoring an unreadable graph_prune.json" in capsys.readouterr().out
    assert fid(BASE) not in db.keys("Files") and not os.path.exists(_prune_path(out_dir))


def test_an_unreadable_node_jsonl_is_rewritten_by_the_next_run(world, out_dir, monkeypatch):
    # review probe: Phase 9 rewrites only the edge files, so after a PruneError
    # the next run starts at Phase 8 to rewrite the node files too
    db = world["db"]
    _graph(world)
    world["delete"](BASE)
    original = pipeline._write_rows

    def break_methods(data, rows, **kw):
        counts = original(data, rows, **kw)
        if "MethodCalls" in rows:
            with open(os.path.join(out_dir, "graph_rows", "Methods.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write("{not json\n")
        return counts
    monkeypatch.setattr(pipeline, "_write_rows", break_methods)
    with pytest.raises(pipeline.PruneError):
        _graph(world)
    monkeypatch.setattr(pipeline, "_write_rows", original)
    _graph(world)                                          # the same command again
    assert fid(BASE) not in db.keys("Files") and not os.path.exists(_prune_path(out_dir))
    for table in TABLES:
        assert db.keys(table) == _jsonl_keys(out_dir, table), table
