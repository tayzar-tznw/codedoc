"""Wiki track above the node summaries: Phase 3 package and directory summaries
keyed by child hashes (C7), Phases 4-5 rebuilt only when a summary changed
with topics/stats.json (C7), and the Phase 10 write-back (spec §7.5, R10)."""

import asyncio
import json
import os

import pytest

from graph_generator import config, node_summaries, pipeline
from tests.conftest import FakeGenaiClient, _FakeResponse, default_responder
from tests.wiki_worlds import (BILLING, DOC, FEE, GEN, MAIN, SERVICE, build, cid, fid, mid,
                               wiki_responder)


@pytest.fixture
def world(tmp_path, out_dir, monkeypatch):
    data = build(tmp_path, monkeypatch)
    asyncio.run(node_summaries.summarize(data, FakeGenaiClient(responder=wiki_responder()),
                                         pipeline._graph_rows(data)))
    return data


def _json(out_dir, *parts):
    with open(os.path.join(out_dir, *parts), encoding="utf-8") as f:
        return json.load(f)


# ── Phase 3 ──────────────────────────────────────────────────────

def test_packages_and_dirs_are_summarized_and_cached(world, out_dir):
    record = []
    asyncio.run(pipeline.phase3_package_dir_summaries(
        world, FakeGenaiClient(responder=wiki_responder(record))))
    assert set(world.package_summaries) == {"libs/billing|billing", "cmd/reception|main",
                                            "libs/kernel|kernel"}
    assert set(world.dir_summaries) == {"", "cmd", "cmd/reception", "libs", "libs/billing",
                                        "libs/kernel"}
    billing = next(p for p in record if f"Go パッケージ {BILLING}" in p)
    assert f"**{GEN}**: 生成コード（protoc-gen-go）。" in billing and f"**{FEE}**" in billing
    packages = _json(out_dir, "summaries", "packages.json")
    assert packages["libs/billing|billing"]["summary"] == "パッケージの要約"
    assert len(packages["libs/billing|billing"]["hash"]) == 64
    assert _json(out_dir, "summaries", "dirs.json")[""]["summary"] == "ディレクトリの要約"

    record.clear()
    asyncio.run(pipeline.phase3_package_dir_summaries(
        world, FakeGenaiClient(responder=wiki_responder(record))))
    assert record == []  # no child changed

    # A changed file summary reaches its package and every directory above it
    # (each answer depends on its input here, so parents see a new child).
    def by_input(model, contents, config):
        record.append(contents)
        return f"{wiki_responder()(model, contents, config)} {hash(contents) % 1000}"
    world.file_summaries[DOC] = "別の要約"
    asyncio.run(pipeline.phase3_package_dir_summaries(world, FakeGenaiClient(responder=by_input)))
    names = sorted(p.split("ディレクトリ: ", 1)[1].split("\n", 1)[0] if "ディレクトリ: " in p
                   else p.split("Go パッケージ ", 1)[1].split("（", 1)[0] for p in record)
    assert names == [".", "example.com/iryo/libs/kernel", "libs", "libs/kernel"]


def test_dirs_go_bottom_up(world, out_dir):
    order = []

    def responder(model, contents, config):
        if "ディレクトリ: " in contents:
            order.append(contents.split("ディレクトリ: ", 1)[1].split("\n", 1)[0])
        return wiki_responder()(model, contents, config)
    asyncio.run(pipeline.phase3_package_dir_summaries(world, FakeGenaiClient(responder=responder)))
    assert order.index("libs/billing") < order.index("libs") < order.index(".")


def test_every_wiki_phase_records_its_gemini_usage(tmp_path, out_dir, monkeypatch):
    """R49: Phases 2-5 put calls and input, output and thinking tokens into
    data.gemini_usage, the timing report's per-phase Gemini block."""
    from types import SimpleNamespace
    data = build(tmp_path, monkeypatch)

    def counted(model, contents, config):
        answer = _FakeResponse(wiki_responder()(model, contents, config))
        answer.usage_metadata = SimpleNamespace(prompt_token_count=10, candidates_token_count=2,
                                                thoughts_token_count=5)
        return answer
    client = FakeGenaiClient(responder=counted)
    for phase in (pipeline.phase2_node_summaries, pipeline.phase3_package_dir_summaries,
                  pipeline.phase4_topics, pipeline.phase5_topic_pages):
        asyncio.run(phase(data, client))
    calls = {"phase2_node_summaries": 4, "phase3_package_dir_summaries": 9, "phase4_topics": 2,
             "phase5_topic_pages": 1}
    assert data.gemini_usage == {
        phase: {"calls": n, "input_tokens": 10 * n, "output_tokens": 2 * n, "thinking_tokens": 5 * n}
        for phase, n in calls.items()}
    asyncio.run(pipeline.phase4_topics(data, client))  # reused: no call this time
    assert data.gemini_usage["phase4_topics"]["calls"] == 0


