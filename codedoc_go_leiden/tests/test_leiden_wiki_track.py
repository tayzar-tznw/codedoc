"""The wiki track of codedoc_go_leiden (leiden spec §2, §5, L-R38): the --wiki
precondition also covers the communities, 4b names them through the async
bridge, Phase 5 writes the pages from structural evidence (children before
parents, scaffolds, grounding), Phase 6 keeps subtopic pages, Phase 10 writes
names and summaries into the topic tables."""
from __future__ import annotations

import json
import math
import os
import re
from types import SimpleNamespace

import pytest

from graph_generator import pipeline, topics_phase
from graph_generator.pipeline import WikiPreconditionError
from graph_generator.topics import community, naming
from tests.conftest import FakeGenaiClient, _FakeResponse
from tests.leiden_worlds import (REPO, TopicDb, graph_data, install_leiden_graph, install_wiki_world,
                                 leiden_responder, read_jsonl, run_wiki, topics_file)


@pytest.fixture
def graph(monkeypatch, tmp_path, out_dir):
    """A finished --graph --no-spanner run on synthetic rows."""
    target, _calls = install_leiden_graph(monkeypatch, tmp_path)
    install_wiki_world(monkeypatch)
    pipeline.run_graph_track(graph_data(target))
    return target


class Recorder:
    """leiden_responder, keeping every prompt."""

    def __init__(self, responder=leiden_responder):
        self.prompts: list[str] = []
        self.responder = responder

    def __call__(self, model, contents, config):
        self.prompts.append(contents)
        return self.responder(model, contents, config)


def _topics(out_dir: str) -> dict[str, dict]:
    return community.all_topics(topics_file(out_dir, "communities.json"))


def _refusal(target: str) -> str:
    data = pipeline.load_graph_state(target, REPO)
    with pytest.raises(WikiPreconditionError) as exc:
        pipeline.prepare_wiki(data, no_spanner=True)
    return str(exc.value)


# ── preconditions ─────────────────────────────────────────────────────


def test_wiki_refuses_a_graph_without_communities(monkeypatch, tmp_path, out_dir):
    target, _calls = install_leiden_graph(monkeypatch, tmp_path)
    monkeypatch.setattr(community, "check_integrity", lambda c, p: ["broken"])
    pipeline.run_graph_track(graph_data(target))
    message = _refusal(target)
    assert "no Leiden communities" in message and "--graph" in message


def test_wiki_refuses_a_missing_communities_file(graph, out_dir):
    os.remove(os.path.join(out_dir, "topics", "communities.json"))
    assert "communities.json is missing" in _refusal(graph)


def test_wiki_refuses_a_communities_file_that_differs_from_the_checkpoint(graph, out_dir):
    path = os.path.join(out_dir, "topics", "communities.json")
    doc = topics_file(out_dir, "communities.json")
    doc["resolution"] = 99.0
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    assert "does not match graph_checkpoint.json" in _refusal(graph)


def test_wiki_refuses_graph_rows_that_give_another_projection(graph, out_dir):
    path = os.path.join(out_dir, "graph_rows", "MethodCalls.jsonl")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines[1:])
    assert "projection" in _refusal(graph)


def test_wiki_accepts_the_graph_it_was_built_for(graph):
    data = pipeline.load_graph_state(graph, REPO)
    pipeline.prepare_wiki(data, no_spanner=True)


# ── 4b names ──────────────────────────────────────────────────────────


def test_4b_names_every_topic_through_the_bridge(graph, out_dir, capsys):
    rec = Recorder()
    data = run_wiki(graph, FakeGenaiClient(rec))
    doc = topics_file(out_dir, "names.json")
    topics = _topics(out_dir)
    assert set(doc["names"]) == set(topics)  # 「未分類」 is not named
    assert {e["label_source"] for e in doc["names"].values()} == {"llm"}
    unassigned = topics_file(out_dir, "communities.json")["unassigned_topic"]
    naming_prompts = [p for p in rec.prompts if "名前を付けてください" in p]
    by_level = [sum(1 for t in topics.values() if t["level"] == lv) for lv in (1, 2)]
    assert len(naming_prompts) == sum(math.ceil(n / naming.BATCH_SIZE) for n in by_level if n)
    assert unassigned and not any(unassigned in p for p in naming_prompts)
    stats = topics_file(out_dir, "stats.json")
    assert stats["discovery"]["calls"] == len(naming_prompts)
    assert "phase4b_naming" in data.timings
    assert re.search(r"\[Phase 4b\] Named \d+ topics", capsys.readouterr().out)


