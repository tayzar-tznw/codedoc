"""The graph track of codedoc_go_leiden (leiden spec §2, §3, §4, L-R8, L-R24,
R10, R20): Phase 4a after Phase 1.6 and before Phase 8, communities.json and
its hash in the checkpoint, the five topic tables after Phase 9, and 4a
failures that stop 4a but never the graph track."""
from __future__ import annotations

import json
import os

import pytest

from graph_generator import __main__ as cli
from graph_generator import config, pipeline, topics_phase
from graph_generator.topics import community
from tests.leiden_worlds import (REPO, TopicDb, graph_data, install_leiden_graph, read_jsonl,
                                 topics_file)
from tests.pipeline_fakes import recorder


@pytest.fixture
def world(monkeypatch, tmp_path, out_dir):
    target, calls = install_leiden_graph(monkeypatch, tmp_path)
    return target, calls


def _checkpoint(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "graph_checkpoint.json"), encoding="utf-8") as f:
        return json.load(f)


def test_phase_4a_runs_after_resolution_and_the_topic_tables_after_phase_9(world, monkeypatch):
    target, calls = world
    for name in ("phase8_write_nodes", "phase9_write_edges"):
        monkeypatch.setattr(pipeline, name, recorder(calls, name, getattr(pipeline, name)))
    for name in ("phase4a_communities", "write_graph_topics"):
        monkeypatch.setattr(topics_phase, name, recorder(calls, name, getattr(topics_phase, name)))
    pipeline.run_graph_track(graph_data(target))
    order = [c for c in calls if c in ("phase1c_resolve", "phase4a_communities", "phase8_write_nodes",
                                      "phase9_write_edges", "write_graph_topics")]
    assert order == ["phase1c_resolve", "phase4a_communities", "phase8_write_nodes",
                     "phase9_write_edges", "write_graph_topics"]


def test_4a_writes_its_outputs_and_the_checkpoint_stores_their_hash(world, out_dir, capsys):
    target, _calls = world
    data = graph_data(target)
    pipeline.run_graph_track(data)
    doc = topics_file(out_dir, "communities.json")
    assert doc["repo"] == REPO and doc["id_prefix"] == config.ID_PREFIX
    assert doc["backend"] == {"name": "graspologic-native", "version": "1.3.1", "function": "leiden"}
    assert os.path.getsize(os.path.join(out_dir, "topics", "projection.jsonl")) > 0
    cp = _checkpoint(out_dir)
    assert cp["complete"] and cp["communities_hash"] == topics_phase.document_hash(doc)
    assert "phase4a_communities" in data.timings and "phase9_write_topics" in data.timings
    out = capsys.readouterr().out
    assert "[Phase 4a] " in out and "topics" in out and "subtopics" in out


def test_a_rerun_on_the_same_input_resumes_although_names_carry_over(world, out_dir, capsys):
    target, _calls = world
    pipeline.run_graph_track(graph_data(target))
    first = topics_file(out_dir, "communities.json")
    capsys.readouterr()
    pipeline.run_graph_track(graph_data(target))
    second = topics_file(out_dir, "communities.json")
    out = capsys.readouterr().out
    assert "Skipping Phase 8" in out and "Skipping Phase 9" in out
    assert second["carry_over"] and not first["carry_over"]
    assert second["membership_hash"] == first["membership_hash"]
    assert topics_phase.document_hash(second) == topics_phase.document_hash(first)


def test_changed_communities_rewrite_from_phase_8(world, out_dir, monkeypatch, capsys):
    target, _calls = world
    pipeline.run_graph_track(graph_data(target))
    before = _checkpoint(out_dir)["communities_hash"]
    monkeypatch.setattr(config, "SUBTOPIC_MIN_ATOMS", 10_000)  # no subtopics
    capsys.readouterr()
    pipeline.run_graph_track(graph_data(target))
    out = capsys.readouterr().out
    assert "rewriting nodes and edges from Phase 8" in out
    assert "[Phase 8] Wrote" in out and "[Phase 9] Wrote" in out
    assert _checkpoint(out_dir)["communities_hash"] not in (None, before)
    assert read_jsonl(out_dir, "TopicContainsTopic") == []


def _assert_stopped(out_dir: str, capsys, needle: str) -> None:
    out = capsys.readouterr().out
    assert "[Phase 4a] Stopped:" in out and needle in out
    assert "[Phase 9] Wrote" in out  # the graph track went on
    cp = _checkpoint(out_dir)
    assert cp["complete"] and cp["communities_hash"] is None
    for table in community.TOPIC_TABLES:
        assert read_jsonl(out_dir, table) == []
    with open(os.path.join(out_dir, "error_log.txt"), encoding="utf-8") as f:
        assert needle in f.read()


