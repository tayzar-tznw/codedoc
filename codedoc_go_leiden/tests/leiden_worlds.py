"""Worlds for the codedoc_go_leiden tests.

* `install_leiden_graph` runs the graph track on synthetic C6 rows: scan,
  discovery and Phases 1.5/1.6 are pipeline_fakes', the builders return
  `topics.synthetic.synthetic_repo` rows, and Spanner writes use the real
  column lists.
* `TopicDb` is a FakeSpannerDb that logs every batch commit in order and
  answers the topic key queries.
* `leiden_responder` answers the 4b naming prompt with unique names and the
  Phase 5 prompt with a page that cites one real and one invented identifier.
"""
from __future__ import annotations

import json
import os
import re

from graph_generator import pipeline, setup_spanner_graph
from graph_generator.topics import synthetic
from tests.conftest import SAMPLE_TREE, FakeSpannerDb, write_tree
from tests.pipeline_fakes import install_graph_fakes

REPO = "iryo"


def synthetic_rows(n_atoms: int = 120, seed: int = 0) -> tuple[dict, dict]:
    nodes, edges, _features = synthetic.synthetic_repo(n_atoms=n_atoms, seed=seed)
    return nodes, edges


def install_leiden_graph(monkeypatch, tmp_path, n_atoms: int = 120, seed: int = 0,
                         calls: list | None = None) -> tuple[str, list]:
    """A repo dir and the graph track's fakes with synthetic rows; returns
    (target dir, call log)."""
    calls = install_graph_fakes(monkeypatch, calls)
    nodes, edges = synthetic_rows(n_atoms, seed)

    def build_node_rows(data):
        calls.append("build_node_rows")
        return {table: [dict(r) for r in rows] for table, rows in nodes.items()}

    def derive_edge_rows(data, node_index):
        calls.append("derive_edge_rows")
        return {table: [dict(r) for r in rows] for table, rows in edges.items()}

    monkeypatch.setattr(pipeline, "build_node_rows", build_node_rows)
    monkeypatch.setattr(pipeline, "derive_edge_rows", derive_edge_rows)
    monkeypatch.setattr(pipeline, "_node_index", lambda data: {})
    monkeypatch.setattr(pipeline, "_columns", setup_spanner_graph.write_columns)
    target = write_tree(tmp_path / REPO, SAMPLE_TREE)
    return target, calls


def graph_data(target: str, no_spanner: bool = True) -> pipeline.PipelineData:
    return pipeline.PipelineData(target_dir=target, repo=REPO, no_spanner=no_spanner)


def read_jsonl(out_dir: str, table: str) -> list[dict]:
    path = os.path.join(out_dir, "graph_rows", f"{table}.jsonl")
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def topics_file(out_dir: str, name: str):
    path = os.path.join(out_dir, "topics", name)
    with open(path, encoding="utf-8") as f:
        return json.load(f) if name.endswith(".json") else f.read()


class _LogBatch:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def insert_or_update(self, table, columns, values):
        self._db.ops.append(("insert_or_update", table, len(values)))
        self._db.mutations.setdefault(table, []).extend(values)
        self._db.columns[table] = columns

    def update(self, table, columns, values):
        self._db.ops.append(("update", table, len(values)))
        self._db.updates.setdefault(table, []).extend(values)

    def delete(self, table, keyset):
        keys = list(keyset.keys)
        self._db.ops.append(("delete", table, len(keys)))
        self._db.deletes.setdefault(table, []).extend(keys)


class TopicDb(FakeSpannerDb):
    """Logs batch commits as (op, table, rows) in order; a delete query of a
    topic table returns the keys of `keys[table]` its `@shape` matches (as
    REGEXP_CONTAINS does); every query is kept with its params."""

    def __init__(self, keys: dict[str, list[str]] | None = None):
        super().__init__()
        self.ops: list[tuple[str, str, int]] = []
        self.queries: list[tuple[str, dict]] = []
        self.keys = keys or {}

    def batch(self):
        return _LogBatch(self)

    def sql_result(self, sql, params):
        self.queries.append((sql, dict(params or {})))
        m = re.match(r"SELECT (\w+) FROM (\w+) ", sql)
        if m and m.group(2) in self.keys:
            shape = (params or {}).get("shape")
            return [[k] for k in self.keys[m.group(2)] if shape is None or re.search(shape, k)]
        return []


def install_wiki_world(monkeypatch) -> None:
    """Phases 2 and 3 without Gemini: every Classes row of graph_rows/ gets a
    node summary and every file a file summary. Phases 4b-10 stay real."""
    async def phase2(data, client):
        rows = pipeline._graph_rows(data)
        path_of = {r["file_id"]: r["path"] for r in rows.get("Files", [])}
        data.file_summaries = {p: f"{p} のファイル要約。" for p in path_of.values()}
        data.node_summaries = {}
        for r in rows.get("Classes", []):
            rel = path_of[r["file_id"]]
            data.node_summaries.setdefault(rel, {})[r["qualname"]] = f"{r['qualname']} の要約。"

    async def phase3(data, client):
        data.dir_summaries = {"": "リポジトリ全体の要約です。"}

    monkeypatch.setattr(pipeline, "phase2_node_summaries", phase2)
    monkeypatch.setattr(pipeline, "phase3_package_dir_summaries", phase3)


def run_wiki(target: str, client, no_spanner: bool = True) -> pipeline.PipelineData:
    """load_graph_state + prepare_wiki + run_wiki_track, as `analyze --wiki` does."""
    import asyncio
    data = pipeline.load_graph_state(target, REPO)
    pipeline.prepare_wiki(data, no_spanner=no_spanner)
    asyncio.run(pipeline.run_wiki_track(data, client))
    return data


_NAMING = "各トピックに日本語の名前を付けてください"


def leiden_responder(model, contents, config) -> str:
    """4b: `{topic_id: "<last 6 hex digits> の処理"}` for the topic IDs in the
    prompt (never a placeholder, even when the digits are all numbers). Phase 5:
    a page with a heading, an intro naming the topic, one identifier from the
    evidence and one invented identifier."""
    text = contents if isinstance(contents, str) else str(contents)
    if _NAMING in text:
        ids = re.findall(r"^#### (\S+)$", text, re.MULTILINE)
        return json.dumps({tid: f"{tid[-6:]} の処理" for tid in ids}, ensure_ascii=False)
    name = re.search(r"^# (.+)$", text, re.MULTILINE).group(1)
    hub = re.search(r"^- ハブ: `([^`]+)`", text, re.MULTILINE)
    real = hub.group(1) if hub else "x"
    return (f"# {name}\n\n## はじめに\n{name} はこのトピックの機能です。\n\n"
            f"## 主要コンポーネント\n- `{real}` が中心です。\n- `NoSuchThing` は存在しません。\n")