def test_phase3_thinks_low_and_pages_keep_the_default(world, out_dir):
    """R49 (lead's decision): package and directory summaries get thinking
    level LOW; topic pages, the main wiki artifact, keep the model's default."""
    from google.genai import types
    levels: dict[str, set] = {}

    def respond(model, contents, config):
        kind = "phase3" if ("Go パッケージ" in contents or "子ファイルの要約" in contents) else "page"
        levels.setdefault(kind, set()).add(config.thinking_config.thinking_level
                                           if config.thinking_config else None)
        return wiki_responder()(model, contents, config)
    asyncio.run(pipeline.phase3_package_dir_summaries(world, FakeGenaiClient(responder=respond)))
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient()))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient(responder=respond)))
    assert levels == {"phase3": {types.ThinkingLevel.LOW}, "page": {None}}


def test_phase3_reports_its_tokens(world, out_dir, capsys):
    client = FakeGenaiClient(responder=lambda m, c, cfg: _FakeResponse(
        wiki_responder()(m, c, cfg), usage=(100, 20)))
    asyncio.run(pipeline.phase3_package_dir_summaries(world, client))
    assert "9 Gemini calls (900 input, 180 output, 0 thinking tokens)" in capsys.readouterr().out


def test_failed_package_is_retried_after_a_cooldown(world, out_dir, instant_sleeps):
    left = {"n": 1}

    def responder(model, contents, config):
        if "Go パッケージ example.com/iryo/libs/kernel" in contents and left["n"]:
            left["n"] -= 1
            return ValueError("503 unavailable")
        return wiki_responder()(model, contents, config)
    asyncio.run(pipeline.phase3_package_dir_summaries(world, FakeGenaiClient(responder=responder)))
    assert world.package_summaries["libs/kernel|kernel"] == "パッケージの要約"
    assert 30 in instant_sleeps


# ── Phases 4-5: stats and rebuilds ───────────────────────────────

def _usage_responder(record=None):
    def respond(model, contents, config):
        if record is not None:
            record.append(contents)
        return _FakeResponse(default_responder(model, contents, config), usage=(100, 20))
    return respond


def test_topic_stats_and_rebuild_only_on_change(world, out_dir):
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient(responder=_usage_responder())))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient(responder=_usage_responder())))
    stats = _json(out_dir, "topics", "stats.json")
    assert set(stats) == {"discovery", "pages"}
    chunks = 2  # cmd/ and libs/
    assert stats["discovery"]["calls"] == chunks and stats["discovery"]["input_tokens"] == 100 * chunks
    assert stats["discovery"]["output_tokens"] == 20 * chunks and stats["discovery"]["seconds"] >= 0
    assert stats["pages"]["calls"] == len(world.topics) == 1

    record = []
    world.topics, world.topic_summaries = [], {}
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient(responder=_usage_responder(record))))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient(responder=_usage_responder(record))))
    assert record == [] and len(world.topics) == 1 and world.topic_summaries  # reused from disk
    assert _json(out_dir, "topics", "stats.json") == stats  # no call: earlier numbers kept

    world.file_summaries[DOC] = "変更された要約"
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient(responder=_usage_responder(record))))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient(responder=_usage_responder(record))))
    assert len(record) == chunks + 1  # rebuilt: discovery and the page


def test_missing_page_alone_is_rewritten(world, out_dir):
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient()))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient()))
    [topic] = world.topics
    os.remove(os.path.join(out_dir, "topics", f"{topic['id']}.md"))
    world.topics, world.topic_summaries = [], {}
    record = []
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient(responder=_usage_responder(record))))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient(responder=_usage_responder(record))))
    assert len(record) == 1 and "トピック名: 料金計算" in record[0]


# ── Phase 10 ─────────────────────────────────────────────────────

@pytest.fixture
def layered(world, out_dir):
    asyncio.run(pipeline.phase3_package_dir_summaries(world, FakeGenaiClient(responder=wiki_responder())))
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient()))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient()))
    return world