def test_4b_falls_back_to_hub_labels_after_one_retry(graph, out_dir):
    def responder(model, contents, config):
        return "{}" if "名前を付けてください" in contents else leiden_responder(model, contents, config)

    run_wiki(graph, FakeGenaiClient(responder))
    doc = topics_file(out_dir, "names.json")
    assert {e["label_source"] for e in doc["names"].values()} == {"hub"}
    assert doc["stats"]["retries"] >= 1 and doc["stats"]["fallbacks"] == len(doc["names"])


def test_a_cut_naming_answer_falls_back_to_hub_labels_without_a_re_ask(graph, out_dir):
    """R49: a naming answer cut at the budget gets one retry with a larger
    budget inside pipeline._generate; cut again, its batch takes hub labels at
    once, so each batch costs two calls, the estimate's bound."""
    from tests.conftest import _FakeResponse
    naming_calls = []

    def responder(model, contents, config):
        if "名前を付けてください" in contents:
            naming_calls.append(config.max_output_tokens)
            return _FakeResponse('{"a_', finish_reason="MAX_TOKENS")
        return leiden_responder(model, contents, config)

    data = run_wiki(graph, FakeGenaiClient(responder))
    doc = topics_file(out_dir, "names.json")
    batches = topics_phase.estimate_calls(data)["phase4"] // 2
    assert len(naming_calls) == 2 * batches  # each batch: the call and its larger-budget retry
    assert naming_calls[1] > naming_calls[0]
    assert {e["label_source"] for e in doc["names"].values()} == {"hub"}
    assert doc["stats"]["retries"] == 0 and len(doc["stats"]["errors"]) == batches
    assert all(e.startswith("AnswerCut: ") for e in doc["stats"]["errors"])


def test_a_rerun_reuses_names_and_pages_without_gemini_calls(graph, out_dir, capsys):
    run_wiki(graph, FakeGenaiClient(leiden_responder))
    first = topics_file(out_dir, "names.json")
    client = FakeGenaiClient(leiden_responder)
    capsys.readouterr()
    run_wiki(graph, client)
    assert client.calls == 0
    second = topics_file(out_dir, "names.json")
    assert {t: e["name"] for t, e in second["names"].items()} == \
        {t: e["name"] for t, e in first["names"].items()}
    assert {e["label_source"] for e in second["names"].values()} == {"inherited"}
    assert "reused" in capsys.readouterr().out


def _shrink_largest_topic(monkeypatch, out_dir) -> None:
    """A code change: every call, embed and implements of one primary member
    of the largest level-1 topic disappears, so that topic and its parts get
    new IDs that match the old ones at a high Jaccard."""
    from tests.leiden_worlds import synthetic_rows
    l1 = {t: r for t, r in topics_file(out_dir, "communities.json")["levels"]["1"].items()
          if not r.get("unassigned")}
    _tid, rec = max(l1.items(), key=lambda kv: len(community.primary_members(kv[1])))
    victim = sorted(a for a in community.primary_members(rec) if not a.endswith(":<package>"))[0]
    methods = {r["method_id"] for r in synthetic_rows()[0]["Methods"] if r.get("class_id") == victim}
    real = pipeline.derive_edge_rows

    def derive(data, index):
        rows = real(data, index)
        rows["MethodCalls"] = [r for r in rows["MethodCalls"]
                               if r["caller_method"] not in methods and r["callee_method"] not in methods]
        rows["ClassEmbeds"] = [r for r in rows["ClassEmbeds"]
                               if victim not in (r["child_class"], r["embedded_class"])]
        rows["ClassImplements"] = [r for r in rows["ClassImplements"]
                                   if victim not in (r["class_id"], r["interface_id"])]
        return rows
    monkeypatch.setattr(pipeline, "derive_edge_rows", derive)


