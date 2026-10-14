"""R48 re-ingest pruning together with the five Leiden topic tables, against
the stateful fake Spanner of tests/test_graph_prune.py: after a re-ingest the
Spanner rows equal graph_rows/, topic tables included; a prune that cannot
read its rows back leaves the topic rows alone, and the next run repairs
everything. codedoc_go's re-ingest test is skipped here (tests/leiden_plugin.py)."""
from __future__ import annotations

import json
import os
import re

import pytest

from graph_generator import pipeline
from graph_generator.topics import community
from tests import test_graph_prune as tp
from tests.graph_world import BASE, EXTRA, fid
from tests.test_graph_prune import world  # noqa: F401  (the fixture)

_TOPIC_REFS = ("parent_topic", "child_topic", "source_topic", "target_topic", "topic_id")


class TopicSpanner(tp.SpannerState):
    """SpannerState that also answers topics_phase's key queries: this repo's
    rows of the exact key shape (REGEXP_CONTAINS(<key>, @shape))."""

    def sql_result(self, sql, params):
        shape = (params or {}).get("shape")
        if shape and sql.startswith("SELECT topic_id FROM Topics WHERE repo"):
            return [[k] for k, r in self.rows.get("Topics", {}).items()
                    if r.get("repo") == params["repo"] and re.search(shape, k)]
        if shape and sql.startswith("SELECT edge_id FROM "):
            mine = {t: {k for k, r in self.rows.get(t, {}).items() if r.get("repo") == params["repo"]}
                    for t in ("Topics", "Files", "Classes")}
            return [[k] for k, r in self.rows.get(sql.split()[3], {}).items()
                    if re.search(shape, k) and ({r.get(c) for c in _TOPIC_REFS} & mine["Topics"]
                                                or r.get("file_id") in mine["Files"]
                                                or r.get("class_id") in mine["Classes"])]
        return super().sql_result(sql, params)


@pytest.fixture
def spanner(world, monkeypatch):
    db = TopicSpanner()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    world["db"] = db
    return db


def _topic_links_of(db, file_id: str) -> list[str]:
    return [k for k, r in db.rows.get("FileBelongsToTopic", {}).items() if r.get("file_id") == file_id]


def _matches_graph_rows(db, out_dir, tables) -> dict[str, bool]:
    return {t: db.keys(t) == tp._jsonl_keys(out_dir, t) for t in tables}


def test_reingest_leaves_spanner_equal_to_graph_rows_topic_tables_included(world, spanner, out_dir):
    tp._graph(world)
    assert spanner.keys("Topics") and spanner.keys("ClassBelongsToTopic")
    assert all(_matches_graph_rows(spanner, out_dir, community.TOPIC_TABLES).values())
    assert _topic_links_of(spanner, fid(BASE))
    world["edits"]["drop_calls"].add((EXTRA, "mk", 10))
    world["delete"](BASE)
    with open(os.path.join(world["repo"], EXTRA), "a", encoding="utf-8") as f:
        f.write("\nfunc extra() {}\n")
    tp._graph(world)
    assert fid(BASE) not in spanner.keys("Files") and not _topic_links_of(spanner, fid(BASE))
    assert all(_matches_graph_rows(spanner, out_dir, tp.TABLES).values())
    assert all(_matches_graph_rows(spanner, out_dir, community.TOPIC_TABLES).values())
    assert not set(community.TOPIC_TABLES) & set(pipeline._pruned_tables())  # replaced whole, never pruned


def test_a_failed_prune_leaves_the_topic_rows_and_the_next_run_repairs(world, spanner, out_dir, monkeypatch):
    tp._graph(world)
    before = {t: dict(spanner.rows.get(t, {})) for t in community.TOPIC_TABLES}
    world["delete"](BASE)
    original = pipeline._write_rows

    def write_then_break(data, rows, **kw):
        counts = original(data, rows, **kw)
        if "MethodCalls" in rows:  # graph_rows/ can no longer be read back for the prune
            with open(os.path.join(out_dir, "graph_rows", "MethodCalls.jsonl"), "a", encoding="utf-8") as f:
                f.write("{not json\n")
        return counts

    monkeypatch.setattr(pipeline, "_write_rows", write_then_break)
    with pytest.raises(pipeline.PruneError):
        tp._graph(world)
    assert {t: dict(spanner.rows.get(t, {})) for t in community.TOPIC_TABLES} == before
    with open(os.path.join(out_dir, "graph_checkpoint.json"), encoding="utf-8") as f:
        checkpoint = json.load(f)
    assert (checkpoint["complete"], checkpoint["phase"]) == (False, "phase1.6")
    monkeypatch.setattr(pipeline, "_write_rows", original)
    tp._graph(world)
    assert fid(BASE) not in spanner.keys("Files")
    assert all(_matches_graph_rows(spanner, out_dir, (*tp.TABLES, *community.TOPIC_TABLES)).values())
