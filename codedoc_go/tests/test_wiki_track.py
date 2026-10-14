"""Wiki track pieces kept from codedoc_python (Gemini calls, throttle, JSON
helpers) and Phases 4-6: topics link files by repo-relative path, never by
basename (spec §7.3), in the topic tree shape of C7."""

import asyncio
import json
import os
import re

import pytest
from google.genai import types

from graph_generator import config, pipeline
from graph_generator.pipeline import (PipelineData, _AdaptiveThrottle, _chunk_content,
                                      _extract_json_substring, _generate, _strip_markdown_fences,
                                      _truncate, phase4_topics, phase5_topic_pages, phase6_index,
                                      topic_file_relpaths)
from tests.conftest import SAMPLE_GO, SAMPLE_TREE, FakeGenaiClient, _FakeResponse, write_tree
from tests.pipeline_fakes import fake_scan_go_files


@pytest.fixture
def data(tmp_path, out_dir, monkeypatch):
    monkeypatch.setattr(pipeline, "_scan_go_files", fake_scan_go_files)
    repo = write_tree(tmp_path / "iryo", {**SAMPLE_TREE, "main.go": "package main\n"})
    data = PipelineData(target_dir=repo, repo="iryo")
    pipeline.phase1_rescan(data)
    data.file_summaries = {rel: f"{rel} の要約です。" for rel in data.file_hashes}
    return data


def _topics_json(*topics):
    return json.dumps([{"name": name, "files": files, "subtopics": subs}
                       for name, files, subs in topics], ensure_ascii=False)


def _topic_client(answer, prompts=None):
    def responder(model, contents, config):
        if prompts is not None:
            prompts.append(contents)
        return answer if "JSON形式" in contents or "JSON配列" in contents else "ページ本文"
    return FakeGenaiClient(responder=responder)


# ── Phase 4 ──────────────────────────────────────────────────────

def test_topics_link_files_by_relpath(data, out_dir):
    prompts = []
    answer = _topics_json(
        ("料金計算", ["libs/billing/fee.go", "./libs/billing/doc.go", "doc.go", "fee.go"],
         [{"name": "計算規則", "files": ["libs/billing/fee.go", "libs/kernel/doc.go"]}]),
        ("受付", ["cmd/reception/main.go", "main.go", "missing/x.go"], []))
    asyncio.run(phase4_topics(data, _topic_client(answer, prompts)))
    fee, recv = data.topics
    assert fee["files"] == ["libs/billing/fee.go", "libs/billing/doc.go"]  # bare names dropped
    assert fee["subtopics"] == [{"name": "計算規則", "summary": "",
                                 "files": ["libs/billing/fee.go", "libs/kernel/doc.go"]}]
    assert recv["files"] == ["cmd/reception/main.go", "main.go"]  # main.go is the root file
    assert topic_file_relpaths(fee) == ["libs/billing/fee.go", "libs/billing/doc.go",
                                        "libs/kernel/doc.go"]
    # Gemini sees relpaths, so two doc.go files stay apart
    sent = "\n".join(prompts)
    assert "**libs/billing/doc.go**" in sent and "**libs/kernel/doc.go**" in sent
    assert "**main.go**" in sent  # files at the repo root get a chunk of their own


def test_topic_tree_has_the_c7_shape_and_node_ids(data, out_dir):
    answer = _topics_json(("料金計算", ["libs/billing/fee.go"], [{"name": "規則", "files": ["main.go"]}]),
                          ("料金計算", ["libs/billing/doc.go"],  # one name: joined, nothing dropped
                           [{"name": "規則", "files": ["libs/kernel/doc.go"]}, {"name": "表", "files": []}]))
    asyncio.run(phase4_topics(data, _topic_client(answer)))
    with open(os.path.join(out_dir, "topics", "topic_tree.json"), encoding="utf-8") as f:
        tree = json.load(f)
    assert tree == [{"id": pipeline._make_id("topic", "iryo", "料金計算", ""), "name": "料金計算",
                     "summary": "", "files": ["libs/billing/fee.go", "libs/billing/doc.go"],
                     "subtopics": [{"name": "規則", "summary": "", "files": ["main.go", "libs/kernel/doc.go"]},
                                   {"name": "表", "summary": "", "files": []}]}]


def test_same_named_chunk_topics_are_joined_on_the_small_path(tmp_path, out_dir):
    """Review B2: with at most 15 chunk topics there is no merge; client's and
    server's 「設定」 become one topic with both files."""
    data = _synthetic(tmp_path, {"client": 2, "server": 2})

    def responder(model, contents, config):
        files = _chunk_files(contents)
        return _topics_json(("設定", files[:1], []), (f"{files[0].split('/')[0]}本体", files[1:], []))
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert [(t["name"], t["files"]) for t in data.topics] == [
        ("設定", ["client/f000.go", "server/f000.go"]), ("client本体", ["client/f001.go"]),
        ("server本体", ["server/f001.go"])]


