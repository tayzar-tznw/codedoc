"""reports: evidence package and budget, Mermaid scaffolds from real edges,
diagram checks, the page prompt and the grounding check (leiden spec §5.2)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_generator.topics import reports  # noqa: E402
from graph_generator.topics.community import detect  # noqa: E402
from graph_generator.topics.graph_builder import build_projection  # noqa: E402
from graph_generator.topics.synthetic import RowBuilder  # noqa: E402


def billing_repo():
    """Billing (Fee, Points, Ledger) and reception (Desk, Queue, Slot); Desk
    calls Fee (Fee's only caller), Ledger calls Queue."""
    b = RowBuilder()
    t = {}
    for code, path in (("Fee", "billing/fee.go"), ("Points", "billing/points.go"), ("Ledger", "billing/ledger.go"),
                       ("Desk", "reception/desk.go"), ("Queue", "reception/queue.go"),
                       ("Slot", "reception/slot.go")):
        t[code] = b.type(path, code, summary=f"{code} の要約\n詳細")
    m = {}

    def meth(code, name):
        key = f"{code}.{name}"
        if key not in m:
            m[key] = b.method(t[code], name)
        return m[key]

    for a, c in (("Fee.Calculate", "Points.Round"), ("Fee.Calculate", "Points.Sum"), ("Fee.Total", "Points.Add"),
                 ("Points.Round", "Ledger.Post"), ("Points.Sum", "Ledger.Post"), ("Points.Add", "Ledger.Close"),
                 ("Fee.Calculate", "Ledger.Post"), ("Fee.Total", "Ledger.Post"), ("Fee.Total", "Ledger.Close"),
                 ("Desk.Open", "Queue.Push"), ("Desk.Open", "Queue.Pop"), ("Desk.Close", "Queue.Push"),
                 ("Queue.Push", "Slot.Take"), ("Queue.Pop", "Slot.Take"), ("Queue.Pop", "Slot.Free"),
                 ("Desk.Open", "Slot.Take"), ("Desk.Close", "Slot.Free"), ("Desk.Open", "Slot.Free"),
                 ("Desk.Bill", "Fee.Calculate"), ("Ledger.Notify", "Queue.Push")):
        b.call(meth(*a.split(".")), meth(*c.split(".")))
    nodes, edges = b.rows()
    proj = build_projection(nodes, edges)
    data = detect(proj, {"band": (2, 4)}).data
    tid = {code: next(k for k, v in data["levels"]["1"].items() if t[code] in v["members"]) for code in ("Fee", "Desk")}
    names = {tid["Fee"]: "診療報酬計算", tid["Desk"]: "患者受付"}
    return nodes, proj, data, t, tid, names


def test_evidence_sections_hold_the_structural_facts():
    _nodes, proj, data, t, tid, names = billing_repo()
    ev = reports.build_evidence(tid["Fee"], data, proj, names=names)
    s = ev["sections"]
    assert ev["header"]["name"] == "診療報酬計算" and ev["header"]["size"] == 6
    assert ev["header"]["roles"] == {"attached-file": 3, "primary": 3}
    assert {h["name"] for h in s["hubs"]} == {"Fee", "Points", "Ledger"}  # isolated members are no hubs
    fee = next(h for h in s["hubs"] if h["name"] == "Fee")
    assert (fee["calls_in"], fee["calls_out"]) == (1, 6)
    assert s["entry_points"] == [{"class_id": t["Fee"], "name": "Fee", "calls_in": 1, "callers": ["患者受付"]}]
    deps = {(d["direction"], d["topic"]): d for d in s["dependencies"]}
    assert deps[("out", "患者受付")]["examples"] == [["Ledger.Notify", "Queue.Push"]]
    assert deps[("in", "患者受付")]["call_count"] == 1 and deps[("in", "患者受付")]["weight"] == 1.0
    assert [(p["source_name"], p["target_name"], p["count"]) for p in s["call_paths"]] == [
        ("Fee", "Ledger", 3), ("Fee", "Points", 3), ("Points", "Ledger", 3)]
    assert ["Fee.Calculate", "Points.Round"] in next(p for p in s["call_paths"] if p["target_name"] == "Points")["examples"]
    assert len(s["members"]) == 6 and s["members"][0]["summary"] in {"Fee の要約", "Points の要約", "Ledger の要約"}
    assert {r["role"] for r in s["members"]} == {"primary", "attached-file"}
    assert ev["omitted"] == {} and ev["omitted_counts"] == {} and ev["tokens"] <= ev["budget"]


def test_evidence_is_cut_at_the_budget_and_omissions_are_recorded():
    _nodes, proj, data, _t, tid, names = billing_repo()
    full = reports.build_evidence(tid["Fee"], data, proj, names=names)
    header = reports.estimate_tokens(reports._render_header(full["header"]))
    small = reports.build_evidence(tid["Fee"], data, proj, names=names, budget_tokens=header + 30)
    assert small["tokens"] <= header + 30
    assert sum(len(v) for v in small["sections"].values()) < sum(len(v) for v in full["sections"].values())
    assert "members" in small["omitted"] and small["omitted_counts"]["members"] == len(small["omitted"]["members"])
    assert "### 省略" in reports.render_evidence(small)


def test_rendered_evidence_uses_backticked_identifiers():
    _nodes, proj, data, _t, tid, names = billing_repo()
    text = reports.render_evidence(reports.build_evidence(tid["Fee"], data, proj, names=names))
    for needle in ("## トピック: 診療報酬計算", "### ハブ", "### 入口", "### トピック間の依存", "### トピック内の主な呼び出し",
                   "### メンバー", "`Fee.Calculate` → `Points.Round`", "`billing/fee.go`", "ファイルの関数"):
        assert needle in text


def test_scaffolds_use_real_edges_only():
    _nodes, proj, data, _t, tid, names = billing_repo()
    ev = reports.build_evidence(tid["Fee"], data, proj, names=names)
    component, flow = reports.component_scaffold(ev), reports.flow_scaffold(ev)
    assert component.startswith("graph LR") and '["Fee"]' in component and "|呼び出し 3|" in component
    assert flow.startswith("sequenceDiagram") and "participant p1 as Fee" in flow
    assert {tuple(e) for e in ev["scaffolds"]["edges"]} == {("Fee", "Points"), ("Fee", "Ledger"), ("Points", "Ledger")}
    page = f"# x\n```mermaid\n{component}\n```\n\n```mermaid\n{flow}\n```\n"
    assert reports.diagram_problems(page, ev) == []
    renamed = '```mermaid\ngraph TD\n    A["Fee"] -->|計算を依頼| B["Points"]\n    B -- 記帳 --> C["Ledger"]\n```'
    assert reports.diagram_problems(renamed, ev) == []


def test_diagram_problems_and_scaffold_enforcement():
    _nodes, proj, data, _t, tid, names = billing_repo()
    ev = reports.build_evidence(tid["Fee"], data, proj, names=names)
    page = ("前文\n```mermaid\ngraph LR\n    n1[\"Points\"] --> n2[\"Fee\"]\n```\n"
            "```mermaid\nsequenceDiagram\n    participant a as Ledger\n    participant b as Desk\n    a->>b: call\n```\n"
            "```mermaid\nclassDiagram\n    Fee <|-- Points\n```\n後文")
    problems = reports.diagram_problems(page, ev)
    assert "edge Points -> Fee is not in the graph" in problems
    assert "edge Ledger -> Desk is not in the graph" in problems
    assert "diagram type classDiagram is not allowed" in problems
    fixed, found = reports.enforce_scaffolds(page, ev)
    assert found == problems
    assert fixed.count(ev["scaffolds"]["component"]) == 2 and fixed.count(ev["scaffolds"]["flow"]) == 1
    assert fixed.startswith("前文") and fixed.endswith("後文")
    assert reports.diagram_problems(fixed, ev) == []
    assert reports.enforce_scaffolds("no diagrams", ev) == ("no diagrams", [])


def test_scaffold_labels_are_unique_within_a_topic():
    b = RowBuilder()
    x = b.type("a/config.go", "Config")
    y = b.type("b/config.go", "Config")
    z = b.type("a/main.go", "Main")
    for i in range(3):
        b.call(b.method(z, f"m{i}"), b.method(x, f"m{i}"))
        b.call(b.method(z, f"n{i}"), b.method(y, f"n{i}"))
    nodes, edges = b.rows()
    proj = build_projection(nodes, edges)
    data = detect(proj, {"band": (1, 3)}).data
    (tid,) = data["levels"]["1"]
    ev = reports.build_evidence(tid, data, proj)
    assert '["Config (a/config.go)"]' in ev["scaffolds"]["component"]
    assert ("Main", "Config (b/config.go)") in {tuple(e) for e in ev["scaffolds"]["edges"]}


def test_parent_evidence_lists_children_and_child_dependencies_use_leaves():
    b = RowBuilder()
    blocks = []
    for k in range(2):
        codes = [b.type(f"s/b{k}/t{i}.go", f"B{k}T{i}") for i in range(7)]
        blocks.append(codes)
        for i, x in enumerate(codes):
            for y in codes[i + 1:]:
                for j in range(3):
                    b.call(b.method(x, f"to{y[-4:]}{j}"), b.method(y, f"from{x[-4:]}{j}"))
    for i in range(5):
        for j in range(3):
            b.call(b.method(blocks[0][i], f"x{j}"), b.method(blocks[1][i], f"y{j}"))
    nodes, edges = b.rows()
    proj = build_projection(nodes, edges)
    data = detect(proj, {"resolutions": (0.1,), "band": (1, 30)}).data
    (parent_id,) = data["levels"]["1"]
    children = data["levels"]["1"][parent_id]["children"]
    assert len(children) == 2
    ev = reports.build_evidence(parent_id, data, proj, names={children[0]: "子A", children[1]: "子B"},
                                child_summaries={children[0]: "子Aの要約\n続き"})
    assert ev["sections"]["children"] == [{"topic_id": children[0], "name": "子A", "summary": "子Aの要約"},
                                          {"topic_id": children[1], "name": "子B", "summary": ""}]
    child_ev = reports.build_evidence(children[0], data, proj, names={children[1]: "子B"})
    assert {d["topic"] for d in child_ev["sections"]["dependencies"]} == {"子B"}
    assert child_ev["header"]["level"] == 2 and child_ev["header"]["parent"]


def test_report_prompt_follows_the_sibling_template():
    _nodes, proj, data, _t, tid, names = billing_repo()
    ev = reports.build_evidence(tid["Fee"], data, proj, names=names)
    prompt = reports.build_report_prompt(ev)
    for needle in ("# 診療報酬計算", "## はじめに", "## アーキテクチャと設計", "### コンポーネント関係図", "## データフロー",
                   "## 主要コンポーネント", "## 依存関係", "## 構造的根拠", "```mermaid\ngraph LR",
                   "```mermaid\nsequenceDiagram", "バッククォート", "追加・削除しない"):
        assert needle in prompt


def test_grounding_check_counts_identifiers_in_backticks():
    nodes, *_ = billing_repo()
    names = reports.build_name_set(nodes)
    page = "\n".join([
        "`Fee` と `Fee.Calculate()` と `billing.Fee` と `(*Fee).Calculate` を使う。",
        "ファイル `billing/fee.go`、パッケージ `example.com/iryo/billing`、疑似型 `<package>`。",
        "`nil` と `go test ./...` と `42` は識別子ではない。`http.Handler` と `Ghost.Run` は存在しない。",
        "`Points.Round(x)` と `List[T]` と `Ghost.Run`。",
        "```go\nfunc `Invented`()\n```",
    ])
    result = reports.grounding_check(page, names)
    assert result["missing"] == ["Ghost.Run", "List", "http.Handler"]
    assert result["missing_count"] == 4
    assert (result["checked"], result["found"]) == (12, 8)
    assert result["skipped"] == 3
    assert result["rate"] == pytest.approx(8 / 12)
    assert reports.grounding_check("no code here", names)["rate"] is None


def test_identifier_of():
    assert reports.identifier_of("FeeCalculator.Calculate()") == "FeeCalculator.Calculate"
    assert reports.identifier_of("(*T).M") == "T.M"
    assert reports.identifier_of("init#2") == "init#2"
    assert reports.identifier_of("<init>.func1") == "<init>.func1"
    assert reports.identifier_of("libs/billing/fee.go") == "libs/billing/fee.go"
    assert reports.identifier_of("err != nil") is None
    assert reports.identifier_of("string") is None
    assert reports.identifier_of("--wiki") is None


def test_append_grounding_lists_misses_at_the_bottom():
    page = reports.append_grounding("# 題\n本文\n", {"checked": 3, "found": 2, "missing": ["Ghost"]})
    assert page.endswith("グラフに見つからない識別子: `Ghost`\n")
    assert "本文のコード識別子 3 個のうち 2 個がグラフに存在します。" in page
    clean = reports.append_grounding("# 題\n", {"checked": 1, "found": 1, "missing": []})
    assert "見つからない" not in clean


def test_aggregate_grounding():
    agg = reports.aggregate_grounding([{"checked": 4, "found": 3, "missing": ["A"]},
                                       {"checked": 6, "found": 6, "missing": []},
                                       {"checked": 2, "found": 0, "missing": ["A", "B"]}])
    assert agg == {"pages": 3, "checked": 12, "found": 9, "rate": 0.75, "missing_distinct": 2,
                   "top_missing": ["A", "B"]}


def test_estimate_tokens():
    assert reports.estimate_tokens("") == 0
    assert reports.estimate_tokens("abcd") == 1
    assert reports.estimate_tokens("abcde") == 2
    assert reports.estimate_tokens("日本語") == 3
    assert reports.estimate_tokens("ab日本") == 3


def test_name_set_includes_package_rows():
    names = reports.build_name_set({"Packages": [{"import_path": "example.com/iryo/kernel", "name": "kernel",
                                                  "directory": "libs/kernel"}]})
    assert names == {"example.com/iryo/kernel", "kernel", "libs/kernel"}


def test_name_set_includes_directory_names_and_module_paths():
    nodes = {"Directories": [{"dir_id": "d1", "name": "interop", "summary": "", "repo": "iryo"}],
             "Packages": [{"import_path": "example.com/iryo/interop/hl7", "name": "hl7", "directory": "interop/hl7",
                           "module": "example.com/iryo/interop"}]}
    names = reports.build_name_set(nodes)
    assert {"interop", "example.com/iryo/interop"} <= names
    result = reports.grounding_check("`interop` と `example.com/iryo/interop` と `iryo`", names)
    assert result["missing"] == ["iryo"] and result["found"] == 2    # a bare module-path segment is no node


def test_member_rows_list_their_functions():
    _nodes, proj, data, t, tid, names = billing_repo()
    ev = reports.build_evidence(tid["Fee"], data, proj, names=names)
    fee = next(r for r in ev["sections"]["members"] if r["class_id"] == t["Fee"])
    assert fee["functions"] == ["Fee.Calculate", "Fee.Total"]
    assert "`Fee.Calculate`, `Fee.Total`" in reports.render_evidence(ev)


def test_annotated_node_labels_are_not_new_edges():
    _nodes, proj, data, _t, tid, names = billing_repo()
    ev = reports.build_evidence(tid["Fee"], data, proj, names=names)
    annotated = '```mermaid\ngraph LR\n    A["Fee（料金計算）"] -->|計算| B["Points: 点数"]\n    B --> C["Ledger<br/>記帳"]\n```'
    assert reports.diagram_problems(annotated, ev) == []
    renamed = '```mermaid\ngraph LR\n    A["FeeX"] --> B["Points"]\n    B --> C["Ledger.Post"]\n```'
    assert reports.diagram_problems(renamed, ev) == ["edge FeeX -> Points is not in the graph",
                                                     "edge Points -> Ledger.Post is not in the graph"]


def test_unassigned_topic_gets_no_evidence():
    b = RowBuilder()
    b.type("dto/lonely.go", "Lonely")
    nodes, edges = b.rows()
    proj = build_projection(nodes, edges)
    data = detect(proj).data
    with pytest.raises(ValueError, match="未分類"):
        reports.build_evidence(data["unassigned_topic"], data, proj)


def test_a_page_scores_the_same_before_and_after_append_grounding():
    nodes, *_ = billing_repo()
    names = reports.build_name_set(nodes)
    page = "# 診療報酬計算\n\n`Fee` は `Points.Round` と `Ghost.Run` を呼ぶ。`http.Handler` も使う。\n"
    before = reports.grounding_check(page, names)
    assert before["missing"] == ["Ghost.Run", "http.Handler"]
    appended = reports.append_grounding(page, before)
    assert reports.GROUNDING_MARKER in appended and "`Ghost.Run`" in appended.split(reports.GROUNDING_MARKER)[1]
    assert reports.grounding_check(appended, names) == before
    again = reports.append_grounding(appended, before)                      # re-appending replaces the section
    assert again == appended and again.count(reports.GROUNDING_HEADING) == 1


def test_pages_without_the_marker_drop_only_a_final_grounding_section():
    nodes, *_ = billing_repo()
    names = reports.build_name_set(nodes)
    body = "# 題\n`Fee` と `Ghost`\n"
    legacy = body + ("\n---\n\n## 識別子の確認\n本文のコード識別子 2 個のうち 1 個がグラフに存在します。\n"
                     "グラフに見つからない識別子: `Ghost`\n")
    assert reports.grounding_check(legacy, names) == reports.grounding_check(body, names)
    middle = "# 題\n## 識別子の確認\n`Ghost`\n## 依存関係\n`Fee`\n"           # not the last section: kept
    assert reports.grounding_check(middle, names)["checked"] == 2
    assert reports.strip_grounding("no section") == "no section"


def test_evidence_accepts_names_json_entries():
    _nodes, proj, data, _t, tid, names = billing_repo()
    doc = {"names": {k: {"name": v, "label_source": "llm"} for k, v in names.items()}}
    for given in (doc, doc["names"]):
        ev = reports.build_evidence(tid["Fee"], data, proj, names=given)
        assert ev["name"] == "診療報酬計算" and ev["sections"]["entry_points"][0]["callers"] == ["患者受付"]


def test_topic_ids_and_topic_vocabulary_are_not_identifiers():
    nodes, *_ = billing_repo()
    names = reports.build_name_set(nodes)
    page = ("トピック `a_00cf5c8f6123e053` と `cd_my_prefix_0123456789abcdef` の役割は `primary`、`attached-test`、"
            "`unassigned`。名前は `llm` 由来。`Fee` は `http.Handler` と `Ghost.Run` を使う。")
    result = reports.grounding_check(page, names)
    assert result["missing"] == ["Ghost.Run", "http.Handler"]          # real misses still count
    assert (result["checked"], result["found"], result["skipped"]) == (3, 1, 6)
    hub_pkg = reports.build_name_set({"Packages": [{"import_path": "example.com/x/hub", "name": "hub"}]})
    assert reports.grounding_check("`hub`", hub_pkg)["found"] == 1        # a real name stays a name


def test_rendered_evidence_shows_no_topic_ids():
    _nodes, proj, data, _t, tid, names = billing_repo()
    text = reports.build_report_prompt(reports.build_evidence(tid["Fee"], data, proj, names=names))
    assert not any(t in text for t in data["levels"]["1"])
    assert "- レベル 1" in text


def test_a_grounding_section_written_by_the_llm_is_checked_and_kept():
    nodes, *_ = billing_repo()
    names = reports.build_name_set(nodes)
    page = "# 題\n`Fee` を使う。\n\n## 識別子の確認\n`Ghost.Run` もある。\n"     # no marker, no count line
    result = reports.grounding_check(page, names)
    assert result["missing"] == ["Ghost.Run"] and result["checked"] == 2
    appended = reports.append_grounding(page, result)
    assert "`Ghost.Run` もある。" in appended                           # the LLM's section stays
    assert reports.grounding_check(appended, names) == result


def test_name_set_includes_ancestor_directories_without_go_files():
    names = reports.build_name_set({"Files": [{"file_id": "f", "path": "contrib/lock/client/main.go",
                                               "file_name": "main.go", "directory": "contrib/lock/client"}]})
    assert {"contrib", "contrib/lock", "contrib/lock/client", "contrib/lock/client/main.go"} <= names
    assert reports.grounding_check("`contrib/lock` と `contrib/missing`", names)["missing"] == ["contrib/missing"]


def test_topic_names_are_skipped_only_when_given():
    names = reports.build_name_set({"Classes": [{"class_id": "c", "name": "Fee", "qualname": "Fee"}]})
    page = "`etcdサーバーコア機能` と `v2インメモリデータストア` と `Fee` と `Ghost` と `フルUUID/ショートUUID`"
    plain = reports.grounding_check(page, names)
    assert plain["missing"] == ["Ghost", "etcdサーバーコア機能", "v2インメモリデータストア"]   # narrow: nothing guessed
    skipped = reports.grounding_check(page, names, skip={"etcdサーバーコア機能", "v2インメモリデータストア"})
    assert (skipped["checked"], skipped["found"], skipped["missing"]) == (2, 1, ["Ghost"])
    assert reports.identifier_of("フルUUID/ショートUUID") is None    # paths are ASCII (Go import paths are)

def test_directory_qualified_types_and_methods_are_names():
    nodes = {"Files": [{"file_id": "f", "path": "model/labels/labels.go", "file_name": "labels.go",
                        "directory": "model/labels", "package_name": "labels"}],
             "Classes": [{"class_id": "c", "name": "Labels", "qualname": "Labels", "file_id": "f"}],
             "Methods": [{"method_id": "m", "name": "Get", "qualname": "Labels.Get", "file_id": "f"}]}
    names = reports.build_name_set(nodes)
    result = reports.grounding_check("`model/labels.Labels` と `model/labels.Labels.Get` と `model/labels.Ghost`", names)
    assert (result["found"], result["missing"]) == (2, ["model/labels.Ghost"])
