"""Phase 10's topic write (R10, final review #2 and #3): only this repo's
topic rows of this ID_PREFIX's exact key shape are replaced, through bounded
commits, links before topics on delete and after them on insert."""
from __future__ import annotations

import pytest

from graph_generator import config, pipeline
from graph_generator.pipeline import _edge_id, _make_id
from tests.conftest import FakeSpannerDb
from tests.test_graph_prune import SpannerState

REPO = "iryo"
LIMIT = 80_000   # Spanner mutations per commit (cloud.google.com/spanner/quotas)


def topic(name, repo=REPO):
    return {"topic_id": _make_id("topic", repo, name, ""), "name": name, "summary": f"{name}。",
            "repo": repo}


def link(file_rel, topic_row):
    fid = _make_id("file", REPO, file_rel, "")
    return {"edge_id": _edge_id("FileBelongsToTopic", fid, topic_row["topic_id"]),
            "file_id": fid, "topic_id": topic_row["topic_id"]}


def _seed(db, topics, links):
    for t in topics:
        db.rows.setdefault("Topics", {})[t["topic_id"]] = dict(t)
    for row in links:
        db.rows.setdefault("FileBelongsToTopic", {})[row["edge_id"]] = dict(row)
        db.rows.setdefault("Files", {}).setdefault(row["file_id"],
                                                   {"file_id": row["file_id"], "repo": REPO})


def test_the_new_topics_replace_the_old_ones():
    db = SpannerState()
    old_a, kept = topic("料金"), topic("受付")
    _seed(db, [old_a, kept], [link("a.go", old_a), link("b.go", kept)])
    new = topic("会計")
    pipeline._replace_topics(db, REPO, [kept, new], [link("b.go", kept), link("c.go", new)])
    assert db.keys("Topics") == {kept["topic_id"], new["topic_id"]}
    assert db.keys("FileBelongsToTopic") == {link("b.go", kept)["edge_id"],
                                            link("c.go", new)["edge_id"]}
    assert db.transactions == 0   # no single transaction holds the whole write


def test_a_side_by_side_graphs_topic_rows_survive(monkeypatch):
    # review probe: ID_PREFIX "a" must not delete the keys "a_x_<16 hex>" of
    # a graph whose ID_PREFIX is "a_x"
    db = SpannerState()
    monkeypatch.setattr(config, "ID_PREFIX", "a_x")
    foreign_topic = topic("料金")
    foreign_link = link("a.go", foreign_topic)
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    ours = topic("料金")
    _seed(db, [foreign_topic, ours], [foreign_link, link("a.go", ours)])
    assert foreign_topic["topic_id"].startswith("a_x_") and ours["topic_id"].startswith("a_")
    pipeline._replace_topics(db, REPO, [], [])
    assert db.keys("Topics") == {foreign_topic["topic_id"]}
    assert db.keys("FileBelongsToTopic") == {foreign_link["edge_id"]}


def test_another_repos_topic_rows_survive():
    db = SpannerState()
    other = topic("料金", repo="web")
    other_link = {"edge_id": _edge_id("FileBelongsToTopic", _make_id("file", "web", "w.go", ""),
                                      other["topic_id"]),
                  "file_id": _make_id("file", "web", "w.go", ""), "topic_id": other["topic_id"]}
    db.rows["Topics"] = {other["topic_id"]: dict(other)}
    db.rows["FileBelongsToTopic"] = {other_link["edge_id"]: dict(other_link)}
    db.rows["Files"] = {other_link["file_id"]: {"file_id": other_link["file_id"], "repo": "web"}}
    pipeline._replace_topics(db, REPO, [topic("受付")], [])
    assert other["topic_id"] in db.keys("Topics") and other_link["edge_id"] in db.keys(
        "FileBelongsToTopic")


class _CountingDb(SpannerState):
    """Counts the mutations of every commit (an insert or update once per
    column, a deleted row once) and logs the operations in order."""

    def __init__(self):
        super().__init__()
        self.commits: list[int] = []
        self.log: list[tuple[str, str]] = []

    def batch(self):
        db, inner = self, super().batch()

        class _Batch:
            def __enter__(self):
                db.commits.append(0)
                return self

            def __exit__(self, *exc):
                return False

            def insert_or_update(self, table, columns, values):
                db.commits[-1] += len(columns) * len(values)
                db.log.append(("upsert", table))
                inner.insert_or_update(table, columns, values)

            def update(self, table, columns, values):
                db.commits[-1] += len(columns) * len(values)
                inner.update(table, columns, values)

            def delete(self, table, keyset):
                db.commits[-1] += len(keyset.keys)
                db.log.append(("delete", table))
                inner.delete(table, keyset)

        return _Batch()


def test_a_large_topic_write_stays_within_the_mutation_limit():
    db = _CountingDb()
    old = [topic(f"旧{i}") for i in range(15)]
    old_links = [link(f"f{i}.go", old[i % 15]) for i in range(30_000)]
    _seed(db, old, old_links)
    new = [topic(f"新{i}") for i in range(15)]
    new_links = [link(f"f{i}.go", new[i % 15]) for i in range(30_000)]
    pipeline._replace_topics(db, REPO, new, new_links)
    assert max(db.commits) <= LIMIT // 2 < LIMIT
    assert len(db.commits) > 2
    assert db.keys("Topics") == {t["topic_id"] for t in new}
    assert db.keys("FileBelongsToTopic") == {r["edge_id"] for r in new_links}
    order = [entry for i, entry in enumerate(db.log) if entry not in db.log[:i]]
    assert order == [("delete", "FileBelongsToTopic"), ("delete", "Topics"),
                     ("upsert", "Topics"), ("upsert", "FileBelongsToTopic")]


def test_an_interrupted_write_leaves_no_link_to_a_missing_topic_and_a_rerun_repairs_it(
        monkeypatch):
    db = SpannerState()
    old = topic("料金")
    _seed(db, [old], [link("a.go", old)])
    new = topic("会計")
    original = pipeline._batch_insert

    def fail_on_links(db_, table, columns, rows, max_retries=5):
        if table == "FileBelongsToTopic":
            raise KeyboardInterrupt()
        return original(db_, table, columns, rows, max_retries)
    monkeypatch.setattr(pipeline, "_batch_insert", fail_on_links)
    with pytest.raises(KeyboardInterrupt):
        pipeline._replace_topics(db, REPO, [new], [link("a.go", new)])
    topics = db.keys("Topics")
    assert all(r["topic_id"] in topics for r in db.rows.get("FileBelongsToTopic", {}).values())
    monkeypatch.setattr(pipeline, "_batch_insert", original)
    pipeline._replace_topics(db, REPO, [new], [link("a.go", new)])   # the next --wiki
    assert db.keys("Topics") == {new["topic_id"]}
    assert db.keys("FileBelongsToTopic") == {link("a.go", new)["edge_id"]}


def test_the_delete_queries_pass_the_exact_key_shape():
    seen = []

    class _Recording(FakeSpannerDb):
        def sql_result(self, sql, params):
            seen.append((sql, dict(params or {})))
            return []

    pipeline._replace_topics(_Recording(), REPO, [], [])
    assert len(seen) == 2 and all("REGEXP_CONTAINS" in sql for sql, _p in seen)
    assert all(p["shape"] == rf"^{config.ID_PREFIX}_[0-9a-f]{{16}}$" and p["repo"] == REPO
               for _sql, p in seen)
    assert not any("STARTS_WITH" in sql for sql, _p in seen)