def test_unknown_topic_paths_are_logged(data, out_dir, capsys):
    answer = _topics_json(("料金計算", ["fee.go", "libs/billing/fee.go"], []))
    asyncio.run(phase4_topics(data, _topic_client(answer)))
    assert "(1 unknown paths dropped)" in capsys.readouterr().out
    with open(os.path.join(out_dir, "error_log.txt"), encoding="utf-8") as f:
        assert "['fee.go']" in f.read()


def test_many_chunk_topics_are_merged(data, out_dir, monkeypatch):
    many = _topics_json(*[(f"トピック{i}", ["libs/billing/fee.go"], []) for i in range(8)])
    members = list(range(1, 25))  # 3 chunks (main.go, cmd/, libs/) × 8 topics
    merged = json.dumps([{"name": "統合", "summary": "全部", "members": members}], ensure_ascii=False)

    def responder(model, contents, config):
        return merged if "統合してください" in contents else many
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert [t["name"] for t in data.topics] == ["統合"]


def test_failed_merge_falls_back_to_a_topic_per_chunk(data, out_dir, capsys):
    """R49: the fallback keeps every chunk topic, as a subtopic of its chunk's
    topic, and is not cached: the next --wiki builds the topics again."""
    many = _topics_json(*[(f"トピック{i}", ["libs/billing/fee.go"], []) for i in range(8)])

    def responder(model, contents, config):
        return '{"not": "a list"}' if "統合してください" in contents else many
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert [t["name"] for t in data.topics] == ["ルート", "cmd", "libs"]  # never silently lost
    assert [s["name"] for s in data.topics[2]["subtopics"]] == [f"トピック{i}" for i in range(8)]
    assert not os.path.exists(os.path.join(out_dir, "topics", "inputs.json"))
    assert "1 calls failed (see error_log.txt), so the next --wiki builds the topics again" in (
        capsys.readouterr().out)


def test_the_fallback_keeps_at_most_fifteen_topics(tmp_path, out_dir):
    data = _synthetic(tmp_path, {f"d{i:02d}": 2 for i in range(40)})

    def responder(model, contents, config):
        if "統合してください" in contents:
            return "[]"
        return json.dumps([{"name": f"{f}の機能", "files": [f]} for f in _chunk_files(contents)],
                          ensure_ascii=False)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert len(data.topics) == 14 and data.topics[0]["name"] == "d00, d01, d02"  # 40 chunks, 3 a topic
    assert {rel for t in data.topics for rel in topic_file_relpaths(t)} == set(data.file_hashes)


def test_the_fallback_keeps_the_files_of_a_split_directory(tmp_path, out_dir, monkeypatch):
    """Review B1: the slices of a directory above the cap (and of the root)
    are chunks of their own names, so through the fallback each stays a topic
    and every file stays linked."""
    monkeypatch.setattr(pipeline, "_TOPIC_CHUNK_FILES", 4)
    data = _synthetic(tmp_path, {"": 5, "big": 13, "cmd": 2})

    def responder(model, contents, config):
        if "統合してください" in contents:
            return '{"not": "a list"}'
        return json.dumps([{"name": f"機能 {f}", "files": [f]} for f in _chunk_files(contents)],
                          ensure_ascii=False)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert [t["name"] for t in data.topics] == [
        "ルート (1/2)", "ルート (2/2)", "big (1/4)", "big (2/4)", "big (3/4)", "big (4/4)", "cmd"]
    assert {rel for t in data.topics for rel in topic_file_relpaths(t)} == set(data.file_hashes)


def test_no_file_is_lost_on_any_path(tmp_path, out_dir, monkeypatch):
    """Property (review B1, B2): whatever the layout, the chunk answers and the
    merge answer (members, wrong numbers, file lists, junk, nothing, a cut
    answer), every file a chunk topic names stays linked, in at most 15 topics."""
    import random
    rng = random.Random(49)
    monkeypatch.setattr(pipeline, "_TOPIC_CHUNK_FILES", 5)
    monkeypatch.setattr(pipeline, "_reuse_topics", lambda data, digest: False)
    names = ["設定", "料金", "受付", "共通", "帳票", "予約"]
    trials = 0
    for trial in range(200):
        layout = {"": rng.randint(0, 7)}
        for d in range(rng.randint(1, 5)):
            layout[f"d{d}"] = rng.randint(0, 12)
            for s in range(rng.randint(0, 3)):
                layout[f"d{d}/s{s}"] = rng.randint(0, 7)
        if not any(layout.values()):
            continue
        data = _synthetic(tmp_path / f"t{trial}", layout)
        named: set[str] = set()

        def respond(model, contents, config, data=data, named=named):
            if "統合してください" in contents:
                count = len(re.findall(r"^\d+\. ", contents, re.M))
                kind = rng.choice(["members", "wrong numbers", "files", "junk", "empty", "cut"])
                if kind == "cut":
                    return _FakeResponse('[{"name": "途', finish_reason="MAX_TOKENS")
                if kind in ("junk", "empty"):
                    return '{"not": "a list"}' if kind == "junk" else "[]"
                topics = []
                for k in range(rng.randint(1, 20)):
                    members = rng.sample(range(1, count + 1), rng.randint(0, min(count, 4)))
                    topic = {"name": rng.choice(names + [f"機能{k}"]), "members": members}
                    if kind == "wrong numbers":
                        topic["members"] += [count + 5, 0, "x", True]
                    if kind == "files":
                        topic = {"name": topic["name"], "files": rng.sample(sorted(data.file_hashes), 1)}
                    topics.append(topic)
                return json.dumps(topics, ensure_ascii=False)
            files = _chunk_files(contents)
            topics = []
            for _ in range(rng.randint(0, 4)):
                picked = rng.sample(files, rng.randint(0, len(files)))
                subs = ([{"name": rng.choice(names), "files": rng.sample(files, 1)}]
                        if rng.random() < 0.3 else [])
                named.update(picked, *(s["files"] for s in subs))
                topics.append({"name": rng.choice(names), "files": picked, "subtopics": subs})
            return json.dumps(topics, ensure_ascii=False)
        asyncio.run(phase4_topics(data, FakeGenaiClient(responder=respond)))
        linked = {rel for t in data.topics for rel in topic_file_relpaths(t)}
        assert named <= linked, (trial, layout, sorted(named - linked))
        assert len(data.topics) <= 15, (trial, len(data.topics))
        trials += 1
    assert trials > 150


