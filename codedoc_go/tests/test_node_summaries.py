"""Phase 2 node summaries (spec §7.2, R11, R20, R30): batched per file with
graph context, node by node for large files, gaps retried one by one,
closures and generated files without a call, a cache keyed by (sha256,
context hash, prompt version, model)."""

import asyncio
import json
import os

import pytest

from graph_generator import config, node_summaries, pipeline
from tests.conftest import FakeGenaiClient
from tests.wiki_worlds import (BILLING, DOC, FEE, GEN, MAIN, SERVICE, build, graph_rows,
                               mid, prompts_for, wiki_responder)


def _run(data, client):
    return asyncio.run(node_summaries.summarize(data, client, pipeline._graph_rows(data)))


@pytest.fixture
def world(tmp_path, out_dir, monkeypatch):
    return build(tmp_path, monkeypatch)


@pytest.fixture
def record():
    return []


@pytest.fixture
def client(record):
    return FakeGenaiClient(responder=wiki_responder(record))


def test_batched_call_carries_source_declarations_and_graph_context(world, client, record):
    result = _run(world, client)
    [prompt] = prompts_for(record, FEE)
    assert "// Calculate returns the fee for points." in prompt  # the source
    section = prompt.split("### 宣言とグラフ上の関係", 1)[1]
    lines = section.splitlines()
    assert (f"- FeeCalculator（struct）: 埋め込み: {BILLING}.Base; 実装するインターフェース: "
            f"{BILLING}.Totaler; メソッド: Calculate") in lines  # the closure is no method
    # its closure calls nothing, and the closure itself is folded in, not listed
    assert f"- FeeCalculator.Calculate（method）: 呼び出し元: {BILLING}.Total" in lines
    assert f"- Totaler（interface）: 実装する型: {BILLING}.FeeCalculator; メソッド: Total" in section
    assert f"- Totaler.Total（interface_method）: 実装: {BILLING}.FeeCalculator" in section
    assert "func1（" not in section and "<package>" not in section  # no call for those
    [main_prompt] = prompts_for(record, MAIN)
    assert "インターフェース経由の候補: " + f"{BILLING}.FeeCalculator.Calculate" in main_prompt
    assert "<init>（" not in main_prompt
    assert world.file_summaries[FEE] == "ファイルの要約（一括）"
    assert world.node_summaries[FEE] == {
        "FeeCalculator": "FeeCalculator の要約", "FeeCalculator.Calculate": "FeeCalculator.Calculate の要約",
        "Totaler": "Totaler の要約", "Totaler.Total": "Totaler.Total の要約",
        "FeeCalculator.Calculate.func1": "FeeCalculator.Calculate 内の関数リテラル。"}
    assert world.node_summaries[MAIN]["<init>"] == "このファイルのパッケージレベル変数の初期化式。"
    assert result["batched"] == 4 and result["calls"] == 4  # FEE, SERVICE, MAIN, DOC


def test_callers_and_callees_are_capped(tmp_path, out_dir, monkeypatch, record, client):
    rows = graph_rows()
    target = mid("iryo", SERVICE, "Total")
    for i in range(12):
        caller = f"a_caller{i:02d}"
        rows["Methods"].append({"method_id": caller, "qualname": f"C{i:02d}", "name": f"C{i:02d}",
                                "fqmn": f"x.C{i:02d}", "file_id": "nowhere", "class_id": "",
                                "member_kind": "func", "summary": ""})
        rows["MethodCalls"].append({"caller_method": caller, "callee_method": target})
        rows["MethodCalls"].append({"caller_method": target, "callee_method": caller})
    data = build(tmp_path, monkeypatch, rows=rows)
    _run(data, client)
    [prompt] = prompts_for(record, SERVICE)
    line = next(l for l in prompt.splitlines() if l.startswith("- Total（func）"))
    # 5 of 14 callers (main and <init> come first) and 10 of 13 callees, sorted
    assert ("呼び出し元: example.com/iryo/cmd/reception.<init>, example.com/iryo/cmd/reception.main, "
            "x.C00, x.C01, x.C02 ほか 9 件") in line
    assert f"呼び出し先: {BILLING}.FeeCalculator.Calculate, x.C00," in line and "x.C08 ほか 3 件" in line