def test_a_backend_panic_stops_4a_but_not_the_graph_track(world, out_dir, monkeypatch, capsys):
    """PanicException derives from BaseException (L-R8); detect turns it into
    CommunityError."""
    class PanicException(BaseException):
        pass

    class Backend:
        @staticmethod
        def leiden(*args, **kwargs):
            raise PanicException("called `Option::unwrap()` on a `None` value")

    monkeypatch.setattr(community, "_backend", lambda: Backend)
    target, _calls = world
    pipeline.run_graph_track(graph_data(target))
    _assert_stopped(out_dir, capsys, "PanicException")


def test_a_missing_backend_stops_4a(world, out_dir, monkeypatch, capsys):
    def missing():
        raise community.BackendMissingError(community.INSTALL_HINT)

    monkeypatch.setattr(community, "_backend", missing)
    target, _calls = world
    pipeline.run_graph_track(graph_data(target))
    _assert_stopped(out_dir, capsys, "graspologic-native is not installed")


def test_integrity_problems_stop_4a(world, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(community, "check_integrity", lambda c, p: ["level 1: atom x appears 2 times"])
    target, _calls = world
    pipeline.run_graph_track(graph_data(target))
    _assert_stopped(out_dir, capsys, "atom x appears 2 times")


def test_a_bad_topic_setting_stops_4a(world, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "TOPIC_SEEDS", "42,x")
    target, _calls = world
    pipeline.run_graph_track(graph_data(target))
    _assert_stopped(out_dir, capsys, "TOPIC_SEEDS")


def _key(prefix: str, i: int) -> str:
    return f"{prefix}_{i:016x}"


def test_a_stopped_4a_empties_the_repos_topic_rows_in_spanner(world, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(community, "check_integrity", lambda c, p: ["broken"])
    db = TopicDb(keys={"Topics": [_key("a", 9)], "ClassBelongsToTopic": [_key("a", 1), _key("a", 2)]})
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    target, _calls = world
    pipeline.run_graph_track(graph_data(target, no_spanner=False))
    assert db.deletes == {"Topics": [[_key("a", 9)]], "ClassBelongsToTopic": [[_key("a", 1)], [_key("a", 2)]]}
    assert not any(op == "insert_or_update" and table in community.TOPIC_TABLES
                   for op, table, _n in db.ops)


def test_topic_tables_go_to_graph_rows(world, out_dir):
    target, _calls = world
    data = graph_data(target)
    pipeline.run_graph_track(data)
    rows = {table: read_jsonl(out_dir, table) for table in community.TOPIC_TABLES}
    assert all(rows.values())
    assert {t: data.graph_counts[t] for t in community.TOPIC_TABLES} == {t: len(r) for t, r in rows.items()}
    sources = {r["label_source"] for r in rows["Topics"]}
    assert sources == {"hub", "fixed"}  # no --wiki yet; 「未分類」 is fixed
    unassigned = [r for r in rows["Topics"] if r["label_source"] == "fixed"]
    assert [r["name"] for r in unassigned] == [community.UNASSIGNED_NAME]
    primaries: dict[str, int] = {}
    for r in rows["FileBelongsToTopic"]:
        assert r["role"] in ("primary", "secondary")
        primaries[r["file_id"]] = primaries.get(r["file_id"], 0) + (r["role"] == "primary")
    assert set(primaries.values()) == {1}


def test_spanner_topic_tables_are_deleted_then_inserted_in_batches(world, out_dir, monkeypatch, capsys):
    side_by_side = [_key("a_x", 1), _key("a_x", 2)]  # ID_PREFIX "a_x", whose keys STARTS_WITH "a_" too
    db = TopicDb(keys={"ClassBelongsToTopic": [_key("a", i) for i in range(4500)] + side_by_side,
                       "TopicDependsOn": [_key("a", 1)], "Topics": [_key("a", 1), _key("a", 2)] + side_by_side})
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    monkeypatch.setattr(topics_phase, "_TOPIC_BATCH", 50)
    target, _calls = world
    pipeline.run_graph_track(graph_data(target, no_spanner=False))
    topic_ops = [op for op in db.ops if op[1] in community.TOPIC_TABLES]
    deletes = [op for op in topic_ops if op[0] == "delete"]
    inserts = [op for op in topic_ops if op[0] == "insert_or_update"]
    assert topic_ops.index(deletes[-1]) < topic_ops.index(inserts[0])  # every delete first
    assert [t for _op, t, _n in deletes] == ["TopicDependsOn"] + ["ClassBelongsToTopic"] * 90 + ["Topics"]
    assert all(n <= 50 for _op, _t, n in deletes + inserts)
    assert inserts[0][1] == "Topics"  # nodes before their edges
    written = {t: sum(n for _op, table, n in inserts if table == t) for t in community.TOPIC_TABLES}
    assert written == {t: len(read_jsonl(out_dir, t)) for t in community.TOPIC_TABLES}
    sqls = [sql for sql, params in db.queries if params.get("shape")]
    assert len(sqls) == 5 and all("REGEXP_CONTAINS" in sql and "@repo" in sql for sql in sqls)
    assert {json.dumps(p, sort_keys=True) for _s, p in db.queries if p.get("shape")} == {
        json.dumps({"shape": "^a_[0-9a-f]{16}$", "repo": REPO}, sort_keys=True)}
    assert not set(side_by_side) & {key for keys in db.deletes.values() for (key,) in keys}
    assert db.columns["Topics"] == pipeline._columns("Topics")
    assert "not atomic" in capsys.readouterr().out


def test_unchanged_topics_keep_their_wiki_name_summary_and_cached_embedding(world, out_dir, monkeypatch):
    target, _calls = world
    pipeline.run_graph_track(graph_data(target))
    topics = {r["topic_id"]: r for r in read_jsonl(out_dir, "Topics") if r["label_source"] == "hub"}
    named, other = sorted(topics)[:2], sorted(topics)[2]
    names = {"version": 1, "names": {
        named[0]: {"name": "診療報酬計算", "label_source": "llm"},
        named[1]: {"name": "患者受付", "label_source": "inherited", "inherited_from": named[1]},
        other: {"name": "ハブの名前 (2)", "label_source": "hub"},  # 4b's deduplicated fallback
        "a_0000000000000000": {"name": "消えたトピック", "label_source": "llm"}}}
    with open(os.path.join(out_dir, "topics", "names.json"), "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False)
    page = "# 診療報酬計算\n\n## はじめに\n点数から料金を計算します。\n"
    hub_page = "# ハブの名前 (2)\n\n## はじめに\nハブの説明です。\n"
    for tid, text in ((named[0], page), (other, hub_page)):
        with open(os.path.join(out_dir, "topics", f"{tid}.md"), "w", encoding="utf-8") as f:
            f.write(text)
    cache = pipeline._EmbeddingCache(os.path.join(out_dir, "embeddings", "cache.sqlite"))
    cache.put_many([(pipeline._EmbeddingCache.key(f"診療報酬計算: {page}"), [0.5, 0.25]),
                    (pipeline._EmbeddingCache.key(f"ハブの名前 (2): {hub_page}"), [0.125, 1.0])])
    cache.close()
    db = TopicDb()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    pipeline.run_graph_track(graph_data(target, no_spanner=False))  # the Spanner target changed
    rows = {r["topic_id"]: r for r in read_jsonl(out_dir, "Topics")}
    assert (rows[named[0]]["name"], rows[named[0]]["label_source"]) == ("診療報酬計算", "llm")
    assert (rows[named[1]]["name"], rows[named[1]]["label_source"]) == ("患者受付", "inherited")
    assert (rows[other]["name"], rows[other]["label_source"]) == ("ハブの名前 (2)", "hub")
    assert rows[named[0]]["summary"] == page and rows[named[1]]["summary"] == ""
    assert rows[other]["summary"] == hub_page
    assert sorted(db.updates["Topics"]) == sorted([[named[0], [0.5, 0.25]], [other, [0.125, 1.0]]])


def test_three_runs_give_the_same_membership_hash(world, out_dir):
    target, _calls = world
    hashes = set()
    for _ in range(3):
        pipeline.run_graph_track(graph_data(target))
        doc = topics_file(out_dir, "communities.json")
        hashes.add((doc["membership_hash"], topics_phase.document_hash(doc)))
    assert len(hashes) == 1


def test_the_timing_report_names_the_leiden_phases():
    keys = [key for key, _label in cli._PHASE_LABELS]
    assert keys.index("phase1c_resolve") + 1 == keys.index("phase4a_communities")
    assert keys.index("phase9_write_edges") + 1 == keys.index("phase9_write_topics")
    assert "phase4b_naming" in keys and "phase4_topics" not in keys
    assert dict(cli._PHASE_LABELS)["phase5_topic_pages"] == "Phase 5: Topic reports"