@pytest.mark.parametrize("graph_runs", [1, 2])
def test_a_carried_topic_keeps_its_name_however_many_graph_runs_follow(monkeypatch, tmp_path, out_dir,
                                                                        graph_runs):
    """L-R38: --graph, --wiki, a code change, then one or two --graph runs (an
    --all's graph half counts) before the next --wiki. A topic with a new ID
    that matches a named topic at Jaccard >= 0.6 keeps that name, and 4b does
    not ask for it. A second --graph run rematches it to itself, so 4b matches
    against the communities names.json named, not the last --graph's."""
    target, _calls = install_leiden_graph(monkeypatch, tmp_path)
    install_wiki_world(monkeypatch)
    pipeline.run_graph_track(graph_data(target))
    run_wiki(target, FakeGenaiClient(leiden_responder))
    first = topics_file(out_dir, "names.json")["names"]
    _shrink_largest_topic(monkeypatch, out_dir)
    pipeline.run_graph_track(graph_data(target))
    carried = {tid: t["inherited_from"] for tid, t in _topics(out_dir).items()
               if t.get("inherited_from") in first and t["inherited_from"] != tid}
    assert carried  # the change gave named topics new IDs
    for _ in range(graph_runs - 1):
        pipeline.run_graph_track(graph_data(target))
    rec = Recorder()
    run_wiki(target, FakeGenaiClient(rec))
    names = topics_file(out_dir, "names.json")["names"]
    naming_prompts = [p for p in rec.prompts if "名前を付けてください" in p]
    for tid, old in carried.items():
        assert names[tid] == {"name": first[old]["name"], "label_source": "inherited", "inherited_from": old}
        assert not any(f"#### {tid}\n" in p for p in naming_prompts), tid


# ── Phase 5 reports ───────────────────────────────────────────────────


def test_reports_write_children_before_parents(graph, out_dir):
    rec = Recorder()
    run_wiki(graph, FakeGenaiClient(rec))
    topics = _topics(out_dir)
    names = {tid: e["name"] for tid, e in topics_file(out_dir, "names.json")["names"].items()}
    report_prompts = [p for p in rec.prompts if "構造的根拠" in p]
    assert len(report_prompts) == len(topics) == len(set(names.values()))
    order = [re.search(r"^# (.+)$", p, re.MULTILINE).group(1) for p in report_prompts]
    parents = [tid for tid, t in topics.items() if t.get("children")]
    assert parents
    for parent in parents:
        prompt = report_prompts[order.index(names[parent])]
        assert "### 子トピック" in prompt
        for child in topics[parent]["children"]:
            assert order.index(names[child]) < order.index(names[parent])
            assert f"{names[child]} はこのトピックの機能です。" in prompt  # the child's intro line


def test_pages_carry_the_grounding_check_and_metrics_aggregate_it(graph, out_dir):
    run_wiki(graph, FakeGenaiClient(leiden_responder))
    topics = _topics(out_dir)
    for tid in topics:
        page = topics_file(out_dir, f"{tid}.md")
        assert "## 識別子の確認" in page and "`NoSuchThing`" in page
    metrics = topics_file(out_dir, "metrics.json")
    assert metrics["grounding"]["pages"] == len(topics)
    assert metrics["grounding"]["found"] >= len(topics)
    assert metrics["grounding"]["top_missing"] == ["NoSuchThing"]
    assert metrics["structure"] == topics_file(out_dir, "communities.json")["metrics"]
    pages = topics_file(out_dir, "pages.json")["pages"]
    assert set(pages) == set(topics) and all(p["key"] for p in pages.values())


def test_pages_may_quote_topic_names(graph, out_dir):
    """A topic name in backticks is no code identifier: Phase 5's grounding
    check skips the run's topic names, as compare-topics does."""
    def responder(model, contents, config):
        if "名前を付けてください" in contents:
            ids = re.findall(r"^#### (\S+)$", contents, re.MULTILINE)
            # an ASCII letter first, as etcd's `etcdサーバーコア機能`: a code identifier by form
            return json.dumps({tid: f"v{tid[-6:]}処理" for tid in ids}, ensure_ascii=False)
        name = re.search(r"^# (.+)$", contents, re.MULTILINE).group(1)
        return f"# {name}\n\n## はじめに\n`{name}` の説明です。`NoSuchThing` は無い名前です。\n"

    run_wiki(graph, FakeGenaiClient(responder))
    pages = topics_file(out_dir, "pages.json")["pages"]
    assert pages and all(p["grounding"]["missing"] == ["NoSuchThing"] for p in pages.values())
    assert all(p["grounding"]["skipped"] >= 1 for p in pages.values())