def test_large_file_goes_per_node(world, record, client, monkeypatch, out_dir):
    monkeypatch.setattr(config, "WIKI_FILE_MAX_NODES", 3)  # FEE has 4 declarations
    result = _run(world, client)
    fee_prompts = prompts_for(record, FEE)
    assert not any("### 宣言とグラフ上の関係" in p for p in fee_prompts)  # no batched call
    singles = [p for p in fee_prompts if p.startswith("あなたは Go のソースコードアナリストです。以下の宣言")]
    assert sorted(p.split("宣言: ", 1)[1].split("（", 1)[0] for p in singles) == [
        "FeeCalculator", "FeeCalculator.Calculate", "Totaler", "Totaler.Total"]
    calc = next(p for p in singles if "宣言: FeeCalculator.Calculate（" in p)
    assert "ドキュメントコメント: Calculate returns the fee for points." in calc
    assert "func (c *FeeCalculator) Calculate(points int) int {" in calc  # its span
    assert "type Totaler interface" not in calc
    from_nodes = [p for p in fee_prompts if "宣言ごとの要約です" in p]
    assert len(from_nodes) == 1 and "- FeeCalculator.Calculate: FeeCalculator.Calculate の要約（個別）" in from_nodes[0]
    assert world.file_summaries[FEE] == "ファイルの要約（宣言から）"
    record_path = os.path.join(out_dir, "summaries", "nodes", f"{FEE}.json")
    with open(record_path, encoding="utf-8") as f:
        assert json.load(f)["mode"] == "per-node"
    assert result["per-node"] == 1 and result["batched"] == 3


def test_large_source_goes_per_node(world, record, client, monkeypatch):
    monkeypatch.setattr(config, "WIKI_FILE_MAX_CHARS", 200)  # FEE has 330 characters
    _run(world, client)
    assert not any("### 宣言とグラフ上の関係" in p for p in prompts_for(record, FEE))
    assert any("### 宣言とグラフ上の関係" in p for p in prompts_for(record, SERVICE))


def test_missing_nodes_retried_individually(tmp_path, out_dir, monkeypatch, record):
    data = build(tmp_path, monkeypatch)
    client = FakeGenaiClient(responder=wiki_responder(record, drop={"FeeCalculator.Calculate"}))
    result = _run(data, client)
    singles = [p for p in prompts_for(record, FEE) if "宣言: " in p]
    assert [p.split("宣言: ", 1)[1].split("（", 1)[0] for p in singles] == ["FeeCalculator.Calculate"]
    assert data.node_summaries[FEE]["FeeCalculator.Calculate"] == "FeeCalculator.Calculate の要約（個別）"
    assert data.node_summaries[FEE]["Totaler"] == "Totaler の要約"
    assert data.file_summaries[FEE] == "ファイルの要約（一括）"  # the batched answer had it
    assert result["batched"] == 4 and result["calls"] == 5


def test_unparsable_answer_falls_back_to_nodes(tmp_path, out_dir, monkeypatch, record):
    data = build(tmp_path, monkeypatch)
    client = FakeGenaiClient(responder=wiki_responder(record, bad_json=True))
    result = _run(data, client)
    assert data.node_summaries[FEE]["Totaler.Total"] == "Totaler.Total の要約（個別）"
    assert data.file_summaries[FEE] == "ファイルの要約（宣言から）"
    assert result["per-node"] >= 1


def test_closures_and_init_get_no_call(world, client, record):
    _run(world, client)
    joined = "\n".join(record)
    assert "FeeCalculator.Calculate.func1（" not in joined and "宣言: <init>" not in joined
    assert world.node_summaries[FEE]["FeeCalculator.Calculate.func1"] == (
        "FeeCalculator.Calculate 内の関数リテラル。")


def test_generated_files_get_deterministic_summaries(world, client, record, out_dir):
    _run(world, client)
    assert prompts_for(record, GEN) == []
    assert world.file_summaries[GEN] == "生成コード（protoc-gen-go）。"
    assert world.node_summaries[GEN] == {"Msg": "Msg is a message.（生成コード）"}
    with open(os.path.join(out_dir, "summaries", "nodes", f"{GEN}.json"), encoding="utf-8") as f:
        assert json.load(f)["mode"] == "generated"  # for the webapp; never a cache hit


