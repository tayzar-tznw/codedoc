"""crossref: peer targets in resolutions.json become CrossRepoCalls, CrossRepoImports
and CrossRepoRef rows (spec §8.3); everything else is a counted drop or a
module-cache listing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_generator import config, crossref, pipeline
from tests.eval_worlds import external, record, target

SHARED = "example.com/iryo/shared"


def _entities() -> dict:
    """C3 records; sha256 "x" is also what _repo writes into resolutions.json (fresh)."""
    return {
        "web": {
            "orders/orders.go": {"sha256": "x", "package_name": "orders", "origin": "package",
                                 "types": [{"qualname": "<package>", "methods": []},
                                           {"qualname": "Order", "methods": []}],
                                 "funcs": [{"qualname": "Place"}, {"qualname": "<init>"}]},
        },
        "shared": {
            "log/logger.go": {"sha256": "x", "package_name": "log", "origin": "package",
                              "types": [{"qualname": "<package>", "methods": []},
                                        {"qualname": "Logger", "methods": []},
                                        {"qualname": "Sink", "methods": [{"qualname": "Sink.Write"}]}],
                              "funcs": [{"qualname": "New"}, {"qualname": "Logger.Info"}]},
            "contracts/audit.go": {"sha256": "x", "package_name": "contracts", "origin": "package",
                                   "types": [{"qualname": "Audit", "methods": []}], "funcs": []},
        },
    }


def _peer(relpath: str, qualname: str, *, kind: str = "method", repo: str = "shared") -> dict:
    return target(relpath, qualname, kind=kind, repo=repo, unit=SHARED)


def _repo(name: str, files: dict, *, modules=(), entities=None) -> dict:
    res = {"version": 1, "repo": name, "files": {}}
    for rel, parts in files.items():
        res["files"][rel] = {"sha256": "x", "calls": parts.get("calls", []),
                             "imports": parts.get("imports", []), "embeds": parts.get("embeds", []),
                             "implements": parts.get("implements", [])}
    return {"repo": name, "root": f"/repos/{name}", "entities": (entities or _entities()).get(name, {}),
            "resolutions": res, "modules": list(modules)}


def _call(line: int, tgt: dict | None, *, caller: str = "Place", module: str = SHARED, peer=True,
          version: str = "") -> dict:
    return record(line, 7, "external", caller=caller, target=tgt,
                  external=external(module, peer=peer, version=version))


def _import(line: int, *, repo: str | None = "shared", directory: str = "log", clause: str = "log",
            path: str = SHARED + "/log", status: str = "external", module: str = SHARED,
            version: str = "") -> dict:
    tgt = None if repo is None else {"unit": module, "package": path, "dir": directory,
                                     "clause": clause, "repo": repo}
    return {"line": line, "col": 2, "import_path": path, "status": status, "target": tgt,
            "external": external(module, peer=repo is not None, version=version), "reason": None}


def _relation(field: str, tgt: dict | None, *, type_qualname: str = "Order", module: str = SHARED,
              version: str = "") -> dict:
    return {"type": {"relpath": "orders/orders.go", "qualname": type_qualname}, field: tgt,
            "status": "external", "external": external(module, peer=bool(tgt and tgt.get("repo")),
                                                       version=version),
            "pointer" if field == "embedded" else "pointer_receiver": False}


def _derive(*web_files: dict, shared_modules=(SHARED,), extra=()) -> dict:
    files = web_files[0] if web_files else {}
    repos = [_repo("web", files, modules=("example.com/iryo/web",)),
             _repo("shared", {}, modules=shared_modules), *extra]
    return crossref.derive_cross_repo(repos, printer=lambda *a: None)


def test_a_peer_call_becomes_a_cross_repo_call():
    out = _derive({"orders/orders.go": {"calls": [_call(12, _peer("log/logger.go", "New")),
                                                  _call(13, _peer("log/logger.go", "New"))]}})
    sid = pipeline._make_id("method", "web", "orders/orders.go", "Place")
    did = pipeline._make_id("method", "shared", "log/logger.go", "New")
    assert out["CrossRepoCalls"] == [{
        "edge_id": pipeline._edge_id("CrossRepoCalls", sid, did), "caller_method": sid,
        "callee_method": did, "source_repo": "web", "target_repo": "shared",
        "symbol": f"{SHARED}.New", "site_kind": "call"}]
    assert out["stats"]["call_edges"] == 1 and out["drops"] == []


def test_a_peer_import_becomes_a_cross_repo_import():
    out = _derive({"orders/orders.go": {"imports": [_import(3)]}})
    sid = pipeline._make_id("file", "web", "orders/orders.go", "")
    did = pipeline._make_id("package", "shared", "log", "log")
    assert out["CrossRepoImports"] == [{
        "edge_id": pipeline._edge_id("CrossRepoImports", sid, did), "source_file": sid,
        "target_package": did, "source_repo": "web", "target_repo": "shared",
        "import_path": SHARED + "/log"}]


def test_an_import_target_without_a_clause_takes_the_directorys_package():
    # resolution sees no peer files, so peer import targets may lack "clause"
    imp = _import(3)
    del imp["target"]["clause"]
    out = _derive({"orders/orders.go": {"imports": [imp]}})
    assert [r["target_package"] for r in out["CrossRepoImports"]] == [
        pipeline._make_id("package", "shared", "log", "log")]


def test_the_clause_of_a_directory_prefers_build_included_non_test_files():
    ents = _entities()
    shared = ents["shared"]
    shared["log/log_test.go"] = {"package_name": "log_test", "origin": "xtest", "build_included": True,
                                 "types": [], "funcs": []}
    shared["log/gen.go"] = {"package_name": "main", "origin": "command", "build_included": False,
                            "types": [], "funcs": []}
    imp = _import(3)
    del imp["target"]["clause"]
    repos = [_repo("web", {"orders/orders.go": {"imports": [imp]}}, entities=ents),
             _repo("shared", {}, modules=(SHARED,), entities=ents)]
    out = crossref.derive_cross_repo(repos, printer=lambda *a: None)
    assert [r["target_package"] for r in out["CrossRepoImports"]] == [
        pipeline._make_id("package", "shared", "log", "log")]
    shared["log/gen.go"]["build_included"] = True        # two compiled clauses: ambiguous
    out = crossref.derive_cross_repo(repos, printer=lambda *a: None)
    assert _drops(out) == [("node missing", "orders/orders.go:3:2", "shared:log|?")]


def test_a_directory_with_no_compiled_file_has_no_package_to_import():
    # R50 minor 15: its one clause is not enough; no file of it is in the build
    ents = _entities()
    ents["shared"]["tools/gen.go"] = {"sha256": "x", "package_name": "main", "origin": "command",
                                      "build_included": False, "types": [], "funcs": []}
    imp = _import(3, directory="tools", path=SHARED + "/tools")
    del imp["target"]["clause"]
    repos = [_repo("web", {"orders/orders.go": {"imports": [imp]}}, entities=ents),
             _repo("shared", {}, modules=(SHARED,), entities=ents)]
    out = crossref.derive_cross_repo(repos, printer=lambda *a: None)
    assert out["CrossRepoImports"] == []
    assert _drops(out) == [("node missing", "orders/orders.go:3:2", "shared:tools|?")]


@pytest.mark.parametrize("field, kind, tgt", [
    ("embedded", "embeds", ("contracts/audit.go", "Audit")),
    ("iface", "implements", ("log/logger.go", "Sink")),
])
def test_peer_embeds_and_implements_become_cross_repo_refs(field, kind, tgt):
    key = "embeds" if field == "embedded" else "implements"
    out = _derive({"orders/orders.go": {key: [_relation(field, _peer(*tgt, kind="class"))]}})
    sid = pipeline._make_id("class", "web", "orders/orders.go", "Order")
    did = pipeline._make_id("class", "shared", tgt[0], tgt[1])
    assert out["CrossRepoRef"] == [{
        "edge_id": pipeline._edge_id(f"CrossRepoRef:{kind}", sid, did), "source_class": sid,
        "target_class": did, "source_repo": "web", "target_repo": "shared",
        "symbol": f"{SHARED}.{tgt[1]}", "kind": kind}]


def _drops(out: dict) -> list[tuple[str, str, str]]:
    return [(d["reason"], d["source"], d["target"]) for d in out["drops"]]


def test_drops_name_their_reason():
    out = _derive({"orders/orders.go": {
        "calls": [_call(20, _peer("log/logger.go", "Gone")),                      # node missing
                  _call(21, _peer("log/logger.go", "New"), caller="Vanished"),    # caller missing
                  _call(22, _peer("x/y.go", "F", repo="billing")),                # repo not ingested
                  _call(23, None)],                                               # peer without owner
        "imports": [_import(4, directory="nowhere", clause="nowhere")],
        "embeds": [_relation("embedded", _peer("contracts/audit.go", "Audit", kind="class"),
                             type_qualname="Ghost")]}})
    assert sorted(_drops(out)) == sorted([
        ("node missing", "orders/orders.go:20:7", "shared:log/logger.go:Gone"),
        ("node missing", "orders/orders.go:21:7", "shared:log/logger.go:New"),
        ("repo not ingested", "orders/orders.go:22:7", "billing:x/y.go:F"),
        ("no owner", "orders/orders.go:23:7", SHARED),
        ("node missing", "orders/orders.go:4:2", "shared:nowhere|nowhere"),
        ("node missing", "orders/orders.go:Ghost", "shared:contracts/audit.go:Audit"),
    ])
    assert out["stats"]["drops_by_reason"] == {"no owner": 1, "two owners": 0, "node missing": 4,
                                               "repo not ingested": 1, "stale": 0}
    assert out["CrossRepoCalls"] == out["CrossRepoImports"] == out["CrossRepoRef"] == []


def test_records_of_a_file_that_changed_during_the_load_are_dropped_as_stale():
    # R46: positions in a file the helper saw change are untrusted (B3 refuses them too)
    web = _repo("web", {"orders/orders.go": {
        "calls": [_call(12, _peer("log/logger.go", "New")),
                  _call(13, None, peer=False, version="v1.0.0")],     # else a module-cache listing
        "imports": [_import(3)],
        "embeds": [_relation("embedded", _peer("contracts/audit.go", "Audit", kind="class"))]}},
        modules=("example.com/iryo/web",))
    web["resolutions"]["files"]["orders/orders.go"]["sha256"] = "changed-during-load"
    out = crossref.derive_cross_repo([web, _repo("shared", {}, modules=(SHARED,))], printer=lambda *a: None)
    assert sorted(_drops(out)) == sorted([
        ("stale", "orders/orders.go:12:7", "shared:log/logger.go:New"),
        ("stale", "orders/orders.go:3:2", "shared:log|log"),
        ("stale", "orders/orders.go:Order", "shared:contracts/audit.go:Audit")])
    assert out["CrossRepoCalls"] == out["CrossRepoImports"] == out["CrossRepoRef"] == []
    assert out["module_cache"] == [] and out["stats"]["drops_by_reason"]["stale"] == 3


def test_no_ref_edge_starts_at_a_type_declared_in_a_changed_file():
    ents = _entities()
    ents["web"]["orders/extra.go"] = {"sha256": "x", "package_name": "orders", "origin": "package",
                                      "types": [], "funcs": []}
    web = _repo("web", {"orders/extra.go": {"embeds": [   # the record names Order of orders/orders.go
        _relation("embedded", _peer("contracts/audit.go", "Audit", kind="class"))]}, "orders/orders.go": {}},
        modules=("example.com/iryo/web",), entities=ents)
    web["resolutions"]["files"]["orders/orders.go"]["sha256"] = "changed-during-load"
    out = crossref.derive_cross_repo([web, _repo("shared", {}, modules=(SHARED,), entities=ents)],
                                     printer=lambda *a: None)
    assert _drops(out) == [("stale", "orders/extra.go:Order", "shared:contracts/audit.go:Audit")]


def test_edges_that_end_in_a_changed_file_of_a_peer_are_dropped_as_stale():
    shared = _repo("shared", {"log/logger.go": {}}, modules=(SHARED,))
    shared["resolutions"]["files"]["log/logger.go"]["sha256"] = "9f86d081884c7d65"   # observed, not scanned
    web = _repo("web", {"orders/orders.go": {
        "calls": [_call(12, _peer("log/logger.go", "New"))],
        "imports": [_import(3)],
        "embeds": [_relation("embedded", _peer("contracts/audit.go", "Audit", kind="class"))],
        "implements": [_relation("iface", _peer("log/logger.go", "Sink", kind="class"))]}},
        modules=("example.com/iryo/web",))
    out = crossref.derive_cross_repo([web, shared], printer=lambda *a: None)
    assert sorted(_drops(out)) == sorted([
        ("stale", "orders/orders.go:12:7", "shared:log/logger.go:New"),
        ("stale", "orders/orders.go:Order", "shared:log/logger.go:Sink")])
    assert [r["kind"] for r in out["CrossRepoRef"]] == ["embeds"]          # audit.go is fresh
    assert len(out["CrossRepoImports"]) == 1                               # a package is not a file


def test_module_cache_targets_of_an_ingested_module_are_listed_not_matched():
    out = _derive({"orders/orders.go": {
        "calls": [_call(30, None, peer=False, version="v1.0.0"),
                  _call(31, None, module="example.com/thirdparty/textfmt", peer=False, version="v1.0.0"),
                  _call(32, None, module="std", peer=False)],
        "imports": [_import(5, repo=None, version="v1.0.0")]}})
    assert out["CrossRepoCalls"] == [] and out["drops"] == []
    assert [(m["source"], m["module"], m["version"], m["owner"]) for m in out["module_cache"]] == [
        ("orders/orders.go:30:7", SHARED, "v1.0.0", "shared"),
        ("orders/orders.go:5:2", SHARED, "v1.0.0", "shared")]
    assert all("--peer shared=/repos/shared" in m["hint"] for m in out["module_cache"])


def test_a_published_copy_of_the_repos_own_module_is_not_listed():
    # spec §8.1: a single-module group without a local replace reaches a sibling
    # in-repo module's published version; that is not a peer question.
    out = _derive({"orders/orders.go": {"calls": [
        _call(35, None, module="example.com/iryo/web", peer=False, version="v1.2.0")]}})
    assert out["module_cache"] == [] and out["drops"] == []


def test_a_module_provided_by_two_ingested_repos_has_two_owners():
    fork = _repo("fork", {}, modules=(SHARED,))
    out = _derive({"orders/orders.go": {"calls": [_call(40, None, peer=False, version="v1.0.0")]}},
                  extra=(fork,))
    assert _drops(out) == [("two owners", "orders/orders.go:40:7", f"{SHARED}@v1.0.0")]
    assert out["module_cache"] == []


def test_only_external_records_are_considered():
    resolved = record(50, 7, "resolved", caller="Place", target=target("orders/orders.go", "Place"))
    ambiguous = record(51, 7, "ambiguous", caller="Place", candidates=[_peer("log/logger.go", "New")])
    out = _derive({"orders/orders.go": {"calls": [resolved, ambiguous]}})
    assert out["CrossRepoCalls"] == [] and out["drops"] == [] and out["module_cache"] == []


def test_derivation_is_deterministic_and_deduplicated():
    calls = [_call(14, _peer("log/logger.go", "New")), _call(12, _peer("log/logger.go", "Logger.Info")),
             _call(13, _peer("log/logger.go", "New"))]
    a = _derive({"orders/orders.go": {"calls": calls}})
    b = _derive({"orders/orders.go": {"calls": list(reversed(calls))}})
    assert a == b
    assert [r["symbol"] for r in a["CrossRepoCalls"]] == [f"{SHARED}.Logger.Info", f"{SHARED}.New"]


# ═══════════════════════════════════════════════════════ ingested repos on disk


_TARGET = object()   # the current Spanner target (pipeline._spanner_target(False))


def _checkpoint(d: Path, *, complete: bool = True) -> None:
    """graph_checkpoint.json of a graph built from the repo's current resolutions.json."""
    res = json.loads((d / "resolutions.json").read_text(encoding="utf-8"))
    (d / "graph_checkpoint.json").write_text(json.dumps({
        "id_scheme": pipeline.ID_SCHEME, "complete": complete, "phase": "phase9",
        "resolutions_hash": pipeline.resolutions_hash(res), "spanner": pipeline._spanner_target(False)}))