def test_a_chunk_without_a_usable_topic_counts_as_failed(data, out_dir, capsys):
    def responder(model, contents, config):
        files = _chunk_files(contents)
        if "**libs/" in contents:
            return _topics_json(("幽霊", ["doc.go"], []))  # parses, but names no scanned file
        return _topics_json((f"機能 {files[0]}", files, []))
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert not os.path.exists(os.path.join(out_dir, "topics", "inputs.json"))
    assert "1 calls failed" in capsys.readouterr().out
    with open(os.path.join(out_dir, "error_log.txt"), encoding="utf-8") as f:
        log = f.read()
    assert "[Phase 4] topic extraction gave no usable topic: libs" in log
    assert "topics are not cached: the next --wiki builds them again" in log


def test_a_failed_call_is_rebuilt_on_the_next_run(data, out_dir):
    """R49 makes a cut answer fail; the topics of a run with a failed call are
    not reused by the next run."""
    def by_chunk(contents):
        files = _chunk_files(contents)
        return _topics_json((f"機能 {files[0]}", files, []))

    def cut_libs(model, contents, config):
        if "**libs/" in contents:
            return _FakeResponse("[", finish_reason="MAX_TOKENS")
        return by_chunk(contents)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=cut_libs)))
    assert {rel for t in data.topics for rel in topic_file_relpaths(t)} == {
        "main.go", "cmd/reception/main.go"}
    healthy = FakeGenaiClient(responder=lambda m, c, cfg: by_chunk(c))
    data.topics = []
    asyncio.run(phase4_topics(data, healthy))
    assert healthy.calls == 3
    assert {rel for t in data.topics for rel in topic_file_relpaths(t)} == set(data.file_hashes)
    assert os.path.exists(os.path.join(out_dir, "topics", "inputs.json"))


def test_topics_without_a_scanned_file_are_dropped(data, out_dir):
    answer = _topics_json(("幽霊", ["doc.go", "x/y.go"], []), ("料金計算", ["libs/billing/fee.go"], []))
    asyncio.run(phase4_topics(data, _topic_client(answer)))
    assert [t["name"] for t in data.topics] == ["料金計算"]


def _chunk_files(prompt):
    return re.findall(r"^\*\*(.+?)\*\*: ", prompt, re.M)


def _member_world_responder(merge_answer, prompts):
    """Chunk topics numbered 1 起動 (main.go), 2 受付 (cmd/), 3-16 料金0-13 (libs/);
    料金0 has a subtopic. The merge answers *merge_answer*."""
    def respond(model, contents, config):
        prompts.append(contents)
        if "統合してください" in contents:
            return merge_answer
        files = _chunk_files(contents)
        if files == ["main.go"]:
            return json.dumps([{"name": "起動", "summary": "起動処理", "files": ["main.go"]}],
                              ensure_ascii=False)
        if files == ["cmd/reception/main.go"]:
            return json.dumps([{"name": "受付", "summary": "受付の流れ", "files": files}],
                              ensure_ascii=False)
        return json.dumps([{"name": f"料金{i}", "summary": f"料金の側面{i}",
                            "files": ["libs/billing/fee.go" if i % 2 else "libs/kernel/doc.go"],
                            "subtopics": ([{"name": "細目0", "files": ["libs/billing/service.go"]}]
                                          if i == 0 else [])} for i in range(14)],
                          ensure_ascii=False)
    return respond