def test_generated_files_summarized_when_asked(world, client, record, monkeypatch):
    monkeypatch.setattr(config, "WIKI_SUMMARIZE_GENERATED", True)
    _run(world, client)
    [prompt] = prompts_for(record, GEN)
    assert "- Msg（struct）" in prompt
    assert world.node_summaries[GEN] == {"Msg": "Msg の要約"}


def test_cache_reuse_by_sha_context_prompt_version_and_model(tmp_path, out_dir, monkeypatch):
    data = build(tmp_path, monkeypatch)
    _run(data, FakeGenaiClient(responder=wiki_responder()))
    with open(os.path.join(out_dir, "summaries", "nodes", f"{FEE}.json"), encoding="utf-8") as f:
        cached = json.load(f)
    assert set(cached) == {"sha256", "context_hash", "mode", "model", "prompt_version", "file", "nodes"}
    assert cached["sha256"] == data.file_hashes[FEE] and cached["mode"] == "batched"
    assert cached["nodes"]["FeeCalculator.Calculate.func1"].endswith("内の関数リテラル。")  # for the webapp

    def again(**changes):
        record = []
        fresh = build(tmp_path, monkeypatch, **changes)
        result = _run(fresh, FakeGenaiClient(responder=wiki_responder(record)))
        return fresh, result, {rel for rel in (FEE, SERVICE, MAIN, DOC) if prompts_for(record, rel)}

    fresh, result, asked = again()
    assert asked == set() and result["calls"] == 0 and result["cached"] == 4
    assert fresh.node_summaries[FEE] == data.node_summaries[FEE]

    with open(os.path.join(data.target_dir, SERVICE), "a", encoding="utf-8") as f:
        f.write("\n// edited\n")
    from tests import wiki_worlds
    monkeypatch.setitem(wiki_worlds.SOURCES, SERVICE, wiki_worlds.SOURCES[SERVICE] + "\n// edited\n")
    assert again()[2] == {SERVICE}  # its SHA-256 changed

    rows = graph_rows()
    rows["MethodCalls"].append({"caller_method": mid("iryo", MAIN, "main"),
                                "callee_method": mid("iryo", FEE, "FeeCalculator.Calculate")})
    assert again(rows=rows)[2] == {FEE, MAIN}  # their graph context changed (R30)

    monkeypatch.setattr(node_summaries, "PROMPT_VERSION", "go-wiki-test")
    assert again()[2] == {FEE, SERVICE, MAIN, DOC}
    monkeypatch.setattr(config, "MODEL", "gemini-other")
    assert again()[2] == {FEE, SERVICE, MAIN, DOC}


def test_shared_cache_serves_another_output_dir(tmp_path, out_dir, monkeypatch):
    """R11: codedoc_go and codedoc_go_leiden point WIKI_SUMMARY_CACHE at one
    directory; each still gets the per-repo artifacts the webapp reads."""
    monkeypatch.setattr(config, "WIKI_SUMMARY_CACHE", str(tmp_path / "shared"))
    first = build(tmp_path, monkeypatch)
    _run(first, FakeGenaiClient(responder=wiki_responder()))
    for base in (tmp_path / "shared" / "iryo", tmp_path / "out" / "summaries"):
        assert (base / "nodes" / f"{FEE}.json").is_file()
    other_out = tmp_path / "leiden-out"
    other_out.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", str(other_out))
    second = build(tmp_path, monkeypatch)
    record = []
    result = _run(second, FakeGenaiClient(responder=wiki_responder(record)))
    assert record == [] and result["cached"] == 4
    assert (other_out / "summaries" / "nodes" / f"{FEE}.json").is_file()
    assert second.file_summaries == first.file_summaries


def test_failed_batch_is_retried_after_a_cooldown(world, record, instant_sleeps):
    responder = wiki_responder(record)
    failures = {"left": 1}

    def flaky(model, contents, config):
        if f"ファイル: {FEE}（" in contents and failures["left"]:
            failures["left"] -= 1
            return ValueError("503 backend error")
        return responder(model, contents, config)
    result = _run(world, FakeGenaiClient(responder=flaky))
    assert 30 in instant_sleeps
    assert world.file_summaries[FEE] == "ファイルの要約（一括）" and result["batched"] == 4