def _write_repo(root: Path, name: str, *, id_scheme=pipeline.ID_SCHEME, prefix="a", versions=1,
                calls=(), sha="x", spanner=_TARGET, checkpoint: bool | None = True) -> None:
    """A repo whose graph is finished and was written to the current target,
    unless `spanner` (None: --no-spanner) or `checkpoint` (False: unfinished,
    None: none) says otherwise."""
    d = root / "repos" / name
    d.mkdir(parents=True)
    (d / "graph_meta.json").write_text(json.dumps({
        "repo": name, "target_dir": f"/src/{name}", "id_scheme": id_scheme, "id_prefix": prefix,
        "spanner": pipeline._spanner_target(False) if spanner is _TARGET else spanner}))
    (d / "entities.json").write_text(json.dumps({"version": versions,
                                                 "entities": _entities().get(name, {})}))
    (d / "resolutions.json").write_text(json.dumps({"version": versions, "repo": name, "files": {
        "orders/orders.go": {"sha256": sha, "calls": list(calls), "imports": [], "embeds": [],
                             "implements": []}} if calls else {}}))
    (d / "units.json").write_text(json.dumps({"version": versions, "units": [
        {"key": f"example.com/iryo/{name}"}]}))
    if checkpoint is not None:
        _checkpoint(d, complete=checkpoint)


