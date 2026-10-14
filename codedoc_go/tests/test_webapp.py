"""webapp/main.py serves one repo's wiki from OUTPUT_DIR/repos/<repo>/ in the
layout of contract C7, chosen by ?repo=, WEBAPP_REPO or the only repo present."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TREE = [
    {"id": "t-billing", "name": "会計", "summary": "料金の計算", "files": ["billing/fee.go"],
     "subtopics": [{"name": "税", "summary": "税の計算", "files": ["billing/fee.go"]}]},
    {"id": "t-reception", "name": "受付", "summary": "受付の流れ", "files": [], "subtopics": []},
]


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                    encoding="utf-8")


def _repo(root: Path, name: str) -> Path:
    repo = root / "repos" / name
    _write(repo / "index.md", f"# {name} の索引\n")
    _write(repo / "metadata.json", {"repo": name})
    _write(repo / "topics" / "topic_tree.json", TREE)
    _write(repo / "topics" / "t-billing.md", "# 会計\n\n料金を計算します。\n")
    _write(repo / "summaries" / "nodes" / "billing" / "fee.go.json", {
        "sha256": "x", "mode": "batched", "model": "m", "prompt_version": 1,
        "file": "料金計算のファイルです。",
        "nodes": {"FeeCalculator": "料金計算機。", "FeeCalculator.Calculate": "点数から料金を出す。",
                  "<init>": "初期化。"}})
    _write(repo / "summaries" / "nodes" / "main.go.json", {"file": "入口。", "nodes": {}})
    _write(repo / "summaries" / "dirs.json", {"": {"hash": "h0", "summary": "リポジトリ全体。"},
                                              "billing": {"hash": "h1", "summary": "会計の部品。"}})
    _write(repo / "summaries" / "packages.json",
           {"billing|billing": {"hash": "h2", "summary": "会計パッケージ。"}})
    return repo


@pytest.fixture
def web(tmp_path, monkeypatch):
    """(client, output_root) with the webapp pointed at an empty OUTPUT_DIR."""
    main = importlib.import_module("webapp.main")
    monkeypatch.setattr(main, "OUTPUT_ROOT", tmp_path)
    monkeypatch.delenv("WEBAPP_REPO", raising=False)
    return TestClient(main.app), tmp_path


def test_the_only_repo_is_chosen(web):
    client, root = web
    _repo(root, "iryo")
    assert client.get("/api/docs/index").json()["markdown"] == "# iryo の索引\n"
    assert client.get("/api/repos").json() == {"repos": ["iryo"], "current": "iryo"}


def test_webapp_repo_and_the_query_parameter_choose_among_several(web, monkeypatch):
    client, root = web
    _repo(root, "iryo")
    _repo(root, "shared")
    (root / "repos" / "stray.txt").write_text("not a repo", encoding="utf-8")
    monkeypatch.setenv("WEBAPP_REPO", "shared")
    assert client.get("/api/docs/index").json()["markdown"] == "# shared の索引\n"
    assert client.get("/api/docs/index?repo=iryo").json()["markdown"] == "# iryo の索引\n"
    assert client.get("/api/repos").json() == {"repos": ["iryo", "shared"], "current": "shared"}


@pytest.mark.parametrize("env, query, error", [
    (None, "", "no repo selected"),
    ("gone", "", "repo 'gone' not found"),
    (None, "?repo=../repos", "repo '../repos' not found"),
])
def test_unresolved_repo_is_a_404_listing_the_repos(web, monkeypatch, env, query, error):
    client, root = web
    _repo(root, "iryo")
    _repo(root, "shared")
    if env:
        monkeypatch.setenv("WEBAPP_REPO", env)
    resp = client.get("/api/docs/tree" + query)
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == error and detail["repos"] == ["iryo", "shared"]
    assert "WEBAPP_REPO" in detail["hint"]


def test_no_repos_at_all(web):
    client, root = web
    resp = client.get("/api/docs/index")
    detail = resp.json()["detail"]
    assert resp.status_code == 404 and detail["repos"] == []
    assert detail["error"] == f"no repo under {root / 'repos'}" and "analyze" in detail["hint"]
    assert client.get("/api/repos").json() == {"repos": [], "current": None}


def test_tree_lists_topics_files_dirs_and_packages(web):
    client, root = web
    _repo(root, "iryo")
    assert client.get("/api/docs/tree").json() == {
        "topics": ["会計", "受付"],
        "summaries": {"files": ["billing/fee.go", "main.go"], "dirs": ["/", "billing"],
                      "packages": ["billing|billing"]}}


def test_tree_of_a_repo_without_wiki_output_is_empty(web):
    client, root = web
    (root / "repos" / "iryo").mkdir(parents=True)
    assert client.get("/api/docs/tree").json() == {
        "topics": [], "summaries": {"files": [], "dirs": [], "packages": []}}


def test_topics_are_the_topic_tree(web):
    client, root = web
    _repo(root, "iryo")
    assert client.get("/api/docs/topics").json() == TREE


@pytest.mark.parametrize("ref", ["会計", "t-billing"])
def test_topic_page_is_found_by_name_or_id(web, ref):
    client, root = web
    _repo(root, "iryo")
    body = client.get(f"/api/docs/topics/{ref}").json()
    assert body["name"] == "会計" and body["id"] == "t-billing"
    assert body["markdown"].startswith("# 会計") and "<h1" in body["html"]


@pytest.mark.parametrize("ref", ["受付", "missing"])
def test_topic_without_a_page_is_a_404(web, ref):
    client, root = web
    _repo(root, "iryo")
    assert client.get(f"/api/docs/topics/{ref}").status_code == 404


def test_file_summary_is_built_from_the_node_cache(web):
    client, root = web
    _repo(root, "iryo")
    for url in ("/api/docs/summaries/files/billing/fee.go",
                "/api/docs/summaries/files/billing%2Ffee.go"):
        body = client.get(url).json()
        assert body["name"] == "billing/fee.go" and body["kind"] == "files"
        assert body["markdown"] == (
            "# `billing/fee.go`\n\n料金計算のファイルです。\n\n"
            "## `FeeCalculator`\n\n料金計算機。\n\n"
            "## `FeeCalculator.Calculate`\n\n点数から料金を出す。\n\n"
            "## `<init>`\n\n初期化。\n")
        assert "<code>&lt;init&gt;</code>" in body["html"]


@pytest.mark.parametrize("kind, ref, name, text", [
    ("dirs", "billing", "billing", "会計の部品。"),
    ("dirs", "%2F", "/", "リポジトリ全体。"),
    ("dirs", "%2E", "/", "リポジトリ全体。"),
    ("packages", "billing|billing", "billing|billing", "会計パッケージ。"),
])
def test_dir_and_package_summaries(web, kind, ref, name, text):
    client, root = web
    _repo(root, "iryo")
    body = client.get(f"/api/docs/summaries/{kind}/{ref}").json()
    assert body["name"] == name and body["markdown"] == f"# `{name}`\n\n{text}\n"


def test_root_dir_key_may_be_a_dot(web):
    client, root = web
    repo = _repo(root, "iryo")
    _write(repo / "summaries" / "dirs.json", {".": {"hash": "h", "summary": "全体。"}})
    assert client.get("/api/docs/tree").json()["summaries"]["dirs"] == ["/"]
    assert client.get("/api/docs/summaries/dirs/%2F").json()["markdown"] == "# `/`\n\n全体。\n"


@pytest.mark.parametrize("url, status", [
    ("/api/docs/summaries/modules/x", 400),
    ("/api/docs/summaries/files/billing/none.go", 404),
    ("/api/docs/summaries/files/..%2F..%2Ftopics%2Ftopic_tree", 404),
    ("/api/docs/summaries/dirs/nowhere", 404),
    ("/api/docs/metadata?repo=nope", 404),
])
def test_bad_summary_requests(web, url, status):
    client, root = web
    _repo(root, "iryo")
    assert client.get(url).status_code == status


def test_metadata_and_missing_files(web):
    client, root = web
    repo = _repo(root, "iryo")
    assert client.get("/api/docs/metadata").json() == {"repo": "iryo"}
    (repo / "index.md").unlink()
    (repo / "topics" / "topic_tree.json").unlink()
    assert client.get("/api/docs/index").status_code == 404
    assert client.get("/api/docs/topics").status_code == 404
    assert client.get("/api/docs/topics/会計").status_code == 404


def test_chat_routes_are_kept():
    main = importlib.import_module("webapp.main")
    paths = {route.path for route in main.app.routes}
    assert {"/api/chat", "/api/chat/stream", "/api/repos"} <= paths