def test_incomplete_results_are_used_but_not_cached(world, record, instant_sleeps, out_dir):
    responder = wiki_responder(record, drop={"Totaler.Total"})

    def no_single_for_total(model, contents, config):
        if "宣言: Totaler.Total（" in contents:
            return ValueError("400 bad request")
        return responder(model, contents, config)
    result = _run(world, FakeGenaiClient(responder=no_single_for_total))
    assert "Totaler.Total" not in world.node_summaries[FEE]
    assert world.node_summaries[FEE]["Totaler"] == "Totaler の要約"
    assert result["incomplete"] == 1
    assert not os.path.exists(os.path.join(out_dir, "summaries", "nodes", f"{FEE}.json"))
    assert [s for s in instant_sleeps if s >= 30] == [30, 90, 180]  # the gap was retried


def test_empty_files_need_no_call(tmp_path, out_dir, monkeypatch, record, client):
    data = build(tmp_path, monkeypatch)
    with open(os.path.join(data.target_dir, DOC), "w", encoding="utf-8") as f:
        f.write("  \n")
    pipeline.phase1_rescan(data)
    result = _run(data, client)
    assert prompts_for(record, DOC) == [] and DOC not in data.file_summaries
    assert result["empty"] == 1


def test_estimate_matches_the_calls(world, record, monkeypatch):
    monkeypatch.setattr(config, "WIKI_FILE_MAX_NODES", 3)
    assert node_summaries.estimate(world, pipeline._graph_rows(world)) == {
        "calls": 3 + 5, "files": 4, "cached": 0, "generated": 1}  # FEE: 4 nodes + its file
    result = _run(world, FakeGenaiClient(responder=wiki_responder(record)))
    assert result["calls"] == 8
    assert node_summaries.estimate(world, pipeline._graph_rows(world))["calls"] == 0


def test_phase2_shell_prints_the_summary(world, client, capsys):
    asyncio.run(pipeline.phase2_node_summaries(world, client))
    out = capsys.readouterr().out
    assert "[Phase 2] 5 file summaries" in out and "batched 4" in out and "generated 1" in out
    assert world.timings["phase2_node_summaries"] >= 0


def test_vanished_file_is_not_retried(world, client, record, instant_sleeps):
    os.remove(os.path.join(world.target_dir, SERVICE))
    result = _run(world, client)
    assert instant_sleeps == [] and SERVICE not in world.file_summaries
    assert result["empty"] == 1 and result["batched"] == 3


def test_rejected_batch_falls_back_to_nodes(world, record):
    responder = wiki_responder(record)

    def reject_batch(model, contents, config):
        if f"ファイル: {FEE}（" in contents and "### 宣言とグラフ上の関係" in contents:
            return ValueError("400 INVALID_ARGUMENT: request too large")
        return responder(model, contents, config)
    result = _run(world, FakeGenaiClient(responder=reject_batch))
    assert world.node_summaries[FEE]["Totaler"] == "Totaler の要約（個別）"
    assert world.file_summaries[FEE] == "ファイルの要約（宣言から）"
    assert result["per-node"] == 1


def test_large_file_without_declarations_sends_its_head(world, record, client, monkeypatch):
    monkeypatch.setattr(config, "WIKI_FILE_MAX_CHARS", 40)  # DOC has 57 characters, no declaration
    _run(world, client)
    [prompt] = prompts_for(record, DOC)
    assert "// Package kernel holds" in prompt and "package kernel" not in prompt  # truncated
    assert "（宣言なし）" in prompt and world.file_summaries[DOC] == "ファイルの要約（一括）"


def _requalified(record, keys):
    """wiki_responder, with FEE's batched answer keyed by keys[qualname]."""
    plain = wiki_responder(record)

    def respond(model, contents, config):
        answer = plain(model, contents, config)
        if f"ファイル: {FEE}（" in contents and "### 宣言とグラフ上の関係" in contents:
            parsed = json.loads(answer)
            parsed["nodes"] = {keys.get(q, q): s for q, s in parsed["nodes"].items()}
            answer = json.dumps(parsed, ensure_ascii=False)
        return answer
    return respond