def test_discover_repos_skips_graphs_that_are_unfinished_or_elsewhere(tmp_path, monkeypatch):
    # final review 1: crossref must not edge onto old node rows or into another target
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    other = {**pipeline._spanner_target(False), "database": "other-db"}
    _write_repo(tmp_path, "good")
    _write_repo(tmp_path, "nockpt", checkpoint=None)
    _write_repo(tmp_path, "partial", checkpoint=False)
    _write_repo(tmp_path, "nospanner", spanner=None)
    _write_repo(tmp_path, "elsewhere", spanner=other)
    _write_repo(tmp_path, "stale")
    (tmp_path / "repos" / "stale" / "resolutions.json").write_text(json.dumps(
        {"version": 1, "repo": "stale", "files": {"a.go": {"sha256": "y", "calls": [], "imports": [],
                                                           "embeds": [], "implements": []}}}))
    lines: list[str] = []
    assert [r["repo"] for r in crossref.discover_repos(str(tmp_path), lines.append)] == ["good"]
    assert lines == [
        "  [crossref] skipping elsewhere: its graph was written to codedoc-instance/other-db "
        "(graph code_graph_a, ID prefix a), not to codedoc-instance/codedoc-go (graph code_graph_a, ID prefix a)",
        "  [crossref] skipping nockpt: it has no graph_checkpoint.json (its graph never finished)",
        "  [crossref] skipping nospanner: its graph was written to off (--no-spanner), not to "
        "codedoc-instance/codedoc-go (graph code_graph_a, ID prefix a)",
        "  [crossref] skipping partial: its graph is unfinished (graph_checkpoint.json is not complete)",
        "  [crossref] skipping stale: resolutions.json is not the one its graph was written from",
    ]
    lines.clear()
    kept = crossref.discover_repos(str(tmp_path), lines.append, strict=False)     # --dry-run
    assert [r["repo"] for r in kept] == ["elsewhere", "good", "nockpt", "nospanner", "partial", "stale"]
    assert len(lines) == 5 and all(line.startswith("  [crossref] warning: ") for line in lines)
    assert all(line.endswith("; a write skips it") for line in lines)