def test_write_back_updates_replaces_topics_and_embeds(layered, out_dir, fake_spanner,
                                                       fake_vertexai):
    layered.no_spanner = False
    pipeline.phase10_write_back(layered)
    updates = {t: {r[0]: r[1] for r in rows if isinstance(r[1], str)}   # summary columns
               for t, rows in fake_spanner.updates.items()}
    files = updates["Files"]
    assert files[fid("iryo", FEE)] == "ファイルの要約（一括）"
    assert files[fid("iryo", GEN)] == "生成コード（protoc-gen-go）。"
    classes = updates["Classes"]
    assert classes[cid("iryo", FEE, "FeeCalculator")] == "FeeCalculator の要約"
    assert classes[cid("iryo", FEE, "<package>")] == "ファイルの要約（一括）"  # the file's summary
    methods = updates["Methods"]
    assert methods[mid("iryo", FEE, "FeeCalculator.Calculate.func1")] == (
        "FeeCalculator.Calculate 内の関数リテラル。")
    assert methods[mid("iryo", SERVICE, "Total")] == "Total の要約"
    assert updates["Packages"][pipeline._make_id("package", "iryo", "libs/billing", "billing")] == (
        "パッケージの要約")
    assert updates["Directories"][pipeline._make_id("dir", "iryo", "", "")] == "ディレクトリの要約"
    summaries_first = [isinstance(r[1], str) for r in fake_spanner.updates["Methods"]]
    assert summaries_first == sorted(summaries_first, reverse=True)  # then the embeddings

    # R10: this repo's topic rows are replaced through bounded upserts, no single
    # transaction (deleting the stale ones: tests/test_graph_topic_write.py)
    assert fake_spanner.transactions == 0 and fake_spanner.dml == []
    [topic] = layered.topics
    assert fake_spanner.mutations["Topics"] == [[topic["id"], "料金計算",
                                                 layered.topic_summaries[topic["id"]], "iryo"]]
    # conftest's topic names fee.go and libs/billing/doc.go; only fee.go is scanned here
    assert [row[1] for row in fake_spanner.mutations["FileBelongsToTopic"]] == [fid("iryo", FEE)]

    embedded = {t: {r[0] for r in rows if isinstance(r[1], list)}
                for t, rows in fake_spanner.updates.items()}
    embedded = {t: ids for t, ids in embedded.items() if ids}
    assert embedded["Topics"] == {topic["id"]}
    assert mid("iryo", FEE, "FeeCalculator.Calculate.func1") not in embedded["Methods"]
    assert mid("iryo", MAIN, "<init>") not in embedded["Methods"]
    assert mid("iryo", SERVICE, "Total") in embedded["Methods"]
    assert cid("iryo", FEE, "<package>") not in embedded["Classes"]
    assert set(embedded) == {"Files", "Packages", "Classes", "Methods", "Topics"}


def test_write_back_without_spanner_writes_topic_rows_locally(layered, out_dir, monkeypatch):
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: pytest.fail("no Spanner"))
    pipeline.phase10_write_back(layered)
    with open(os.path.join(out_dir, "graph_rows", "Topics.jsonl"), encoding="utf-8") as f:
        [topic_row] = [json.loads(line) for line in f]
    assert topic_row["topic_id"] == layered.topics[0]["id"] and topic_row["repo"] == "iryo"
    with open(os.path.join(out_dir, "graph_rows", "FileBelongsToTopic.jsonl"), encoding="utf-8") as f:
        assert [json.loads(line)["file_id"] for line in f] == [fid("iryo", FEE)]


def test_only_graph_nodes_are_updated(layered, fake_spanner, fake_vertexai):
    layered.no_spanner = False
    layered.dir_summaries["not/a/dir"] = "存在しないディレクトリ"
    pipeline.phase10_write_back(layered)
    dir_ids = {r[0] for r in fake_spanner.updates["Directories"] if isinstance(r[1], str)}
    assert pipeline._make_id("dir", "iryo", "not/a/dir", "") not in dir_ids


# ── the whole wiki track ─────────────────────────────────────────