def test_scaffolds_replace_diagrams_that_add_edges(graph, out_dir):
    def responder(model, contents, config):
        page = leiden_responder(model, contents, config)
        if "構造的根拠" in contents:
            page += "\n## アーキテクチャと設計\n```mermaid\ngraph LR\n  X[Invented] --> Y[Other]\n```\n"
        return page

    run_wiki(graph, FakeGenaiClient(responder))
    pages = topics_file(out_dir, "pages.json")["pages"]
    assert all(p["scaffold_problems"] for p in pages.values())
    tid = next(iter(pages))
    assert "Invented" not in topics_file(out_dir, f"{tid}.md")


def test_a_failed_page_is_retried_and_reported(graph, out_dir, capsys, instant_sleeps):
    failures = {"left": 2}

    def responder(model, contents, config):
        if "構造的根拠" in contents and failures["left"]:
            failures["left"] -= 1
            return ValueError("boom")
        return leiden_responder(model, contents, config)

    run_wiki(graph, FakeGenaiClient(responder))
    assert len(topics_file(out_dir, "pages.json")["pages"]) == len(_topics(out_dir))
    assert 30 in instant_sleeps  # the first cooldown


# ── Phase 6 and 10 ────────────────────────────────────────────────────


def test_phase6_keeps_subtopic_pages_and_drops_retired_ones(graph, out_dir):
    stale = os.path.join(out_dir, "topics", "a_0000000000000000.md")
    os.makedirs(os.path.dirname(stale), exist_ok=True)
    with open(stale, "w", encoding="utf-8") as f:
        f.write("# old\n")
    run_wiki(graph, FakeGenaiClient(leiden_responder))
    topics = _topics(out_dir)
    pages = {n[:-3] for n in os.listdir(os.path.join(out_dir, "topics")) if n.endswith(".md")}
    assert pages == set(topics)
    tree = topics_file(out_dir, "topic_tree.json")
    subtopics = [s for t in tree for s in t["subtopics"]]
    assert subtopics and all(s["id"] in topics and s["class_ids"] and s["summary"] for s in subtopics)
    assert all(t["summary"] for t in tree)


def test_phase10_writes_names_and_summaries_and_embeds_the_topics(monkeypatch, tmp_path, out_dir,
                                                                    fake_vertexai):
    target, _calls = install_leiden_graph(monkeypatch, tmp_path)
    install_wiki_world(monkeypatch)
    db = TopicDb()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    pipeline.run_graph_track(graph_data(target, no_spanner=False))
    db.ops.clear()
    run_wiki(target, FakeGenaiClient(leiden_responder), no_spanner=False)
    rows = read_jsonl(out_dir, "Topics")
    named = [r for r in rows if r["label_source"] == "llm"]
    assert len(named) == len(rows) - 1  # all but 「未分類」
    assert all(r["summary"].startswith("# ") for r in named)
    inserted = {t for op, t, _n in db.ops if op == "insert_or_update"}
    assert set(community.TOPIC_TABLES) <= inserted
    embedded = [row[0] for row in db.updates.get("Topics", [])]
    assert sorted(embedded) == sorted(r["topic_id"] for r in named)


def test_the_estimate_counts_naming_batches_and_pages(graph, out_dir):
    data = pipeline.load_graph_state(graph, REPO)
    pipeline.prepare_wiki(data, no_spanner=True)
    estimate = pipeline.estimate_wiki_calls(data)
    topics = _topics(out_dir)
    batches = sum(math.ceil(n / naming.BATCH_SIZE) for n in
                  (sum(1 for t in topics.values() if t["level"] == lv) for lv in (1, 2)) if n)
    assert estimate["phase4"] == 2 * batches
    assert estimate["phase5"] == len(topics)
    assert topics_phase.estimate_calls(data) == {"phase4": 2 * batches, "phase5": len(topics)}


def test_analyze_all_runs_4a_then_names_and_pages(monkeypatch, tmp_path, out_dir, capsys):
    from graph_generator import __main__ as cli
    target, _calls = install_leiden_graph(monkeypatch, tmp_path)
    install_wiki_world(monkeypatch)
    monkeypatch.setattr(pipeline, "preflight", lambda *a, **k: ([], None))
    monkeypatch.setattr(pipeline, "_genai_client", lambda: FakeGenaiClient(leiden_responder))
    cli.main(["analyze", target, "--all", "--no-spanner", "--repo-name", REPO])
    run = os.path.join(out_dir, "repos", REPO)
    topics = _topics(run)
    assert set(topics_file(run, "names.json")["names"]) == set(topics)
    assert all(os.path.isfile(os.path.join(run, "topics", f"{tid}.md")) for tid in topics)
    out = capsys.readouterr().out
    for label in ("Phase 4a: Leiden communities", "Phase 9: Write topic tables", "Phase 4b: Topic names",
                  "Phase 5: Topic reports"):
        assert label in out
    assert out.index("[Phase 4a]") < out.index("[Phase 8]") < out.index("[Phase 4b]")