def test_run_crossref_writes_only_finished_graphs_of_its_target(tmp_path, monkeypatch, fake_spanner):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    _write_repo(tmp_path, "shared")
    _write_repo(tmp_path, "web", calls=[_call(12, _peer("log/logger.go", "New"))], checkpoint=False)
    lines: list[str] = []
    out = crossref.run_crossref(printer=lines.append, write=True)
    assert out["CrossRepoCalls"] == [] and fake_spanner.mutations == {}
    assert "skipping web: its graph is unfinished" in "\n".join(lines)
    lines.clear()
    out = crossref.run_crossref(printer=lines.append, write=False)
    assert len(out["CrossRepoCalls"]) == 1                    # previewed, with the warning
    assert "warning: web: its graph is unfinished" in "\n".join(lines)


def test_discover_repos_checks_versions_and_the_id_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    _write_repo(tmp_path, "shared")
    _write_repo(tmp_path, "web")
    _write_repo(tmp_path, "old", id_scheme=0)
    _write_repo(tmp_path, "other", prefix="b")
    _write_repo(tmp_path, "future", versions=2)
    (tmp_path / "repos" / "broken").mkdir()
    (tmp_path / "repos" / "broken" / "graph_meta.json").write_text("{")
    lines: list[str] = []
    repos = crossref.discover_repos(str(tmp_path), lines.append)
    assert [r["repo"] for r in repos] == ["shared", "web"]
    assert repos[0]["root"] == "/src/shared" and repos[0]["modules"] == ["example.com/iryo/shared"]
    assert "log/logger.go" in repos[0]["entities"]
    text = "\n".join(lines)
    for name in ("old", "other", "future", "broken"):
        assert f"skipping {name}" in text