def test_wiki_track_end_to_end_then_fully_cached(tmp_path, out_dir, monkeypatch, capsys):
    data = build(tmp_path, monkeypatch)
    record = []
    asyncio.run(pipeline.run_wiki_track(data, FakeGenaiClient(responder=wiki_responder(record))))
    out = capsys.readouterr().out
    assert "[Wiki] Estimated Gemini calls: Phase 2 = 4 (4 files to summarize; 0 cached, 1 generated" in out
    for parts in (("summaries", "nodes", f"{FEE}.json"), ("summaries", "packages.json"),
                  ("summaries", "dirs.json"), ("topics", "topic_tree.json"),
                  ("topics", "stats.json"), ("index.md",), ("graph_rows", "Topics.jsonl")):
        assert os.path.isfile(os.path.join(out_dir, *parts)), parts
    [topic] = _json(out_dir, "topics", "topic_tree.json")
    assert set(topic) == {"id", "name", "summary", "files", "subtopics"}
    assert os.path.isfile(os.path.join(out_dir, "topics", f"{topic['id']}.md"))
    with open(os.path.join(out_dir, "index.md"), encoding="utf-8") as f:
        assert f"| {FEE} | ファイルの要約（一括） |" in f.read()

    again = build(tmp_path, monkeypatch)
    record.clear()
    asyncio.run(pipeline.run_wiki_track(again, FakeGenaiClient(responder=wiki_responder(record))))
    assert record == []  # nodes, packages, dirs, topics and pages all reused
    assert "Phase 2 = 0 (0 files to summarize; 4 cached, 1 generated" in capsys.readouterr().out
    assert again.file_summaries == data.file_summaries
    assert again.topic_summaries == data.topic_summaries


def test_pages_of_vanished_topics_are_removed(world, out_dir):
    stale = os.path.join(out_dir, "topics", "a_0000000000000000.md")
    os.makedirs(os.path.dirname(stale), exist_ok=True)
    with open(stale, "w", encoding="utf-8") as f:
        f.write("古いページ")
    asyncio.run(pipeline.phase4_topics(world, FakeGenaiClient()))
    asyncio.run(pipeline.phase5_topic_pages(world, FakeGenaiClient()))
    pipeline.phase6_index(world)
    assert sorted(os.listdir(os.path.join(out_dir, "topics"))) == sorted(
        [f"{world.topics[0]['id']}.md", "inputs.json", "stats.json", "topic_tree.json"])


# ── Phase 10: the embedding cache ────────────────────────────────

def test_unchanged_summaries_make_no_embedding_calls(layered, out_dir, fake_spanner, fake_vertexai,
                                                     capsys):
    layered.no_spanner = False
    pipeline.phase10_write_back(layered)
    first_texts = list(fake_vertexai["texts"])
    first_vectors = {t: {r[0] for r in rows if isinstance(r[1], list)}
                     for t, rows in fake_spanner.updates.items()}
    assert first_texts and os.path.isfile(os.path.join(out_dir, "embeddings", "cache.sqlite"))
    assert "embedded" in capsys.readouterr().out

    fake_spanner.updates.clear()
    fake_vertexai.update(n=0, texts=[], models=0)
    pipeline.phase10_write_back(layered)
    assert fake_vertexai["n"] == 0 and fake_vertexai["models"] == 0  # no API call, no model
    again = {t: {r[0] for r in rows if isinstance(r[1], list)} for t, rows in fake_spanner.updates.items()}
    assert again == first_vectors  # every vector still written to Spanner, from the cache
    out = capsys.readouterr().out
    assert f"0 embedded, {len(first_texts)} cached" in out


def test_changed_summary_reembeds_only_that_node(layered, out_dir, fake_spanner, fake_vertexai):
    layered.no_spanner = False
    pipeline.phase10_write_back(layered)
    fake_vertexai.update(n=0, texts=[])
    layered.node_summaries[SERVICE]["Total"] = "合計を計算する（改訂）"
    pipeline.phase10_write_back(layered)
    assert fake_vertexai["texts"] == [f"{BILLING}.Total: 合計を計算する（改訂）"]


def test_embedding_cache_key_has_model_and_dimension(layered, out_dir, fake_spanner, fake_vertexai,
                                                    monkeypatch):
    layered.no_spanner = False
    pipeline.phase10_write_back(layered)
    total = len(fake_vertexai["texts"])
    for setting, value in (("EMBED_MODEL", "text-embedding-xyz"), ("EMBED_DIMENSION", 256)):
        monkeypatch.setattr(config, setting, value)
        fake_vertexai.update(texts=[])
        pipeline.phase10_write_back(layered)
        assert len(fake_vertexai["texts"]) == total, setting  # nothing reusable
    assert fake_vertexai["dimension"] == 256  # asked of the API too
