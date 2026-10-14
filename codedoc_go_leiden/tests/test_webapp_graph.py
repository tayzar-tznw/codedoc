"""webapp/graph_api.py (spec R51): the graph explorer's read-only, capped views of
OUTPUT_DIR/repos/<repo>/graph_rows/*.jsonl, with no Spanner and no Gemini call."""
from __future__ import annotations

import importlib
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from graph_generator import setup_spanner_graph as schema
from graph_generator.pipeline import _make_id

graph_api = importlib.import_module("webapp.graph_api")

REPO = "iryo"
_DEFAULTS = {"str": "", "int": 0, "bool": False, "float": 0.0}


def _row(table: str, **values) -> dict:
    """A row with exactly write_columns(table), in spec order, as Phase 8/9 write it."""
    spec = schema.NODE_SPEC.get(table) or schema.EDGE_SPEC[table]
    types = dict(spec[1])
    return {c: values.get(c, _DEFAULTS[types[c]]) for c in schema.write_columns(table)}


def nid(kind: str, relpath: str, qualname: str = "") -> str:
    return _make_id(kind, REPO, relpath, qualname)


def eid(table: str, src: str, dst: str) -> str:
    return _make_id("edge", table, src, dst)


def _edge(table: str, src: str, dst: str, **values) -> dict:
    scol, dcol = schema.EDGE_SPEC[table][2][0], schema.EDGE_SPEC[table][3][0]
    return _row(table, edge_id=eid(table, src, dst), **{scol: src, dcol: dst}, **values)


FEE, FEE_TEST, GEN, MAIN = "billing/fee.go", "billing/fee_test.go", "billing/zz_gen.go", \
    "reception/cmd/desk/main.go"
F = {rel: nid("file", rel) for rel in (FEE, FEE_TEST, GEN, MAIN)}
PKG_BILLING, PKG_MAIN = nid("package", "billing", "billing"), nid("package", "reception/cmd/desk", "main")
C_FEE, C_CALC = nid("class", FEE, "FeeCalculator"), nid("class", FEE, "Calculator")
PSEUDO = {rel: nid("class", rel, "<package>") for rel in F}
M_CALC, M_ICALC = nid("method", FEE, "FeeCalculator.Calculate"), nid("method", FEE, "Calculator.Calculate")
M_NEW, M_TEST = nid("method", FEE, "NewFeeCalculator"), nid("method", FEE_TEST, "TestCalculate")
M_GEN, M_MAIN = nid("method", GEN, "generatedTable"), nid("method", MAIN, "main")
DIRS = {d: nid("dir", d) for d in ("", "billing", "reception", "reception/cmd", "reception/cmd/desk")}
TOPIC = "t_billing"


def _file(rel: str, package: str, clause: str, module: str, origin="package", generated=False) -> dict:
    d, _, name = rel.rpartition("/")
    return _row("Files", file_id=F[rel], file_name=name, extension=".go", directory=d, path=rel,
                origin=origin, generated=generated, package=package, package_name=clause,
                module=module, build_included=True, repo=REPO)


def _method(mid, rel, qualname, class_id, kind="func", origin="package", line=3) -> dict:
    return _row("Methods", method_id=mid, name=qualname.rpartition(".")[2], class_id=class_id,
                file_id=F[rel], qualname=qualname, fqmn=f"example.com/iryo/x.{qualname}",
                member_kind=kind, has_body=kind != "interface_method", start_line=line,
                end_line=line + 4, origin=origin, module="example.com/iryo/billing", repo=REPO)