def test_package_qualified_keys_are_accepted(tmp_path, out_dir, monkeypatch, record):
    """R49: a key may carry the import path, a part of it or the package name
    in front of the qualname, or name a method as (*T).M."""
    data = build(tmp_path, monkeypatch)
    keys = {"FeeCalculator": f"{BILLING}.FeeCalculator", "Totaler": "billing.Totaler",
            "Totaler.Total": "libs/billing.Totaler.Total",
            "FeeCalculator.Calculate": "(*FeeCalculator).Calculate"}
    result = _run(data, FakeGenaiClient(responder=_requalified(record, keys)))
    assert [p for p in prompts_for(record, FEE) if "宣言: " in p] == []  # nothing asked again
    assert data.node_summaries[FEE] == {
        "FeeCalculator": "FeeCalculator の要約", "FeeCalculator.Calculate": "FeeCalculator.Calculate の要約",
        "Totaler": "Totaler の要約", "Totaler.Total": "Totaler.Total の要約",
        "FeeCalculator.Calculate.func1": "FeeCalculator.Calculate 内の関数リテラル。"}
    assert result["calls"] == 4 and result["batched"] == 4


def test_answer_keys_name_only_declarations_of_the_file():
    job = node_summaries.FileJob("libs/billing/fee.go", "0" * 64, BILLING, False, [
        node_summaries.Decl(q, "func", 1, 1, "", [])
        for q in ("Total", "Calc", "Fee", "FeeCalculator.Calculate", "main")], {},
        package_name="billing")
    nodes = {"Total": "exact", f"{BILLING}.Total": "qualified twin",   # the exact key wins
             "FeeCalculator.Fee": "a type is no package",             # not Fee
             "kernel.Calc": "another package's name",                 # not Calc
             "example.com/other/billing.FeeCalculator.Calculate": "any import path",
             "billing.main": "the package name", "Missing": "no such declaration", "Fee ": 3}
    file_summary, found = node_summaries._answer({"file": " 要約 ", "nodes": nodes}, job)
    assert file_summary == "要約"
    assert found == {"Total": "exact", "FeeCalculator.Calculate": "any import path",
                     "main": "the package name"}


def test_a_qualified_key_never_falls_back_to_a_shorter_name():
    """`<path>.FeeCalculator.Calculate`, another file's method the model copied
    from the graph context, does not name this file's function Calculate."""
    def job(*qualnames, package=BILLING):
        return node_summaries.FileJob("x.go", "0" * 64, package, False, [
            node_summaries.Decl(q, "func", 1, 1, "", []) for q in qualnames], {},
            package_name=package.rpartition("/")[2].partition(".")[0])
    nodes = {f"{BILLING}.FeeCalculator.Calculate": "another file's method",
             f"{BILLING}.Calculate": "this file's func",
             f"(*{BILLING}.FeeCalculator).Total": "another type's method"}
    assert node_summaries._answer({"nodes": nodes}, job("Calculate", "Total"))[1] == {
        "Calculate": "this file's func"}
    go_types = {f"(*{BILLING}.FeeCalculator).Calculate": "go/types form"}
    assert node_summaries._answer({"nodes": go_types}, job("FeeCalculator.Calculate"))[1] == {
        "FeeCalculator.Calculate": "go/types form"}
    yaml = {"gopkg.in/yaml.v3.Node": "dotted path", "yaml.v3.Kind": "dotted path element"}
    assert node_summaries._answer({"nodes": yaml}, job("Node", "Kind", package="gopkg.in/yaml.v3"))[1] == {
        "Node": "dotted path", "Kind": "dotted path element"}


def test_this_files_import_path_beats_another(tmp_path):
    """Review 3: a key with this file's import path names the declaration
    first; another import path only fills a name still missing."""
    job = node_summaries.FileJob("x.go", "0" * 64, BILLING, False, [
        node_summaries.Decl(q, "func", 1, 1, "", []) for q in ("Total", "Calc")], {},
        package_name="billing")
    nodes = {"example.com/other/billing.Total": "another path", f"{BILLING}.Total": "this path",
             "example.com/other/billing.Calc": "the only key"}
    assert node_summaries._answer({"nodes": nodes}, job)[1] == {"Total": "this path",
                                                                "Calc": "the only key"}


def test_method_expressions_are_read_at_the_start_or_after_a_dot():
    """Review 6: `(*T).M` and `billing.(*T).M` read as T.M; parentheses inside
    a name are no method expression."""
    job = node_summaries.FileJob("x.go", "0" * 64, BILLING, False, [
        node_summaries.Decl(q, "func", 1, 1, "", []) for q in ("T.M", "Fint", "F")], {},
        package_name="billing")
    nodes = {"billing.(*T).M": "after a dot", "F(int)": "a call, not a name", "Fn(int)": "no"}
    assert node_summaries._answer({"nodes": nodes}, job)[1] == {"T.M": "after a dot"}