def test_run_crossref_writes_the_report_and_honours_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    _write_repo(tmp_path, "shared")
    _write_repo(tmp_path, "web", calls=[_call(12, _peer("log/logger.go", "New")),
                                        _call(20, _peer("log/logger.go", "Gone"))])
    writes = []
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: "db")
    monkeypatch.setattr(pipeline, "_batch_insert",
                        lambda db, table, columns, rows: writes.append((table, columns, rows)))
    lines: list[str] = []
    out = crossref.run_crossref(printer=lines.append, write=False)
    assert len(out["CrossRepoCalls"]) == 1 and writes == []
    report = json.loads((tmp_path / "crossref_report.json").read_text(encoding="utf-8"))
    assert report["stats"]["call_edges"] == 1
    assert [d["reason"] for d in report["drops"]] == ["node missing"]
    assert "node missing: 1 site(s)" in "\n".join(lines)
    crossref.run_crossref(printer=lines.append, write=True)
    assert [w[0] for w in writes] == ["CrossRepoCalls"]
    table, columns, rows = writes[0]
    assert columns[:3] == ["edge_id", "caller_method", "callee_method"]
    assert rows[0][0] == out["CrossRepoCalls"][0]["edge_id"]


def test_the_report_lists_stale_drops(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    _write_repo(tmp_path, "shared")
    _write_repo(tmp_path, "web", calls=[_call(12, _peer("log/logger.go", "New"))],
                sha="changed-during-load")
    lines: list[str] = []
    out = crossref.run_crossref(printer=lines.append, write=False)
    assert out["CrossRepoCalls"] == []
    report = json.loads((tmp_path / "crossref_report.json").read_text(encoding="utf-8"))
    assert [(d["reason"], d["source"]) for d in report["drops"]] == [("stale", "orders/orders.go:12:7")]
    assert report["stats"]["drops_by_reason"]["stale"] == 1
    assert "stale: 1 site(s)" in "\n".join(lines)


# ═══════════════════════════════════════════════════════ pruning its own rows (R48)


NEW = _call(12, _peer("log/logger.go", "New"))
INFO = _call(13, _peer("log/logger.go", "Logger.Info"))


def _call_key(qualname: str) -> str:
    sid = pipeline._make_id("method", "web", "orders/orders.go", "Place")
    return pipeline._edge_id("CrossRepoCalls", sid, pipeline._make_id("method", "shared", "log/logger.go",
                                                                       qualname))


def _rewrite_calls(root: Path, calls: list) -> None:
    """A finished re-ingest of web with these calls."""
    path = root / "repos" / "web" / "resolutions.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["files"] = {"orders/orders.go": {"sha256": "x", "calls": list(calls), "imports": [], "embeds": [],
                                         "implements": []}} if calls else {}
    path.write_text(json.dumps(doc), encoding="utf-8")
    _checkpoint(path.parent)