def base_tables() -> dict[str, list[dict]]:
    """A small F1-like graph: billing (fee.go, its test, a generated file) and a
    command in reception/cmd/desk, whose two directories above hold no file."""
    billing = ("example.com/iryo/billing", "billing", "example.com/iryo/billing")
    main = ("example.com/iryo/reception/cmd/desk", "main", "example.com/iryo/reception")
    t = {
        "Files": [_file(FEE, *billing), _file(FEE_TEST, *billing, origin="test"),
                  _file(GEN, *billing, generated=True), _file(MAIN, *main, origin="command")],
        "Packages": [
            _row("Packages", package_id=PKG_BILLING, import_path="example.com/iryo/billing",
                 name="billing", directory="billing", module=billing[2], origin="package",
                 file_count=3, summary="Package billing は料金を計算する。", repo=REPO),
            _row("Packages", package_id=PKG_MAIN, import_path=main[0], name="main",
                 directory="reception/cmd/desk", module=main[2], origin="command", file_count=1,
                 repo=REPO)],
        "Classes": [
            _row("Classes", class_id=C_FEE, name="FeeCalculator", qualname="FeeCalculator",
                 package=billing[0], fqcn=f"{billing[0]}.FeeCalculator", file_id=F[FEE],
                 kind="struct", start_line=5, end_line=9, origin="package", module=billing[2],
                 summary="FeeCalculator は料金計算機。", repo=REPO),
            _row("Classes", class_id=C_CALC, name="Calculator", qualname="Calculator",
                 package=billing[0], fqcn=f"{billing[0]}.Calculator", file_id=F[FEE],
                 kind="interface", start_line=1, end_line=3, origin="package", module=billing[2],
                 repo=REPO)]
        + [_row("Classes", class_id=PSEUDO[rel], name="<package>", qualname="<package>",
                file_id=F[rel], kind="package", repo=REPO,
                origin="test" if rel == FEE_TEST else "package") for rel in F],
        "Methods": [
            _method(M_CALC, FEE, "FeeCalculator.Calculate", C_FEE, "method", line=13),
            _method(M_ICALC, FEE, "Calculator.Calculate", C_CALC, "interface_method", line=2),
            _method(M_NEW, FEE, "NewFeeCalculator", PSEUDO[FEE]),
            _method(M_TEST, FEE_TEST, "TestCalculate", PSEUDO[FEE_TEST], origin="test"),
            _method(M_GEN, GEN, "generatedTable", PSEUDO[GEN]),
            _method(M_MAIN, MAIN, "main", PSEUDO[MAIN], origin="command")],
        "Directories": [_row("Directories", dir_id=i, name=d.rpartition("/")[2] or "iryo-kanri-system",
                             repo=REPO) for d, i in DIRS.items()],
        "Topics": [{"topic_id": TOPIC, "name": "会計", "summary": "# 会計\n\n料金のページ。", "repo": REPO}],
        "MethodCalls": [
            _edge("MethodCalls", M_MAIN, M_NEW, callee_name="NewFeeCalculator", resolution="types",
                  site_kind="call", call_line=5),
            _edge("MethodCalls", M_MAIN, M_ICALC, callee_name="Calculate", resolution="types",
                  site_kind="call", call_line=6),
            _edge("MethodCalls", M_TEST, M_CALC, callee_name="Calculate", resolution="types",
                  site_kind="call", call_line=8),
            _edge("MethodCalls", M_CALC, M_GEN, callee_name="generatedTable", resolution="types",
                  site_kind="call", call_line=14)],
        "PossiblyCalls": [_edge("PossiblyCalls", M_MAIN, M_CALC, callee_name="Calculate",
                                reason="implementation", candidate_count=1, open_world=False)],
        "ClassImplements": [_edge("ClassImplements", C_FEE, C_CALC, resolution="types")],
        "FileDefinesClass": [_edge("FileDefinesClass", F[FEE], C_FEE), _edge("FileDefinesClass", F[FEE], C_CALC)]
        + [_edge("FileDefinesClass", F[rel], PSEUDO[rel]) for rel in F],
        "ClassDefinesMethod": [_edge("ClassDefinesMethod", C_FEE, M_CALC), _edge("ClassDefinesMethod", C_CALC, M_ICALC),
                               _edge("ClassDefinesMethod", PSEUDO[FEE], M_NEW),
                               _edge("ClassDefinesMethod", PSEUDO[FEE_TEST], M_TEST),
                               _edge("ClassDefinesMethod", PSEUDO[GEN], M_GEN),
                               _edge("ClassDefinesMethod", PSEUDO[MAIN], M_MAIN)],
        "DirContainsFile": [_edge("DirContainsFile", DIRS["billing"], F[rel]) for rel in (FEE, FEE_TEST, GEN)]
        + [_edge("DirContainsFile", DIRS["reception/cmd/desk"], F[MAIN])],
        "PackageContainsFile": [_edge("PackageContainsFile", PKG_BILLING, F[rel]) for rel in (FEE, FEE_TEST, GEN)]
        + [_edge("PackageContainsFile", PKG_MAIN, F[MAIN])],
        "PackageImports": [_edge("PackageImports", F[MAIN], PKG_BILLING,
                                 import_path="example.com/iryo/billing", kind="normal")],
        "FileDependsOn": [_edge("FileDependsOn", F[MAIN], F[FEE], resolution="types")],
        "FileBelongsToTopic": [{"edge_id": eid("FileBelongsToTopic", F[rel], TOPIC), "file_id": F[rel],
                                "topic_id": TOPIC} for rel in (FEE, MAIN)],
    }
    return t


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def write_tables(repo_dir: Path, tables: dict[str, list[dict]]) -> None:
    rows_dir = repo_dir / "graph_rows"
    rows_dir.mkdir(parents=True, exist_ok=True)
    for table, rows in tables.items():
        (rows_dir / f"{table}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in rows), encoding="utf-8")


def write_wiki(repo_dir: Path) -> None:
    _write_json(repo_dir / "summaries" / "nodes" / "billing" / "fee.go.json", {
        "file": "料金計算のファイルです。",
        "nodes": {"FeeCalculator.Calculate": "点数から料金を出す。"}})
    _write_json(repo_dir / "summaries" / "packages.json", {"billing|billing": {"summary": "会計パッケージ。"}})
    _write_json(repo_dir / "summaries" / "dirs.json", {"": {"summary": "リポジトリ全体。"},
                                                       "reception/cmd": {"summary": "コマンド群。"}})
    _write_json(repo_dir / "topics" / "topic_tree.json", [{"id": TOPIC, "name": "会計", "files": [FEE, MAIN]}])
    (repo_dir / "topics" / f"{TOPIC}.md").write_text("# 会計\n", encoding="utf-8")


@pytest.fixture
def web(tmp_path, monkeypatch):
    """(client, repo dir) for one repo with the base graph and its wiki files."""
    main = importlib.import_module("webapp.main")
    monkeypatch.setattr(main, "OUTPUT_ROOT", tmp_path)
    monkeypatch.delenv("WEBAPP_REPO", raising=False)
    monkeypatch.setattr(graph_api, "_graphs", OrderedDict())
    repo_dir = tmp_path / "repos" / REPO
    write_tables(repo_dir, base_tables())
    write_wiki(repo_dir)
    return TestClient(main.app), repo_dir


def ids(items) -> list[str]:
    return [i["id"] for i in items]


# ---------------------------------------------------------------------------
# stats and schema
# ---------------------------------------------------------------------------
def test_stats_count_tables_kinds_tests_and_generated(web):
    client, _repo = web
    s = client.get("/api/graph/stats").json()
    assert s["repo"] == REPO
    assert {t["table"]: t["rows"] for t in s["node_tables"]} == {
        "Files": 4, "Packages": 2, "Classes": 6, "Methods": 6, "Directories": 5, "Topics": 1}
    edges = {t["table"]: t for t in s["edge_tables"]}
    assert edges["MethodCalls"] == {"table": "MethodCalls", "source": "Methods", "target": "Methods",
                                    "rows": 4, "dangling": 0}
    assert edges["PackageImports"]["source"] == "Files" and edges["PackageImports"]["target"] == "Packages"
    assert s["kinds"]["pseudo"] == 4 and s["kinds"]["struct"] == 1 and s["kinds"]["interface_method"] == 1
    assert s["topics"] == "files"
    # fee_test.go, its <package> and TestCalculate; zz_gen.go, its <package> and generatedTable
    assert s["tests"] == 3 and s["generated"] == 3
    assert s["limits"] == {"nodes": 300, "edges": 3000, "search": 50, "path_hops": 10}


def test_inference_from_rows_matches_the_schema(web, monkeypatch):
    """Without graph_generator (the webapp image), tables are read from the rows:
    the primary key first, then an edge's source and destination columns."""
    client, _repo = web
    for name, (pk, cols, (scol, _st), (dcol, _dt)) in schema.EDGE_SPEC.items():
        assert [c for c, _t in cols][:3] == [pk, scol, dcol], name
    for name, (pk, cols) in schema.NODE_SPEC.items():
        assert cols[0][0] == pk, name
    with_spec = (client.get("/api/graph/stats").json(),
                 client.get(f"/api/graph/neighbors/{M_CALC}", params={"depth": 2}).json())
    monkeypatch.setattr(graph_api, "_spec", lambda: ({}, {}))
    monkeypatch.setattr(graph_api, "_graphs", OrderedDict())
    assert (client.get("/api/graph/stats").json(),
            client.get(f"/api/graph/neighbors/{M_CALC}", params={"depth": 2}).json()) == with_spec


def test_the_schema_is_optional(monkeypatch):
    graph_api._spec.cache_clear()
    monkeypatch.setitem(sys.modules, "graph_generator.setup_spanner_graph", None)
    try:
        assert graph_api._spec() == ({}, {})
    finally:
        graph_api._spec.cache_clear()
    monkeypatch.undo()
    nodes, edges = graph_api._spec()
    assert nodes["Methods"] == "method_id"
    assert edges["MethodCalls"] == ("caller_method", "Methods", "callee_method", "Methods")


def test_a_repo_without_graph_rows_is_a_404_with_a_hint(web, tmp_path):
    client, _repo = web
    (tmp_path / "repos" / "bare").mkdir(parents=True)
    resp = client.get("/api/graph/stats", params={"repo": "bare"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "graph_rows not found in bare"
    assert "--graph" in resp.json()["detail"]["hint"]


def test_an_unknown_repo_is_the_docs_api_404(web):
    client, _repo = web
    for repo in ("gone", "../repos", "iryo/../.."):
        resp = client.get("/api/graph/stats", params={"repo": repo})
        assert resp.status_code == 404 and resp.json()["detail"]["repos"] == [REPO]


def test_a_corrupt_row_is_a_500_naming_the_line(web):
    client, repo = web
    path = repo / "graph_rows" / "Methods.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    resp = client.get("/api/graph/stats")
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"].startswith("graph_rows/Methods.jsonl line 7: ")


def test_dangling_edges_and_rows_that_are_not_objects_are_skipped(web):
    client, repo = web
    tables = base_tables()
    tables["MethodCalls"].append(_edge("MethodCalls", M_MAIN, "a_peer000000000000", call_line=9))
    write_tables(repo, tables)
    path = repo / "graph_rows" / "Methods.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "[1, 2]\n\n", encoding="utf-8")
    s = client.get("/api/graph/stats").json()
    calls = next(t for t in s["edge_tables"] if t["table"] == "MethodCalls")
    assert (calls["rows"], calls["dangling"]) == (4, 1)
    assert {"table": "Methods", "rows": 6} in s["node_tables"]


def test_tables_without_rows_and_unknown_tables(web):
    client, repo = web
    (repo / "graph_rows" / "ClassEmbeds.jsonl").write_text("", encoding="utf-8")
    (repo / "graph_rows" / "Mystery.jsonl").write_text("", encoding="utf-8")
    (repo / "graph_rows" / "stats.json").write_text("{}", encoding="utf-8")
    (repo / "graph_rows" / "bad name.jsonl").write_text("{}\n", encoding="utf-8")
    s = client.get("/api/graph/stats").json()
    assert {"table": "ClassEmbeds", "source": "Classes", "target": "Classes", "rows": 0,
            "dangling": 0} in s["edge_tables"]
    assert s["empty_tables"] == ["Mystery"]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def test_search_ranks_the_exact_qualname_first(web):
    client, _repo = web
    r = client.get("/api/graph/search", params={"q": "FeeCalculator.Calculate"}).json()
    assert ids(r["results"])[0] == M_CALC and r["truncated"] is False
    # the name: both Calculate methods before the test function that contains the word
    labels = [x["label"] for x in client.get("/api/graph/search", params={"q": "calculate"}).json()["results"]]
    assert labels[:2] == ["Calculator.Calculate", "FeeCalculator.Calculate"]
    assert labels.index("TestCalculate") > 1
    # a path, an import path, a package-qualified name and an ID are exact keys too
    for q, want in ((FEE, F[FEE]), ("example.com/iryo/billing", PKG_BILLING),
                    ("billing.FeeCalculator", C_FEE), (M_MAIN, M_MAIN), ("reception/cmd", DIRS["reception/cmd"])):
        assert ids(client.get("/api/graph/search", params={"q": q}).json()["results"])[0] == want, q
    # a word inside another key ranks after the names that contain it
    inner = client.get("/api/graph/search", params={"q": "ryo/billing"}).json()["results"]
    assert inner and all("ryo/billing" in (x["sub"] + x["path"]) or x["table"] == "Packages" for x in inner)
    first = client.get("/api/graph/search", params={"q": "FeeCalculator.Calculate"}).json()["results"][0]
    assert first == {"id": M_CALC, "table": "Methods", "kind": "method", "label": "FeeCalculator.Calculate",
                     "sub": "example.com/iryo/billing", "path": FEE, "line": 13, "test": False,
                     "generated": False}


def test_search_filters_by_kind_and_hides_tests_and_generated_code(web):
    client, _repo = web

    def found(**params):
        return {x["label"] for x in client.get("/api/graph/search", params=params).json()["results"]}

    assert found(q="go", kind="Files") == {"fee.go", "fee_test.go", "zz_gen.go", "main.go"}
    assert found(q="calc", kind="interface") == {"Calculator"}
    assert "TestCalculate" not in found(q="calculate", hide_tests=True)
    assert found(q="go", kind="Files", hide_tests=True, hide_generated=True) == {"fee.go", "main.go"}
    assert found(q="fee calculate") == {"FeeCalculator.Calculate"}
    assert client.get("/api/graph/search", params={"q": "  "}).json() == {
        "query": "  ", "results": [], "total": 0, "truncated": False}


@pytest.mark.parametrize("params, message", [
    ({"q": "x", "kind": "Widgets"}, "unknown kind 'Widgets'"),
    ({"q": "x" * 201}, "q is longer than 200 characters"),
    ({"q": "x", "limit": 51}, "limit must be between 1 and 50"),
    ({"q": "x", "limit": 0}, "limit must be between 1 and 50"),
])
def test_search_rejects_bad_parameters(web, params, message):
    client, _repo = web
    resp = client.get("/api/graph/search", params=params)
    assert resp.status_code == 400 and resp.json()["detail"].startswith(message)


def test_search_returns_at_most_fifty(web):
    client, repo = web
    tables = base_tables()
    tables["Methods"] += [_method(nid("method", FEE, f"Helper{i}"), FEE, f"Helper{i}", PSEUDO[FEE])
                          for i in range(60)]
    write_tables(repo, tables)
    r = client.get("/api/graph/search", params={"q": "helper"}).json()
    assert len(r["results"]) == 50 and r["total"] == 60 and r["truncated"] is True
    assert len(client.get("/api/graph/search", params={"q": "helper", "limit": 5}).json()["results"]) == 5


# ---------------------------------------------------------------------------
# node details
# ---------------------------------------------------------------------------
def test_node_details_have_columns_summary_location_edges_and_links(web):
    client, _repo = web
    d = client.get(f"/api/graph/node/{M_CALC}").json()
    assert d["node"]["label"] == "FeeCalculator.Calculate"
    assert d["columns"]["signature"] == "" and d["columns"]["member_kind"] == "method"
    assert "summary" not in d["columns"]
    assert (d["summary"], d["summary_source"], d["doc"]) == ("点数から料金を出す。", "wiki", "")
    assert d["location"] == {"path": FEE, "start_line": 13, "end_line": 17}
    assert d["module"] == "example.com/iryo/billing"
    assert {(e["table"], e["direction"], e["count"], e["peer"]) for e in d["edges"]} == {
        ("MethodCalls", "in", 1, "Methods"), ("MethodCalls", "out", 1, "Methods"),
        ("PossiblyCalls", "in", 1, "Methods"), ("ClassDefinesMethod", "in", 1, "Classes")}
    assert d["refs"]["class_id"]["label"] == "FeeCalculator" and d["refs"]["file_id"]["id"] == F[FEE]
    assert d["links"] == [{"type": "summary", "kind": "files", "name": FEE},
                          {"type": "topic", "name": "会計", "label": "会計"}]


def test_details_fall_back_to_the_doc_comment_and_list_the_wiki_pages(web):
    client, _repo = web
    struct = client.get(f"/api/graph/node/{C_FEE}").json()   # no wiki summary for the type
    assert (struct["summary"], struct["summary_source"]) == ("FeeCalculator は料金計算機。", "graph_rows")
    package = client.get(f"/api/graph/node/{PKG_BILLING}").json()
    assert (package["summary"], package["doc"]) == ("会計パッケージ。", "Package billing は料金を計算する。")
    assert package["links"] == [{"type": "summary", "kind": "packages", "name": "billing|billing"}]
    root = client.get(f"/api/graph/node/{DIRS['']}").json()
    assert root["node"]["sub"] == "/" and root["summary"] == "リポジトリ全体。"
    assert root["links"] == [{"type": "summary", "kind": "dirs", "name": "/"}]
    # a directory that holds no file is found by the ID scheme
    middle = client.get(f"/api/graph/node/{DIRS['reception/cmd']}").json()
    assert middle["node"]["path"] == "reception/cmd" and middle["summary"] == "コマンド群。"
    assert middle["links"] == [{"type": "summary", "kind": "dirs", "name": "reception/cmd"}]
    topic = client.get(f"/api/graph/node/{TOPIC}").json()
    assert topic["summary"].startswith("# 会計") and topic["links"] == [
        {"type": "topic", "name": "会計", "label": "会計"}]
    assert topic["roles"] == {} and topic["unassigned"] is False
    bare = client.get(f"/api/graph/node/{M_MAIN}").json()
    assert (bare["summary"], bare["summary_source"], bare["links"]) == (
        "", None, [{"type": "topic", "name": "会計", "label": "会計"}])


def test_unreadable_wiki_files_give_no_summary_and_no_link(web):
    client, repo = web
    (repo / "summaries" / "dirs.json").write_text("{broken", encoding="utf-8")
    _write_json(repo / "summaries" / "packages.json", ["not", "a", "dict"])
    _write_json(repo / "topics" / "topic_tree.json", {"not": "a list"})
    root = client.get(f"/api/graph/node/{DIRS['']}").json()
    assert (root["summary"], root["links"]) == ("", [])
    package = client.get(f"/api/graph/node/{PKG_BILLING}").json()
    assert package["summary_source"] == "graph_rows" and package["links"] == []
    assert client.get(f"/api/graph/node/{TOPIC}").json()["links"] == []


def test_a_topic_named_twice_links_by_its_id(web):
    client, repo = web
    _write_json(repo / "topics" / "topic_tree.json", [{"id": "other", "name": "会計"},
                                                       {"id": TOPIC, "name": "会計"}])
    (repo / "topics" / "other.md").write_text("# 他\n", encoding="utf-8")
    assert client.get(f"/api/graph/node/{TOPIC}").json()["links"] == [
        {"type": "topic", "name": TOPIC, "label": "会計"}]


@pytest.mark.parametrize("url", [
    "/api/graph/node/a_0000000000000000", "/api/graph/neighbors/a_0000000000000000",
    "/api/graph/node/" + "x" * 300,
    f"/api/graph/path?source=a_0000000000000000&target={M_MAIN}",
])
def test_an_unknown_node_is_a_404(web, url):
    client, _repo = web
    resp = client.get(url)
    assert resp.status_code == 404 and "not found" in resp.json()["detail"]["error"]


# ---------------------------------------------------------------------------
# neighbourhood
# ---------------------------------------------------------------------------
def test_neighbours_follow_every_table_both_ways_at_depth_one(web):
    client, _repo = web
    r = client.get(f"/api/graph/neighbors/{M_CALC}").json()
    assert r["center"] == M_CALC and r["truncated"] is False and r["omitted"] == {}
    assert {n["id"]: n["depth"] for n in r["nodes"]} == {
        M_CALC: 0, M_TEST: 1, M_GEN: 1, M_MAIN: 1, C_FEE: 1}
    assert {(e["table"], e["source"], e["target"]) for e in r["edges"]} == {
        ("MethodCalls", M_TEST, M_CALC), ("MethodCalls", M_CALC, M_GEN),
        ("PossiblyCalls", M_MAIN, M_CALC), ("ClassDefinesMethod", C_FEE, M_CALC)}
    call = next(e for e in r["edges"] if e["source"] == M_TEST)
    assert call["id"] == eid("MethodCalls", M_TEST, M_CALC)
    assert call["attrs"] == {"callee_name": "Calculate", "resolution": "types", "site_kind": "call",
                             "call_line": 8}
    center = next(n for n in r["nodes"] if n["id"] == M_CALC)
    assert center["degree"] == 4


def test_neighbours_by_table_direction_depth_and_flags(web):
    client, _repo = web

    def nodes(**params):
        return set(ids(client.get(f"/api/graph/neighbors/{M_CALC}", params=params).json()["nodes"]))

    assert nodes(tables="MethodCalls", direction="in") == {M_CALC, M_TEST}
    assert nodes(tables="MethodCalls,PossiblyCalls", direction="in") == {M_CALC, M_TEST, M_MAIN}
    assert nodes(tables="MethodCalls", hide_tests=True) == {M_CALC, M_GEN}
    assert nodes(tables="MethodCalls", hide_generated=True) == {M_CALC, M_TEST}
    assert nodes(tables="") == {M_CALC}
    # depth 2: main is reached through PossiblyCalls, and its other callees from it
    assert nodes(tables="PossiblyCalls,MethodCalls", depth=2) == {M_CALC, M_TEST, M_GEN, M_MAIN, M_NEW, M_ICALC}
    # a schema table this graph lacks is skipped, not refused
    assert nodes(tables="MethodCalls,CrossRepoCalls", direction="out") == {M_CALC, M_GEN}
    # the center stays even when it is a test
    assert M_TEST in set(ids(client.get(f"/api/graph/neighbors/{M_TEST}", params={"hide_tests": True})
                             .json()["nodes"]))


@pytest.mark.parametrize("params, message", [
    ({"depth": 3}, "depth must be 1 or 2"),
    ({"depth": 0}, "depth must be 1 or 2"),
    ({"direction": "up"}, "direction must be 'out', 'in' or 'both'"),
    ({"tables": "MethodCalls,Widgets"}, "unknown edge table(s): Widgets"),
    ({"limit": 301}, "limit must be between 1 and 300"),
])
def test_neighbours_reject_bad_parameters(web, params, message):
    client, _repo = web
    resp = client.get(f"/api/graph/neighbors/{M_CALC}", params=params)
    assert resp.status_code == 400 and resp.json()["detail"].startswith(message)


def test_neighbours_are_capped_and_every_direction_gets_a_share(web):
    """A hub with 400 callers and 5 callees: 300 nodes, all 5 callees, the
    rest of the callers counted as omitted."""
    client, repo = web
    tables = base_tables()
    callers = [nid("method", FEE, f"Caller{i}") for i in range(400)]
    callees = [nid("method", FEE, f"Callee{i}") for i in range(5)]
    tables["Methods"] += [_method(m, FEE, q, PSEUDO[FEE]) for m, q in
                          zip(callers + callees, [f"Caller{i}" for i in range(400)] + [f"Callee{i}" for i in range(5)])]
    tables["MethodCalls"] += [_edge("MethodCalls", c, M_NEW, call_line=1) for c in callers] \
        + [_edge("MethodCalls", M_NEW, c, call_line=2) for c in callees]
    write_tables(repo, tables)
    r = client.get(f"/api/graph/neighbors/{M_NEW}", params={"tables": "MethodCalls"}).json()
    got = set(ids(r["nodes"]))
    assert len(got) == 300 and set(callees) <= got
    assert r["truncated"] is True and r["omitted"] == {"MethodCalls": 400 + 1 - 294}  # main calls it too
    small = client.get(f"/api/graph/neighbors/{M_NEW}", params={"tables": "MethodCalls", "limit": 10}).json()
    assert len(small["nodes"]) == 10 and small["truncated"] is True


def test_neighbour_edges_are_capped(web, monkeypatch):
    client, _repo = web
    monkeypatch.setattr(graph_api, "MAX_EDGES", 2)
    r = client.get(f"/api/graph/neighbors/{M_CALC}").json()
    assert len(r["edges"]) == 2 and r["truncated"] is True and r["omitted"] == {}


# ---------------------------------------------------------------------------
# shortest path
# ---------------------------------------------------------------------------
def test_path_finds_a_shortest_route_over_the_chosen_tables(web):
    client, _repo = web
    r = client.get("/api/graph/path", params={"source": M_MAIN, "target": M_GEN,
                                              "tables": "MethodCalls,PossiblyCalls"}).json()
    assert r["hops"] == 2 and ids(r["nodes"]) == [M_MAIN, M_CALC, M_GEN]
    assert [(e["table"], e["source"], e["target"]) for e in r["edges"]] == [
        ("PossiblyCalls", M_MAIN, M_CALC), ("MethodCalls", M_CALC, M_GEN)]
    assert [n["depth"] for n in r["nodes"]] == [0, 1, 2]
    back = client.get("/api/graph/path", params={"source": M_GEN, "target": M_MAIN, "direction": "in",
                                                 "tables": "MethodCalls,PossiblyCalls"}).json()
    assert ids(back["nodes"]) == [M_GEN, M_CALC, M_MAIN]
    same = client.get("/api/graph/path", params={"source": M_MAIN, "target": M_MAIN}).json()
    assert same == {"nodes": [same["nodes"][0]], "edges": [], "hops": 0}


def test_no_path_is_a_404_and_bad_hops_a_400(web):
    client, _repo = web
    resp = client.get("/api/graph/path", params={"source": M_MAIN, "target": M_GEN,
                                                 "tables": "MethodCalls", "max_hops": 3})
    assert resp.status_code == 404 and resp.json()["detail"]["error"].startswith(
        "no path from main to generatedTable within 3 hops")
    resp = client.get("/api/graph/path", params={"source": M_MAIN, "target": M_GEN, "max_hops": 11})
    assert resp.status_code == 400 and resp.json()["detail"] == "max_hops must be between 1 and 10"


# ---------------------------------------------------------------------------
# overviews
# ---------------------------------------------------------------------------
def test_package_overview_aggregates_file_imports_per_package(web):
    client, _repo = web
    r = client.get("/api/graph/overview/packages").json()
    assert r["mode"] == "packages" and r["truncated"] is False
    by_id = {n["id"]: n for n in r["nodes"]}
    assert by_id[PKG_BILLING]["size"] == 3 and by_id[PKG_BILLING]["imported_by"] == 1
    assert by_id[PKG_MAIN]["module"] == "example.com/iryo/reception" and by_id[PKG_MAIN]["imports"] == 1
    assert r["edges"] == [{"id": f"PackageImports:{PKG_MAIN}>{PKG_BILLING}", "table": "PackageImports",
                           "source": PKG_MAIN, "target": PKG_BILLING, "attrs": {"weight": 1}}]
    one = client.get("/api/graph/overview/packages", params={"limit": 1}).json()
    assert len(one["nodes"]) == 1 and one["edges"] == [] and one["omitted"] == {"Packages": 1}


def test_package_overview_hides_imports_from_test_files(web):
    client, repo = web
    tables = base_tables()
    tables["PackageImports"].append(_edge("PackageImports", F[FEE_TEST], PKG_MAIN, kind="normal"))
    write_tables(repo, tables)
    assert len(client.get("/api/graph/overview/packages").json()["edges"]) == 2
    assert len(client.get("/api/graph/overview/packages", params={"hide_tests": True}).json()["edges"]) == 1


def test_directory_overview_is_the_tree_of_directories_with_files(web):
    client, _repo = web
    r = client.get("/api/graph/overview/directories").json()
    assert r["mode"] == "directories" and r["truncated"] is False
    nodes = {n["path"]: n for n in r["nodes"]}
    assert list(nodes) == ["", "billing", "reception", "reception/cmd", "reception/cmd/desk"]
    assert {p: n["id"] for p, n in nodes.items()} == DIRS          # real nodes, no virtual ones
    assert nodes[""]["label"] == "iryo-kanri-system" and nodes[""]["total"] == 4
    assert (nodes["billing"]["files"], nodes["reception"]["files"], nodes["reception"]["total"]) == (3, 0, 1)
    assert {(e["source"], e["target"]) for e in r["edges"]} == {
        (DIRS[""], DIRS["billing"]), (DIRS[""], DIRS["reception"]),
        (DIRS["reception"], DIRS["reception/cmd"]), (DIRS["reception/cmd"], DIRS["reception/cmd/desk"])}
    cut = client.get("/api/graph/overview/directories", params={"limit": 2}).json()
    assert [n["path"] for n in cut["nodes"]] == ["", "billing"]
    assert cut["truncated"] is True and cut["omitted"] == {"Directories": 3}
    assert cut["nodes"][0]["more"] == 1
    hidden = client.get("/api/graph/overview/directories", params={"hide_tests": True,
                                                                   "hide_generated": True}).json()
    assert {n["path"]: n["files"] for n in hidden["nodes"]}["billing"] == 1


def test_directory_overview_without_directory_rows_uses_paths(web):
    client, repo = web
    tables = base_tables()
    del tables["Directories"], tables["DirContainsFile"]
    write_tables(repo, tables)
    (repo / "graph_rows" / "Directories.jsonl").unlink()
    (repo / "graph_rows" / "DirContainsFile.jsonl").unlink()
    r = client.get("/api/graph/overview/directories").json()
    assert [n["id"] for n in r["nodes"]][:2] == ["dir:", "dir:billing"]
    assert r["nodes"][0]["virtual"] is True and r["nodes"][0]["label"] == "/"
    assert {"source": "dir:", "target": "dir:billing"}.items() <= r["edges"][0].items()


def test_topic_overview_of_llm_topics_lists_their_files(web):
    client, _repo = web
    r = client.get("/api/graph/overview/topics").json()
    assert r["mode"] == "files" and r["truncated"] is False
    assert {n["id"]: n.get("files") for n in r["nodes"]} == {TOPIC: 2, F[FEE]: None, F[MAIN]: None}
    assert {(e["source"], e["target"], e["table"]) for e in r["edges"]} == {
        (F[FEE], TOPIC, "FileBelongsToTopic"), (F[MAIN], TOPIC, "FileBelongsToTopic")}
    capped = client.get("/api/graph/overview/topics", params={"limit": 2}).json()
    assert len(capped["nodes"]) == 2 and capped["omitted"] == {"Files": 1}


def test_a_repo_without_topics_has_an_empty_topic_map(web):
    client, repo = web
    for table in ("Topics", "FileBelongsToTopic"):
        (repo / "graph_rows" / f"{table}.jsonl").unlink()
    assert client.get("/api/graph/stats").json()["topics"] == "none"
    assert client.get("/api/graph/overview/topics").json() == {
        "mode": "none", "nodes": [], "edges": [], "truncated": False, "omitted": {}}


def test_overview_names_and_limits_are_checked(web):
    client, _repo = web
    resp = client.get("/api/graph/overview/modules")
    assert resp.status_code == 404 and "packages, directories, topics" in resp.json()["detail"]
    assert client.get("/api/graph/overview/packages", params={"limit": 0}).status_code == 400


# ---------------------------------------------------------------------------
# codedoc_go_leiden's topic tables
# ---------------------------------------------------------------------------
L1, L1B, L2A, L2B, UNASSIGNED = "t_l1", "t_l1b", "t_l2a", "t_l2b", "t_none"


def leiden_tables() -> dict[str, list[dict]]:
    tables = base_tables()

    def topic(tid, name, level, parent, size, source="hub"):
        return {"topic_id": tid, "name": name, "summary": "", "repo": REPO, "embedding": None,
                "level": level, "parent_topic_id": parent, "size": size, "file_count": 1,
                "cohesion": 0.5, "conductance": 0.1, "hub_class_id": C_FEE, "label_source": source,
                "basis": "structure", "membership_hash": tid}

    tables["Topics"] = [topic(L1, "billing.FeeCalculator", 1, None, 5), topic(L1B, "desk.main", 1, None, 2),
                        topic(L2A, "料金", 2, L1, 3), topic(L2B, "税", 2, L1, 2),
                        topic(UNASSIGNED, "未分類", 1, None, 4, source="fixed")]
    tables["TopicContainsTopic"] = [{"edge_id": eid("TopicContainsTopic", L1, c), "parent_topic": L1,
                                     "child_topic": c} for c in (L2A, L2B)]
    tables["TopicDependsOn"] = [
        {"edge_id": eid("TopicDependsOn", L1B, L2A), "source_topic": L1B, "target_topic": L2A,
         "weight": 2.5, "call_count": 2},
        {"edge_id": eid("TopicDependsOn", L2A, L2B), "source_topic": L2A, "target_topic": L2B,
         "weight": 7.0, "call_count": 0}]
    tables["ClassBelongsToTopic"] = [
        {"edge_id": eid("ClassBelongsToTopic", c, t), "class_id": c, "topic_id": t, "role": role}
        for c, t, role in ((C_FEE, L1, "primary"), (C_FEE, L2A, "primary"), (C_CALC, L1, "attached-file"),
                           (C_CALC, L2B, "attached-file"), (PSEUDO[FEE_TEST], UNASSIGNED, "unassigned"))]
    tables["FileBelongsToTopic"] = [
        {"edge_id": eid("FileBelongsToTopic", F[FEE], L2A), "file_id": F[FEE], "topic_id": L2A, "role": "primary"},
        {"edge_id": eid("FileBelongsToTopic", F[MAIN], L1B), "file_id": F[MAIN], "topic_id": L1B,
         "role": "primary"}]
    return tables


def test_leiden_topic_map_nests_levels_weights_dependencies_and_flags_unassigned(web):
    client, repo = web
    write_tables(repo, leiden_tables())
    s = client.get("/api/graph/stats").json()
    assert s["topics"] == "levels"
    edges = {t["table"]: (t["source"], t["target"], t["rows"]) for t in s["edge_tables"]}
    assert edges["ClassBelongsToTopic"] == ("Classes", "Topics", 5)
    assert edges["TopicDependsOn"] == ("Topics", "Topics", 2)
    assert edges["TopicContainsTopic"] == ("Topics", "Topics", 2)
    r = client.get("/api/graph/overview/topics").json()
    assert r["mode"] == "levels" and r["truncated"] is False
    nodes = {n["id"]: n for n in r["nodes"]}
    assert [n["id"] for n in r["nodes"]] == [L1, UNASSIGNED, L1B, L2A, L2B]   # level, then size
    assert (nodes[L2A]["parent"], nodes[L2B]["parent"], nodes[L1]["parent"]) == (L1, L1, None)
    assert nodes[UNASSIGNED]["unassigned"] is True and nodes[L1]["unassigned"] is False
    assert nodes[L1]["roles"] == {"primary": 1, "attached-file": 1}
    assert nodes[UNASSIGNED]["roles"] == {"unassigned": 1}
    assert (nodes[L1]["size"], nodes[L1]["files"], nodes[L1]["label_source"]) == (5, 1, "hub")
    assert [(e["source"], e["target"], e["attrs"]["weight"]) for e in r["edges"]] == [
        (L2A, L2B, 7.0), (L1B, L2A, 2.5)]
    d = client.get(f"/api/graph/node/{L1}").json()
    assert d["roles"] == {"primary": 1, "attached-file": 1}
    assert d["refs"]["hub_class_id"]["id"] == C_FEE
    assert client.get(f"/api/graph/node/{UNASSIGNED}").json()["unassigned"] is True
    cut = client.get("/api/graph/overview/topics", params={"limit": 3}).json()
    assert [n["id"] for n in cut["nodes"]] == [L1, UNASSIGNED, L1B] and cut["omitted"] == {"Topics": 2}


def test_leiden_class_topics_are_linked_and_expandable(web):
    client, repo = web
    write_tables(repo, leiden_tables())
    _write_json(repo / "topics" / "topic_tree.json", [{"id": L1, "name": "billing.FeeCalculator",
                                                       "subtopics": [{"id": L2A, "name": "料金"}]}])
    (repo / "topics" / f"{L1}.md").write_text("# 会計\n", encoding="utf-8")
    (repo / "topics" / f"{L2A}.md").write_text("# 料金\n", encoding="utf-8")
    links = client.get(f"/api/graph/node/{M_CALC}").json()["links"]
    # the method's class belongs to L1 and L2A; only L1 is a page the docs API serves
    assert links == [{"type": "summary", "kind": "files", "name": FEE},
                     {"type": "topic", "name": "billing.FeeCalculator", "label": "billing.FeeCalculator"}]
    r = client.get(f"/api/graph/neighbors/{L2A}", params={"tables": "ClassBelongsToTopic,TopicDependsOn"}).json()
    assert set(ids(r["nodes"])) == {L2A, C_FEE, L1B, L2B}
    role = next(e for e in r["edges"] if e["table"] == "ClassBelongsToTopic")
    assert role["attrs"] == {"role": "primary"}


# ---------------------------------------------------------------------------
# caching and path safety
# ---------------------------------------------------------------------------
def test_the_graph_is_cached_and_reloaded_when_a_file_changes(web):
    client, repo = web
    first = graph_api.load_graph(repo)
    assert graph_api.load_graph(repo) is first
    assert client.get("/api/graph/stats").json()["node_tables"][3] == {"table": "Methods", "rows": 6}
    tables = base_tables()
    tables["Methods"].append(_method(nid("method", FEE, "Round"), FEE, "Round", PSEUDO[FEE]))
    path = repo / "graph_rows" / "Methods.jsonl"
    stamp = path.stat().st_mtime_ns
    write_tables(repo, {"Methods": tables["Methods"]})
    os.utime(path, ns=(stamp + 10**9, stamp + 10**9))
    assert graph_api.load_graph(repo) is not first
    assert {"table": "Methods", "rows": 7} in client.get("/api/graph/stats").json()["node_tables"]
    assert client.get("/api/graph/search", params={"q": "Round"}).json()["results"][0]["label"] == "Round"


def test_a_file_that_vanishes_while_reading_is_a_503(web, monkeypatch):
    client, _repo = web

    def gone(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(graph_api, "_read_rows", gone)
    resp = client.get("/api/graph/stats")
    assert resp.status_code == 503 and "retry" in resp.json()["detail"]["hint"]


def test_a_directory_that_vanishes_while_listing_is_a_503(web, monkeypatch):
    client, _repo = web

    def gone(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(graph_api, "_table_files", gone)
    assert client.get("/api/graph/stats").status_code == 503


def test_only_a_few_repos_stay_in_memory(web, tmp_path, monkeypatch):
    client, _repo = web
    monkeypatch.setattr(graph_api, "CACHED_REPOS", 2)
    for name in ("a", "b", "c"):
        write_tables(tmp_path / "repos" / name, base_tables())
        assert client.get("/api/graph/stats", params={"repo": name}).status_code == 200
    assert len(graph_api._graphs) == 2


def test_paths_outside_the_repo_dir_are_never_read(web, tmp_path):
    client, repo = web
    # a file whose path climbs out of summaries/nodes, with a "summary" waiting there
    tables = base_tables()
    escape = "../../../../outside/x.go"
    tables["Files"][0]["path"] = escape
    tables["Files"][1]["path"] = "billing/fee\x00_test.go"
    secret = tmp_path / "outside" / "x.go.json"
    _write_json(secret, {"file": "読んではいけない。", "nodes": {}})
    # a topic page outside topics/ and a graph_rows symlink to a file outside
    _write_json(repo / "topics" / "topic_tree.json", [{"id": "../../../outside/page", "name": "会計"}])
    (tmp_path / "outside" / "page.md").write_text("# 外\n", encoding="utf-8")
    write_tables(repo, tables)
    outside_rows = tmp_path / "outside" / "Leak.jsonl"
    outside_rows.write_text(json.dumps({"leak_id": "x", "name": "leak"}) + "\n", encoding="utf-8")
    (repo / "graph_rows" / "Leak.jsonl").symlink_to(outside_rows)
    d = client.get(f"/api/graph/node/{F[FEE]}").json()
    assert d["summary"] == "" and d["links"] == []
    assert client.get(f"/api/graph/node/{F[FEE_TEST]}").json()["summary"] == ""
    assert "Leak" not in {t["table"] for t in client.get("/api/graph/stats").json()["node_tables"]}
    assert client.get("/api/graph/search", params={"q": "leak"}).json()["total"] == 0