def test_merge_answers_by_member_numbers_and_python_unions_the_files(data, out_dir):
    """R49: the merge sees numbered chunk topics without file lists and answers
    with their numbers; each topic and subtopic takes its members' files."""
    merge = json.dumps([
        {"name": "医療事務", "summary": "起動と受付", "members": [1, 2],
         "subtopics": [{"name": "受付窓口", "members": [2]}]},
        {"name": "料金計算", "summary": "料金", "members": [*range(3, 16), 99, "x", True]},
    ], ensure_ascii=False)
    prompts = []
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=_member_world_responder(merge, prompts))))
    [merge_prompt] = [p for p in prompts if "統合してください" in p]
    assert "1. 起動（1 ファイル）: 起動処理" in merge_prompt
    assert "3. 料金0（2 ファイル）: 料金の側面0" in merge_prompt  # its subtopic's file counts
    assert ".go" not in merge_prompt  # no file lists
    # 16 (料金13) is in no topic, but 料金計算 links its only file: nothing is lost
    office, fees = data.topics
    assert (office["name"], office["files"]) == ("医療事務", ["main.go", "cmd/reception/main.go"])
    assert office["subtopics"] == [{"name": "受付窓口", "summary": "", "files": ["cmd/reception/main.go"]}]
    assert fees["name"] == "料金計算" and fees["files"] == [
        "libs/kernel/doc.go", "libs/billing/service.go", "libs/billing/fee.go"]
    with open(os.path.join(out_dir, "error_log.txt"), encoding="utf-8") as f:
        log = f.read()
    assert "3 member numbers name no chunk topic" in log and "1 chunk topics in no merged topic" in log


def test_left_out_chunk_topics_with_unlinked_files_go_to_other(data, out_dir):
    merge = json.dumps([{"name": "料金", "summary": "料金", "members": list(range(3, 17))}],
                       ensure_ascii=False)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=_member_world_responder(merge, []))))
    fees, other = data.topics
    assert fees["name"] == "料金" and other["name"] == "その他"
    assert other["files"] == ["main.go", "cmd/reception/main.go"]
    assert [(s["name"], s["files"]) for s in other["subtopics"]] == [
        ("起動", ["main.go"]), ("受付", ["cmd/reception/main.go"])]


def test_the_topic_count_stays_bounded(data, out_dir):
    """R49: sixteen one-member topics keep fourteen; the two smallest of the
    latest go to 「その他」, so Phase 5 writes at most 15 pages."""
    merge = json.dumps([{"name": f"機能{n}", "members": [n]} for n in range(1, 17)], ensure_ascii=False)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=_member_world_responder(merge, []))))
    assert [t["name"] for t in data.topics] == [f"機能{n}" for n in range(1, 15)] + ["その他"]
    assert [s["name"] for s in data.topics[-1]["subtopics"]] == ["機能15", "機能16"]
    assert {rel for t in data.topics for rel in topic_file_relpaths(t)} == (
        set(data.file_hashes) - {"libs/billing/doc.go"})  # every file a chunk topic links


def test_topic_calls_bound_their_thinking_and_count_it(data, out_dir):
    from types import SimpleNamespace
    levels = []
    merge = json.dumps([{"name": "全部", "members": list(range(1, 17))}], ensure_ascii=False)
    plain = _member_world_responder(merge, [])

    def responder(model, contents, cfg):
        levels.append(cfg.thinking_config.thinking_level)
        answer = _FakeResponse(plain(model, contents, cfg))
        answer.usage_metadata = SimpleNamespace(prompt_token_count=10, candidates_token_count=2,
                                                thoughts_token_count=300)
        return answer
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert levels == [types.ThinkingLevel.LOW] * 4  # 3 extractions and the merge
    assert data.topic_stats["discovery"]["thinking_tokens"] == 1200
    pipeline._write_topic_stats(data)
    with open(os.path.join(out_dir, "topics", "stats.json"), encoding="utf-8") as f:
        stats = json.load(f)
    assert stats["discovery"]["thinking_tokens"] == 1200 and stats["pages"]["thinking_tokens"] == 0


def test_topics_of_one_name_are_joined(data, out_dir):
    """Two merged topics of one name become one, and so does a chunk topic left
    out of the merge whose name a merged topic has; a nameless topic's members
    count as left out."""
    merge = json.dumps([{"name": "料金", "members": [3, 4]}, {"name": "料金", "members": [5]},
                        {"name": "受付", "members": [1]}, {"name": "", "members": [6]},
                        {"name": "その他", "members": list(range(7, 16))}], ensure_ascii=False)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=_member_world_responder(merge, []))))
    assert [(t["name"], t["files"]) for t in data.topics] == [
        ("料金", ["libs/kernel/doc.go", "libs/billing/service.go", "libs/billing/fee.go"]),
        ("受付", ["main.go", "cmd/reception/main.go"]),  # 2 (受付) joined it by name
        ("その他", ["libs/kernel/doc.go", "libs/billing/fee.go"])]  # 6 and 16: files linked


def test_a_single_member_topic_keeps_its_chunk_subtopics(data, out_dir):
    merge = json.dumps([{"name": "料金", "members": [3]},
                        {"name": "その他", "members": [1, 2, *range(4, 17)]}], ensure_ascii=False)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=_member_world_responder(merge, []))))
    fee = next(t for t in data.topics if t["name"] == "料金")
    assert fee["subtopics"] == [{"name": "細目0", "summary": "", "files": ["libs/billing/service.go"]}]