@pytest.fixture
def ingested(tmp_path, monkeypatch, fake_spanner):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    _write_repo(tmp_path, "shared")
    _write_repo(tmp_path, "web", calls=[NEW, INFO])
    return tmp_path


def _record(root: Path) -> dict:
    return json.loads((root / "crossref_rows.json").read_text(encoding="utf-8"))


def test_a_write_records_its_keys_with_the_spanner_target(ingested, fake_spanner):
    crossref.run_crossref(printer=lambda *a: None, write=True)
    rec = _record(ingested)
    assert rec["target"] == {"instance": config.SPANNER_INSTANCE, "database": config.SPANNER_DATABASE,
                             "graph": config.GRAPH_NAME, "id_prefix": "a"}
    assert rec["tables"] == {"CrossRepoCalls": sorted([_call_key("New"), _call_key("Logger.Info")]),
                             "CrossRepoImports": [], "CrossRepoRef": []}
    assert fake_spanner.deletes == {}


def test_the_next_write_to_the_same_target_deletes_what_it_no_longer_derives(ingested, fake_spanner):
    crossref.run_crossref(printer=lambda *a: None, write=True)
    _rewrite_calls(ingested, [NEW])
    lines: list[str] = []
    crossref.run_crossref(printer=lines.append, write=True)
    assert fake_spanner.deletes == {"CrossRepoCalls": [[_call_key("Logger.Info")]]}
    assert _record(ingested)["tables"]["CrossRepoCalls"] == [_call_key("New")]
    assert "deleted 1 CrossRepoCalls edges it no longer derives" in "\n".join(lines)
    _rewrite_calls(ingested, [])                          # every peer call is gone
    crossref.run_crossref(printer=lambda *a: None, write=True)
    assert fake_spanner.deletes["CrossRepoCalls"][-1] == [_call_key("New")]
    assert _record(ingested)["tables"]["CrossRepoCalls"] == []