def test_naming_asks_for_json_and_pages_for_prose(graph, monkeypatch):
    """R49: a JSON answer gets the bounded thinking config (json_answer=True);
    pages are Markdown."""
    seen: list[tuple[bool, bool]] = []
    real = pipeline._generate

    async def generate(client, prompt, *args, json_answer=False, **kwargs):
        seen.append(("名前を付けてください" in prompt, json_answer))
        return await real(client, prompt, *args, json_answer=json_answer, **kwargs)

    monkeypatch.setattr(pipeline, "_generate", generate)
    run_wiki(graph, FakeGenaiClient(leiden_responder))
    naming = [json_answer for is_naming, json_answer in seen if is_naming]
    pages = [json_answer for is_naming, json_answer in seen if not is_naming]
    assert naming and all(naming)
    assert pages and not any(pages)


def test_4b_and_5_put_their_gemini_usage_in_the_timing_report(monkeypatch, tmp_path, out_dir, capsys):
    """R49: the timing report's Gemini block has 4b's and Phase 5's calls and
    input, output and thinking tokens, the same as topics/stats.json; a rerun
    that reuses every name and page records zeros."""
    from graph_generator import __main__ as cli
    target, _calls = install_leiden_graph(monkeypatch, tmp_path)
    install_wiki_world(monkeypatch)
    monkeypatch.setattr(pipeline, "preflight", lambda *a, **k: ([], None))
    rec = Recorder()

    def counted(model, contents, config):
        answer = _FakeResponse(rec(model, contents, config))
        answer.usage_metadata = SimpleNamespace(prompt_token_count=10, candidates_token_count=2,
                                                thoughts_token_count=5)
        return answer
    monkeypatch.setattr(pipeline, "_genai_client", lambda: FakeGenaiClient(counted))
    cli.main(["analyze", target, "--all", "--no-spanner", "--repo-name", REPO])
    run = os.path.join(out_dir, "repos", REPO)
    report = os.path.join(run, "timing_report.json")
    calls = {"phase4b_naming": sum(1 for p in rec.prompts if "名前を付けてください" in p),
             "phase5_topic_pages": sum(1 for p in rec.prompts if "構造的根拠" in p)}
    assert calls["phase4b_naming"] and calls["phase5_topic_pages"] == len(_topics(run))
    with open(report, encoding="utf-8") as f:
        gemini = json.load(f)["statistics"]["gemini"]
    stats = topics_file(run, "stats.json")
    for phase, step in (("phase4b_naming", "discovery"), ("phase5_topic_pages", "pages")):
        n = calls[phase]
        assert gemini[phase] == {"calls": n, "input_tokens": 10 * n, "output_tokens": 2 * n,
                                 "thinking_tokens": 5 * n}
        assert {key: stats[step][key] for key in pipeline.USAGE_KEYS} == gemini[phase]
    out = capsys.readouterr().out
    n = calls["phase4b_naming"]
    assert re.search(rf"Phase 4b: Topic names\s+{n} calls\s+{10 * n:,} / {2 * n:,} / {5 * n:,}", out)
    n = calls["phase5_topic_pages"]
    assert re.search(rf"Phase 5: Topic reports\s+{n} calls\s+{10 * n:,} / {2 * n:,} / {5 * n:,}", out)
    n = sum(calls.values())
    assert re.search(rf"TOTAL\s+{n} calls\s+{10 * n:,} / {2 * n:,} / {5 * n:,}", out)

    rec.prompts.clear()
    cli.main(["analyze", target, "--wiki", "--no-spanner", "--repo-name", REPO])
    assert rec.prompts == []
    with open(report, encoding="utf-8") as f:
        gemini = json.load(f)["statistics"]["gemini"]
    zero = dict.fromkeys(pipeline.USAGE_KEYS, 0)
    assert gemini["phase4b_naming"] == gemini["phase5_topic_pages"] == zero