def test_cut_merge_answer_falls_back_to_the_chunk_topics(data, out_dir):
    """R49: a merge answer cut at MAX_TOKENS is asked once more with a larger
    budget; cut again, the deterministic fallback runs (no first-object topic)."""
    many = _topics_json(*[(f"トピック{i}", ["libs/billing/fee.go"], []) for i in range(8)])
    cut = '[{"name": "統合", "summary": "途中", "members": [1, 2]}, {"name": "途'
    budgets = []

    def responder(model, contents, config):
        if "統合してください" in contents:
            budgets.append(config.max_output_tokens)
            return _FakeResponse(cut, finish_reason="MAX_TOKENS")
        return many
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert budgets == [config.MAX_OUTPUT_TOKENS, 4 * config.MAX_OUTPUT_TOKENS]
    assert [t["name"] for t in data.topics] == ["ルート", "cmd", "libs"]  # not one "統合"


def test_cut_merge_text_is_never_repaired_into_its_first_object(data, out_dir):
    """An answer that ends inside its array (whatever the finish reason) raises
    in the parser; no repair call closes it into a partial list."""
    many = _topics_json(*[(f"トピック{i}", ["libs/billing/fee.go"], []) for i in range(8)])
    calls = []

    def responder(model, contents, config):
        calls.append(contents)
        if "統合してください" in contents:
            return '[{"name": "統合", "summary": "途中", "members": [1, 2]}, {"name": "途'
        return many if "JSON形式" in contents else pytest.fail("no repair call")
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert len(data.topics) == 3 and len(calls) == 3 + 1  # the fallback; 3 chunks, 1 merge


def test_a_cut_extraction_answer_is_logged_with_its_chunk(data, out_dir):
    def responder(model, contents, config):
        if "**libs/" in contents:
            return _FakeResponse('[{"name": "料金", "files": ["libs/', finish_reason="MAX_TOKENS")
        return _topics_json(("受付", _chunk_files(contents), []))
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    assert [t["name"] for t in data.topics] == ["受付"]
    with open(os.path.join(out_dir, "error_log.txt"), encoding="utf-8") as f:
        assert "[Phase 4] topic extraction failed: libs: answer cut at max_output_tokens=32768" in f.read()


def _synthetic(tmp_path, layout):
    """A repo of summarized files that exist only in the scan: {dir: file count}."""
    root = str(tmp_path / "big")
    rels = sorted(f"{d}/f{i:03d}.go" if d else f"f{i:03d}.go" for d, n in layout.items() for i in range(n))
    data = PipelineData(target_dir=root, repo="big")
    data.file_list = [os.path.join(root, *rel.split("/")) for rel in rels]
    data.file_hashes = {rel: "0" * 64 for rel in rels}
    pipeline._build_dir_tree(data)
    data.file_summaries = {rel: f"{rel} の要約。" + "あ" * 280 for rel in rels}
    return data


def test_a_directory_above_the_file_cap_is_split_by_subdirectory(tmp_path, monkeypatch):
    """R49: chunks stay one per top-level directory up to the cap; above it,
    a directory splits by subdirectory, small siblings grouped up to the cap,
    and a directory's own files above the cap are sliced."""
    monkeypatch.setattr(pipeline, "_TOPIC_CHUNK_FILES", 10)
    data = _synthetic(tmp_path, {"": 2, "big": 4, "big/a": 6, "big/b": 3, "big/c": 2,
                                 "big/d": 12, "big/d/x": 1, "small": 3, "zz": 10})
    chunks = pipeline._topic_chunks(data)
    assert [label for label, _ in chunks] == [  # slices of one directory keep apart (review B1)
        ".", "big, big/a", "big/b, big/c", "big/d (1/2)", "big/d (2/2), big/d/x", "small", "zz"]
    files = [_chunk_files("\n".join(lines)) for _, lines in chunks]
    assert [len(f) for f in files] == [2, 10, 5, 10, 3, 3, 10]
    assert sorted(f for chunk in files for f in chunk) == sorted(data.file_hashes)  # each once
    # the call estimate counts the chunks before any summary is written
    data.file_summaries = {}
    assert pipeline._topic_chunks(data) == []
    assert len(pipeline._topic_chunks(data, lambda _data, fp: fp)) == len(chunks)