def test_another_target_is_not_pruned_and_the_record_is_replaced(ingested, fake_spanner, monkeypatch):
    crossref.run_crossref(printer=lambda *a: None, write=True)
    monkeypatch.setattr(config, "ID_PREFIX", "b")       # side-by-side graph: other keys, same tables
    _rewrite_calls(ingested, [NEW])
    for name in ("shared", "web"):                         # the repos re-ingested under prefix b
        meta = ingested / "repos" / name / "graph_meta.json"
        meta.write_text(json.dumps({**json.loads(meta.read_text()), "id_prefix": "b",
                                    "spanner": pipeline._spanner_target(False)}), encoding="utf-8")
        _checkpoint(meta.parent)
    crossref.run_crossref(printer=lambda *a: None, write=True)
    assert fake_spanner.deletes == {}
    rec = _record(ingested)
    assert rec["target"]["id_prefix"] == "b" and len(rec["tables"]["CrossRepoCalls"]) == 1


def test_dry_run_reads_and_writes_no_record(ingested, fake_spanner):
    crossref.run_crossref(printer=lambda *a: None, write=False)
    assert not (ingested / "crossref_rows.json").exists()
    crossref.run_crossref(printer=lambda *a: None, write=True)
    before = (ingested / "crossref_rows.json").read_bytes()
    _rewrite_calls(ingested, [NEW])
    writes = dict(fake_spanner.mutations)
    crossref.run_crossref(printer=lambda *a: None, write=False)
    assert (ingested / "crossref_rows.json").read_bytes() == before
    assert fake_spanner.deletes == {} and fake_spanner.mutations == writes


def test_an_unreadable_record_prunes_nothing_and_is_replaced(ingested, fake_spanner):
    (ingested / "crossref_rows.json").write_text("{", encoding="utf-8")
    lines: list[str] = []
    crossref.run_crossref(printer=lines.append, write=True)
    assert fake_spanner.deletes == {}
    assert len(_record(ingested)["tables"]["CrossRepoCalls"]) == 2
    assert "crossref_rows.json" in "\n".join(lines)


def test_an_interrupted_write_leaves_every_key_it_may_have_written(ingested, fake_spanner, monkeypatch):
    crossref.run_crossref(printer=lambda *a: None, write=True)
    _rewrite_calls(ingested, [NEW])

    def boom(*_a, **_k):
        raise RuntimeError("Spanner down")
    monkeypatch.setattr(pipeline, "_batch_insert", boom)
    with pytest.raises(RuntimeError):
        crossref.run_crossref(printer=lambda *a: None, write=True)
    # the pending record keeps the stale key, so the next run still prunes it
    assert _record(ingested)["tables"]["CrossRepoCalls"] == sorted([_call_key("New"), _call_key("Logger.Info")])


def test_deletes_go_through_the_shared_batched_delete(ingested, fake_spanner, monkeypatch):
    # batching, the mutation limit and the retries are pipeline._batch_delete's (test_graph_prune)
    crossref.run_crossref(printer=lambda *a: None, write=True)
    _rewrite_calls(ingested, [NEW])
    calls = []
    monkeypatch.setattr(pipeline, "_batch_delete", lambda db, table, keys: calls.append((db, table, keys)))
    crossref.run_crossref(printer=lambda *a: None, write=True)
    assert calls == [(fake_spanner, "CrossRepoCalls", [_call_key("Logger.Info")])]
    assert not hasattr(crossref, "_batch_delete")


def test_run_crossref_with_no_repos(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    lines: list[str] = []
    out = crossref.run_crossref(printer=lines.append, write=True)
    assert out["CrossRepoCalls"] == [] and "no ingested repos" in "\n".join(lines)


def test_format_drops():
    assert crossref.format_drops([]) == "  (nothing dropped)"
    drops = [{"reason": "node missing", "repo": "web", "source": f"a.go:{i}:1", "target": "t"}
             for i in range(3)]
    text = crossref.format_drops(drops, limit=2)
    assert "node missing: 3 site(s)" in text and "... and 1 more" in text