def test_cut_batched_answer_goes_per_node(world, record, instant_sleeps):
    """R49: a batched answer cut at MAX_TOKENS, also with the larger budget,
    is no failure to retry after a cooldown: the file goes node by node."""
    from tests.conftest import _FakeResponse
    responder = wiki_responder(record)

    def cut_batch(model, contents, config):
        if f"ファイル: {FEE}（" in contents and "### 宣言とグラフ上の関係" in contents:
            record.append(contents)
            return _FakeResponse('{"file": "途中で', finish_reason="MAX_TOKENS")
        return responder(model, contents, config)
    result = _run(world, FakeGenaiClient(responder=cut_batch))
    assert len([p for p in prompts_for(record, FEE) if "### 宣言とグラフ上の関係" in p]) == 2
    assert world.node_summaries[FEE]["Totaler"] == "Totaler の要約（個別）"
    assert world.file_summaries[FEE] == "ファイルの要約（宣言から）"
    assert result["per-node"] == 1 and result["batched"] == 3
    assert [s for s in instant_sleeps if s >= 30] == []  # no cooldown round


def test_batched_calls_ask_for_a_bounded_thinking_level(world, monkeypatch):
    """R49: JSON answers get thinking level LOW; plain-text calls keep the default."""
    from google.genai import types
    levels = {}
    responder = wiki_responder()

    def respond(model, contents, config):
        kind = "batched" if "### 宣言とグラフ上の関係" in contents else "text"
        levels.setdefault(kind, set()).add(
            config.thinking_config.thinking_level if config.thinking_config else None)
        return responder(model, contents, config)
    monkeypatch.setattr(config, "WIKI_FILE_MAX_NODES", 3)  # FEE goes node by node
    _run(world, FakeGenaiClient(responder=respond))
    assert levels == {"batched": {types.ThinkingLevel.LOW}, "text": {None}}


def test_phase2_reports_its_tokens(world, capsys):
    from types import SimpleNamespace
    from tests.conftest import _FakeResponse
    responder = wiki_responder()

    def counted(model, contents, config):
        answer = _FakeResponse(responder(model, contents, config))
        answer.usage_metadata = SimpleNamespace(prompt_token_count=100, candidates_token_count=20,
                                                thoughts_token_count=700)
        return answer
    asyncio.run(pipeline.phase2_node_summaries(world, FakeGenaiClient(responder=counted)))
    assert "4 Gemini calls (400 input, 80 output, 2,800 thinking tokens)" in capsys.readouterr().out


def test_closures_fold_into_their_enclosing_function(tmp_path, out_dir, monkeypatch, record, client):
    """A function's callees include those of its closures, and a call made in a
    closure counts for the function around it (closures get no summary)."""
    rows = graph_rows()
    closure, total = mid("iryo", FEE, "FeeCalculator.Calculate.func1"), mid("iryo", SERVICE, "Total")
    worker = mid("iryo", MAIN, "main.func1")
    rows["Methods"].append({"method_id": worker, "name": "func1", "qualname": "main.func1",
                            "fqmn": "example.com/iryo/cmd/reception.main.func1",
                            "file_id": next(r["file_id"] for r in rows["Files"] if r["path"] == MAIN),
                            "class_id": "", "member_kind": "closure", "summary": "",
                            "start_line": 8, "end_line": 8})
    rows["MethodCalls"] += [{"caller_method": closure, "callee_method": total},
                            {"caller_method": mid("iryo", MAIN, "main"), "callee_method": worker},
                            {"caller_method": worker, "callee_method": total}]
    data = build(tmp_path, monkeypatch, rows=rows)
    _run(data, client)
    fee = next(l for l in prompts_for(record, FEE)[0].splitlines()
               if l.startswith("- FeeCalculator.Calculate（method）"))
    assert fee == f"- FeeCalculator.Calculate（method）: 呼び出し先: {BILLING}.Total; 呼び出し元: {BILLING}.Total"
    total_line = next(l for l in prompts_for(record, SERVICE)[0].splitlines()
                      if l.startswith("- Total（func）"))
    assert "main.func1" not in total_line
    assert ("呼び出し元: example.com/iryo/cmd/reception.<init>, example.com/iryo/cmd/reception.main, "
            f"{BILLING}.FeeCalculator.Calculate") in total_line