def test_prompts_stay_bounded_for_a_thousand_files(tmp_path, out_dir):
    """R49 at etcd scale: no extraction prompt carries more than the cap of
    files, and the merge prompt and answer carry chunk-topic numbers only."""
    layout = {f"server/pkg{i:02d}": 18 for i in range(30)}                  # 540 files
    layout.update({f"tests/e2e{i}": 70 for i in range(4)})                 # 280, each above the cap
    layout.update({f"tool{i}": 9 for i in range(20)})                      # 180 in small dirs
    data = _synthetic(tmp_path, layout)
    assert len(data.file_hashes) == 1000
    prompts = []

    def responder(model, contents, config):
        prompts.append(contents)
        if "統合してください" in contents:
            numbers = [int(n) for n in re.findall(r"^(\d+)\. ", contents, re.M)]
            return json.dumps([{"name": f"機能{k}", "summary": "s", "members": numbers[k::10]}
                               for k in range(10)], ensure_ascii=False)
        files = _chunk_files(contents)
        area = files[0].rpartition("/")[0]
        return json.dumps([{"name": f"{area}の機能{k}", "summary": "s", "files": files[k::4]}
                           for k in range(4)], ensure_ascii=False)
    asyncio.run(phase4_topics(data, FakeGenaiClient(responder=responder)))
    extraction = [p for p in prompts if "統合してください" not in p]
    [merge] = [p for p in prompts if "統合してください" in p]
    cap = pipeline._TOPIC_CHUNK_FILES
    assert max(len(_chunk_files(p)) for p in extraction) <= cap
    template = len(pipeline.TOPIC_EXTRACTION_PROMPT)
    assert max(len(p) for p in extraction) <= template + cap * 400
    assert ".go" not in merge and len(merge) <= len(pipeline.TOPIC_MERGE_PROMPT) + 4 * len(extraction) * 120
    assert len(data.topics) == 10
    linked = {rel for t in data.topics for rel in topic_file_relpaths(t)}
    assert linked == set(data.file_hashes)  # every file, through the members


# ── Phases 5 and 6 ───────────────────────────────────────────────

def test_pages_read_linked_files_by_relpath(data, out_dir):
    answer = _topics_json(("料金計算", ["libs/billing/fee.go"],
                           [{"name": "基盤", "files": ["libs/kernel/doc.go"]}]))
    asyncio.run(phase4_topics(data, _topic_client(answer)))
    prompts = []
    asyncio.run(phase5_topic_pages(data, _topic_client(answer, prompts)))
    [topic] = data.topics
    assert data.topic_summaries == {topic["id"]: "ページ本文"}
    with open(os.path.join(out_dir, "topics", f"{topic['id']}.md"), encoding="utf-8") as f:
        assert f.read() == "ページ本文"
    [prompt] = prompts
    assert "### libs/billing/fee.go\n```go\npackage billing" in prompt
    assert "### libs/kernel/doc.go" in prompt and "**libs/kernel/doc.go**: libs/kernel/doc.go の要約" in prompt
    assert "libs/billing/doc.go" not in prompt


def test_page_retries_record_their_cooldowns(data, out_dir, instant_sleeps):
    data.topics = [{"id": "t1", "name": "料金計算", "summary": "", "files": [], "subtopics": []}]
    asyncio.run(phase5_topic_pages(data, FakeGenaiClient(responder=lambda m, c, cfg: "")))
    assert data.topic_summaries == {}
    assert [s for s in instant_sleeps if s >= 30] == [30, 90, 180]


def test_index_lists_topics_and_files(data, out_dir):
    answer = _topics_json(("料金計算", ["libs/billing/fee.go"], []))
    asyncio.run(phase4_topics(data, _topic_client(answer)))
    asyncio.run(phase5_topic_pages(data, _topic_client(answer)))
    data.dir_summaries = {"": "リポジトリの要約", "libs/billing": "料金の要約"}
    phase6_index(data)
    [topic] = data.topics
    assert topic["summary"] == "ページ本文"
    with open(os.path.join(out_dir, "topics", "topic_tree.json"), encoding="utf-8") as f:
        assert json.load(f)[0]["summary"] == "ページ本文"
    with open(os.path.join(out_dir, "index.md"), encoding="utf-8") as f:
        index = f.read()
    assert index.startswith("# iryo ドキュメント") and "リポジトリの要約" in index
    assert f"**[料金計算](topics/{topic['id']}.md)**: ページ本文" in index
    assert "| libs/kernel/doc.go | libs/kernel/doc.go の要約です。 |" in index
    with open(os.path.join(out_dir, "reasoning_index.json"), encoding="utf-8") as f:
        tree = json.load(f)["summary_tree"]
    libs = next(c for c in tree["children"] if c.get("name") == "libs")
    billing = next(c for c in libs["children"] if c["name"] == "billing")
    assert billing["path"] == "libs/billing" and billing["summary"] == "料金の要約"
    assert {c["path"] for c in billing["children"]} == {
        "libs/billing/doc.go", "libs/billing/fee.go", "libs/billing/service.go"}


def test_index_topic_line_skips_the_page_headings(data, out_dir):
    # Real topic pages open with their own `# name` heading, then section headings.
    answer = _topics_json(("料金計算", ["libs/billing/fee.go"], []))
    asyncio.run(phase4_topics(data, _topic_client(answer)))
    asyncio.run(phase5_topic_pages(data, _topic_client(answer)))
    [topic] = data.topics
    data.topic_summaries = {topic["id"]: "# 料金計算\n\n## 概要\n\n料金を計算するトピックです。\n\n詳しい説明。"}
    phase6_index(data)
    with open(os.path.join(out_dir, "index.md"), encoding="utf-8") as f:
        index = f.read()
    assert f"**[料金計算](topics/{topic['id']}.md)**: 料金を計算するトピックです。\n" in index


def test_no_topics_means_no_pages(data, out_dir):
    asyncio.run(phase4_topics(data, _topic_client("[]")))
    asyncio.run(phase5_topic_pages(data, _topic_client("[]")))
    assert data.topics == [] and data.timings["phase5_topic_pages"] == 0.0
    assert not os.path.exists(os.path.join(out_dir, "topics", "topic_tree.json"))


# ── kept helpers ─────────────────────────────────────────────────

def test_generate_retries_rate_limits_without_sleeping(instant_sleeps):
    client = FakeGenaiClient(script=[Exception("429 RESOURCE_EXHAUSTED"), "OK summary text"])
    assert asyncio.run(_generate(client, "prompt")) == "OK summary text"
    assert instant_sleeps and all(s > 0 for s in instant_sleeps)


def test_generate_raises_on_persistent_empty_text(instant_sleeps):
    with pytest.raises(RuntimeError, match="empty model response"):
        asyncio.run(_generate(FakeGenaiClient(responder=lambda m, c, cfg: ""), "prompt"))


def test_generate_raises_other_errors_at_once(instant_sleeps):
    pipeline._throttle.reset()  # no delay left over from earlier tests
    client = FakeGenaiClient(script=[ValueError("400 INVALID_ARGUMENT")])
    with pytest.raises(ValueError):
        asyncio.run(_generate(client, "prompt"))
    assert instant_sleeps == []


def test_a_slow_call_times_out_and_is_retried(instant_sleeps, monkeypatch):
    """GEMINI_TIMEOUT bounds every call: one that hangs past it is retried
    after a backoff, as a 5xx is, within the same retry budget."""
    from types import SimpleNamespace
    pipeline._throttle.reset()
    monkeypatch.setattr(config, "GEMINI_TIMEOUT", 0.05)
    calls = []

    class Models:
        async def generate_content(self, model=None, contents=None, config=None):
            calls.append(contents)
            if len(calls) == 1:
                await asyncio.sleep(30)  # a hung request
            return _FakeResponse("答え")
    client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    assert asyncio.run(_generate(client, "p")) == "答え"
    assert len(calls) == 2 and len(instant_sleeps) == 1 and instant_sleeps[0] >= 1.0


def test_server_errors_and_timeouts_share_the_retry_budget(instant_sleeps):
    class Unavailable(Exception):
        code = 503
    pipeline._throttle.reset()
    client = FakeGenaiClient(script=[Unavailable("503 UNAVAILABLE"), TimeoutError("timed out"), "答え"])
    assert asyncio.run(_generate(client, "p")) == "答え" and client.calls == 3
    always = FakeGenaiClient(responder=lambda m, c, cfg: TimeoutError("timed out"))
    with pytest.raises(TimeoutError):  # after the budget: a failed call
        asyncio.run(_generate(always, "p", max_retries=3))
    assert always.calls == 3


def test_the_timeout_reaches_the_client(monkeypatch):
    """The SDK takes HttpOptions.timeout in milliseconds and sends it on as
    X-Server-Timeout; 0 sets no timeout."""
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setattr(pipeline, "genai", SimpleNamespace(Client=lambda **kw: seen.update(kw) or "client"))
    assert pipeline._genai_client() == "client" and config.GEMINI_TIMEOUT == 600
    assert seen["http_options"].timeout == 600_000
    monkeypatch.setattr(config, "GEMINI_TIMEOUT", 0)
    pipeline._genai_client()
    assert seen["http_options"] is None


def test_parse_json_response_repairs_through_the_model():
    client = FakeGenaiClient(script=['[{"name": "修復"}]'])
    parsed = asyncio.run(pipeline._parse_json_response("not json at all", client=client))
    assert parsed == [{"name": "修復"}]
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(pipeline._parse_json_response("still not json"))


def test_generate_retries_a_cut_answer_once_with_a_larger_budget(instant_sleeps):
    """R49: finish reason MAX_TOKENS is no answer: one more call with four
    times the budget (at most the model's 65,536)."""
    budgets = []
    answers = iter([_FakeResponse('[{"name": "途', finish_reason=types.FinishReason.MAX_TOKENS),
                    _FakeResponse("[]", finish_reason=types.FinishReason.STOP)])

    def responder(model, contents, cfg):
        budgets.append(cfg.max_output_tokens)
        return next(answers)
    stats = {}
    assert asyncio.run(_generate(FakeGenaiClient(responder=responder), "p", stats=stats)) == "[]"
    assert budgets == [8192, 32768] and stats["calls"] == 2


def test_generate_raises_when_the_larger_budget_is_cut_too(instant_sleeps, monkeypatch):
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 30000)
    budgets = []

    def responder(model, contents, cfg):
        budgets.append(cfg.max_output_tokens)
        return _FakeResponse("", finish_reason="MAX_TOKENS")  # thinking used it all
    with pytest.raises(pipeline.TruncatedAnswer, match="MAX_TOKENS"):
        asyncio.run(_generate(FakeGenaiClient(responder=responder), "p"))
    assert budgets == [30000, 65536]


def test_a_call_can_start_with_a_larger_budget(instant_sleeps):
    """max_output_tokens replaces MAX_OUTPUT_TOKENS for one call, and its cut
    answer is asked again at four times it, at most 65,536 (leiden's pages)."""
    budgets = []
    answers = iter([_FakeResponse("", finish_reason="MAX_TOKENS"),
                    _FakeResponse("ページ", finish_reason="STOP")])

    def responder(model, contents, cfg):
        budgets.append(cfg.max_output_tokens)
        return next(answers)
    client = FakeGenaiClient(responder=responder)
    assert asyncio.run(_generate(client, "p", max_output_tokens=32768)) == "ページ"
    assert budgets == [32768, 65536]
    budgets.clear()
    asyncio.run(_generate(FakeGenaiClient(responder=lambda m, c, cfg: budgets.append(
        cfg.max_output_tokens) or "答え"), "p", max_output_tokens=None))
    assert budgets == [config.MAX_OUTPUT_TOKENS]  # None: as before


def test_json_calls_get_a_bounded_thinking_level(monkeypatch):
    """R49: thinking_level LOW on Gemini 3 and later, where thinking_budget is
    deprecated; Gemini 2.5 rejects thinking_level, so it gets a token budget."""
    seen = []
    client = FakeGenaiClient(responder=lambda m, c, cfg: seen.append(cfg.thinking_config) or "ok")
    asyncio.run(_generate(client, "p", json_answer=True))
    asyncio.run(_generate(client, "p"))
    monkeypatch.setattr(config, "MODEL", "gemini-2.5-flash")
    asyncio.run(_generate(client, "p", json_answer=True))
    assert seen[0].thinking_level == types.ThinkingLevel.LOW and seen[0].thinking_budget is None
    assert seen[1] is None
    assert seen[2].thinking_budget == 1024 and seen[2].thinking_level is None


def test_cut_json_raises_and_is_not_repaired():
    """R49: the parser never returns an inner object of an unclosed array, and
    no repair call turns a cut answer into a partial one."""
    cut = '[{"name": "A", "files": []}, {"name": "B", "files": ["x.go"'
    with pytest.raises(pipeline.UnclosedJSONError):
        _extract_json_substring(cut)
    with pytest.raises(pipeline.UnclosedJSONError):
        _extract_json_substring('noise {"a": [1, 2')
    client = FakeGenaiClient(responder=lambda m, c, cfg: pytest.fail("no repair of a cut answer"))
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(pipeline._parse_json_response(cut, client=client))
    # the first value decides: an object around the array is the answer, as before
    assert _extract_json_substring('x {"topics": [{"name": "A"}]} y') == {"topics": [{"name": "A"}]}
    stats, levels = {}, []
    repaired = FakeGenaiClient(responder=lambda m, c, cfg: levels.append(
        cfg.thinking_config.thinking_level) or '{"a": 1}')
    assert asyncio.run(pipeline._parse_json_response("{'a': 1}", client=repaired, stats=stats)) == {"a": 1}
    assert stats["calls"] == 1 and levels == [types.ThinkingLevel.LOW]  # counted, and a JSON call


def test_helpers():
    assert _truncate("x" * 100, 40).endswith("[TRUNCATED]") and _truncate("ab", 10) == "ab"
    assert _strip_markdown_fences("```json\n{\"a\":1}\n```") == '{"a":1}'
    text = "\n".join(f"line{i}" for i in range(1000))
    assert "".join(_chunk_content(text, chunk_size=100)) == text
    assert _chunk_content("x" * 250, chunk_size=100) == ["x" * 100, "x" * 100, "x" * 50]
    assert _extract_json_substring('noise {"a": 1} trailing') == {"a": 1}
    assert _extract_json_substring('x ["a\\"]", 2] y') == ['a"]', 2]
    with pytest.raises(ValueError):
        _extract_json_substring("no json here")
    assert pipeline._coerce_topic_list({"topics": [{"name": "a"}, 3]}) == [{"name": "a"}]
    assert pipeline._coerce_topic_list({"name": "a"}) == [{"name": "a"}]
    assert pipeline._coerce_topic_list("x") == []
    assert pipeline._fmt_elapsed(3725) == "1h02m05s" and pipeline._fmt_elapsed(65) == "1m05s"


def test_read_source_file_replaces_invalid_utf8(tmp_path):
    path = tmp_path / "bad.go"
    path.write_bytes(b"package x // caf\xe9\n")
    assert pipeline._read_source_file(str(path)) == "package x // caf�\n"


def test_throttle_circuit_breaker_waits_through_injected_sleep(instant_sleeps):
    t = _AdaptiveThrottle()

    async def exercise():
        for _ in range(_AdaptiveThrottle._WINDOW):
            await t.on_rate_limit()
        await t.wait()
        await t.on_success()
    asyncio.run(exercise())
    assert any(s >= 59 for s in instant_sleeps)  # the 60 s circuit pause
    t.reset()
    assert t.current_delay == 0.0


def test_sample_tree_has_repeated_basenames():
    assert [rel for rel in SAMPLE_GO if rel.endswith("/doc.go")] == [
        "libs/billing/doc.go", "libs/kernel/doc.go"]
