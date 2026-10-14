"""evaluate: the fixture runner, its mode settings, gates, report and printing.

The real ingest runs the pipeline in-process (needs the Go toolchain, WP-B2
and WP-B3); here an injected ingest returns runs built from the manifests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graph_generator import config, evaluate, go_toolchain, pipeline
from tests.eval_worlds import ROOT, import_record, perfect_runs, record, target


def _ingest_from(runs: dict, seen: list | None = None):
    def ingest(fixture_dir, dirs, eval_dir, env_mode, printer):
        if seen is not None:
            seen.append({"fixture_dir": fixture_dir, "dirs": dirs, "eval_dir": eval_dir,
                         "env_mode": env_mode})
        return runs
    return ingest


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path / "out"


ORACLE_OK = {"problems": 0, "problem_examples": [], "edges": 0, "verdicts": {}, "wrong": 0,
             "failed_entries": [], "error": None, "pass": True}


@pytest.fixture(autouse=True)
def no_goracle(monkeypatch):
    """The runtime oracle needs a Go toolchain (test_evaluate_oracle covers it);
    here it passes, and records how it was asked."""
    calls = []

    def fake(fixture_dir, runs, eval_dir, *, judge, printer=print):
        calls.append({"fixture": fixture_dir.rsplit("/", 1)[-1], "judge": judge, "repos": sorted(runs)})
        return dict(ORACLE_OK)
    monkeypatch.setattr(evaluate, "oracle_check", fake)
    return calls


def _quiet(*_a, **_k):
    pass


def test_perfect_f1_run_passes_every_gate_but_the_graph_metrics(out_dir):
    m, runs = perfect_runs("iryo-kanri-system")
    n = len(m["cases"])                                   # the corpus grows; never pin the count
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["mode"] == "env" and report["cases"] == n > 100
    assert report["verdicts"]["wrong"] == 0 and report["verdicts"]["correct"] == n
    assert report["completeness"] == {"value": n, "required": evaluate.completeness_min(n), "pass": True}
    # Perfect records but no node rows: coverage and QA fail, so the fixture fails.
    assert report["node_coverage"]["pass"] is False and report["qa"]["pass"] is False
    assert report["edge_rate"]["pass"] is True
    assert report["pass"] is False
    assert report["discovery"] is None and report["cross_repo"] is None
    assert report["imports"]["gated"] is True and report["imports"]["records"] == 0
    assert report["rows"] == {"PackageImports": 0, "ClassEmbeds": 0, "ClassImplements": 0}
    assert report["seconds"] >= 0


def _plant_unmapped_import(runs: dict, repo: str) -> None:
    """A resolved in-repo import record of an existing file, with no Packages node or row."""
    rel = next(iter(runs[repo].resolutions["files"]))
    runs[repo].resolutions["files"][rel]["imports"].append(
        import_record(3, 2, "example.com/iryo/nowhere", directory="nowhere", clause="nowhere"))


def _plant_mapped_import(runs: dict, repo: str) -> None:
    """A resolved in-repo import record with its Packages node and PackageImports row."""
    r = runs[repo]
    rel = next(iter(r.resolutions["files"]))
    r.resolutions["files"][rel]["imports"].append(
        import_record(4, 2, "example.com/iryo/lib", directory="lib", clause="lib"))
    package = pipeline._make_id("package", repo, "lib", "lib")
    r.nodes.setdefault("Packages", []).append({"package_id": package})
    r.edges.setdefault("PackageImports", []).append(
        {"source_file": pipeline._make_id("file", repo, rel, ""), "target_package": package})


def test_an_unmapped_import_fails_f3(out_dir, monkeypatch):
    monkeypatch.setattr(evaluate, "cross_repo_check",
                        lambda *_a: {"expected": 0, "actual": 0, "missing": [], "extra": [], "pass": True})
    _m, runs = perfect_runs("multi_repo")
    clean = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=True, printer=_quiet,
                                      ingest=_ingest_from(runs))
    assert clean["imports"]["pass"] is False                  # no import record at all fails (R50)
    _plant_mapped_import(runs, "web")
    clean = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=True, printer=_quiet,
                                      ingest=_ingest_from(runs))
    assert clean["pass"] is True and clean["imports"]["pass"] is True and clean["loads"]["pass"] is True
    _plant_unmapped_import(runs, "web")
    report = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["imports"]["gated"] is True and report["imports"]["unmapped"] == 1
    assert report["imports"]["unmapped_examples"][0].startswith("web/")
    assert report["verdicts"]["wrong"] == 0 and report["pass"] is False


def test_a_group_that_did_not_load_fails_f1_and_f3_but_not_f2(out_dir, monkeypatch):
    monkeypatch.setattr(evaluate, "cross_repo_check",
                        lambda *_a: {"expected": 0, "actual": 0, "missing": [], "extra": [], "pass": True})
    monkeypatch.setattr(evaluate, "_discovery_report",
                        lambda *_a: {"pass": True, "layouts": 23, "problems": {}})
    for fixture, gated in (("multi_repo", True), ("go_layouts", False)):
        _m, runs = perfect_runs(fixture)
        name = sorted(runs)[0]
        runs[name].resolutions["groups"]["m1"] = {"status": "error", "reason": "planted"}
        report = evaluate.evaluate_fixture(fixture, str(ROOT), env_mode=True, printer=_quiet,
                                           ingest=_ingest_from(runs))
        assert report["loads"]["not_ok"] == [f"{name}:m1 error (planted)"]
        assert report["loads"]["pass"] is (not gated)
        runs[name].resolutions["stats"]["join_misses"] = 1
        report = evaluate.evaluate_fixture(fixture, str(ROOT), env_mode=True, printer=_quiet,
                                           ingest=_ingest_from(runs))
        assert report["loads"]["pass"] is False and report["pass"] is False


def test_the_oracle_judges_f1_and_f3_and_checks_every_fixture(out_dir, no_goracle):
    for fixture in evaluate.FIXTURES:
        _m, runs = perfect_runs(fixture)
        report = evaluate.evaluate_fixture(fixture, str(ROOT), env_mode=True, printer=_quiet,
                                           ingest=_ingest_from(runs))
        assert report["oracle"]["pass"] is True
    assert [(c["fixture"], c["judge"]) for c in no_goracle] == [
        ("iryo-kanri-system", True), ("go_layouts", False), ("multi_repo", True)]


@pytest.mark.parametrize("bad", [{"wrong": 1, "verdicts": {"WRONG-OTHER": 1}}, {"problems": 2},
                                 {"failed_entries": ["web-demo"]}, {"error": "goracle match failed"}])
def test_any_oracle_finding_fails_the_fixture(out_dir, monkeypatch, bad):
    monkeypatch.setattr(evaluate, "cross_repo_check",
                        lambda *_a: {"expected": 0, "actual": 0, "missing": [], "extra": [], "pass": True})
    _m, runs = perfect_runs("multi_repo")
    _plant_mapped_import(runs, "web")
    finding = {**ORACLE_OK, **bad, "pass": False}
    report = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs), oracle=lambda *a, **k: finding)
    assert report["oracle"] is finding and report["pass"] is False
    lines: list[str] = []
    evaluate.print_report(report, lines.append)
    assert "[FAIL] Oracle" in "\n".join(lines)


def test_a_manifest_with_class_edges_gates_them(out_dir, tmp_path):
    root = tmp_path / "root"
    gt = root / "test_codes" / "iryo-kanri-system" / "ground_truth"
    gt.mkdir(parents=True)
    m, runs = perfect_runs("iryo-kanri-system")
    m["class_edges"] = [{"table": "ClassEmbeds", "source": "iryo-kanri-system:a.go:A",
                         "target": "iryo-kanri-system:a.go:B", "pointer": False}]
    (gt / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(root), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["class_edges"]["missing"] == ["ClassEmbeds iryo-kanri-system:a.go:A -> "
                                                "iryo-kanri-system:a.go:B (pointer=false)"]
    assert report["class_edges"]["pass"] is False
    runs["iryo-kanri-system"].edges["ClassEmbeds"] = [
        {"child_class": pipeline._make_id("class", "iryo-kanri-system", "a.go", "A"),
         "embedded_class": pipeline._make_id("class", "iryo-kanri-system", "a.go", "B"), "pointer": False}]
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(root), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["class_edges"]["pass"] is True
    del m["class_edges"]                                       # required on F1: losing the key fails
    (gt / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(root), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["class_edges"]["pass"] is False and report["pass"] is False
    assert report["class_edges"]["missing"] == ["manifest.json has no class_edges"]
    m["class_edges"] = []                                      # required and pinning nothing fails too
    (gt / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    runs["iryo-kanri-system"].edges["ClassEmbeds"] = []
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(root), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["class_edges"]["pass"] is False and report["pass"] is False
    assert report["class_edges"]["missing"] == ["class_edges pins no edge for env"]
    _m, runs = perfect_runs("multi_repo")
    report = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["class_edges"] is None                       # no class_edges key: no gate


def test_unmapped_imports_are_only_reported_on_f2(out_dir, monkeypatch):
    monkeypatch.setattr(evaluate, "_discovery_report",
                        lambda *_a: {"pass": True, "layouts": 23, "problems": {}})
    _m, runs = perfect_runs("go_layouts")
    _plant_unmapped_import(runs, next(name for name in sorted(runs) if runs[name].resolutions["files"]))
    report = evaluate.evaluate_fixture("go_layouts", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["imports"]["gated"] is False and report["imports"]["unmapped"] == 1
    assert report["pass"] is True


def test_the_report_counts_import_embed_and_implements_rows(out_dir):
    _m, runs = perfect_runs("multi_repo", offline=True)
    runs["web"].edges = {"PackageImports": [{}, {}], "ClassEmbeds": [{}], "MethodCalls": [{}]}
    runs["api"].edges = {"PackageImports": [{}], "ClassImplements": [{}, {}, {}]}
    report = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=False, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["rows"] == {"PackageImports": 3, "ClassEmbeds": 1, "ClassImplements": 3}


def test_eval_dir_is_recreated_per_fixture_and_mode(out_dir):
    seen: list = []
    stale = out_dir / "eval_go_layouts" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    _m, runs = perfect_runs("go_layouts", offline=True)
    evaluate.evaluate_fixture("go_layouts", str(ROOT), env_mode=False, printer=_quiet,
                              ingest=_ingest_from(runs, seen))
    assert not stale.exists()
    assert seen[0]["eval_dir"] == str(out_dir.resolve() / "eval_go_layouts")
    assert seen[0]["env_mode"] is False
    assert seen[0]["fixture_dir"] == str(ROOT / "test_codes" / "go_layouts")
    assert "_outside" not in seen[0]["dirs"] and len(seen[0]["dirs"]) == 23
    seen.clear()
    _m, runs = perfect_runs("go_layouts")
    evaluate.evaluate_fixture("go_layouts", str(ROOT), env_mode=True, printer=_quiet,
                              ingest=_ingest_from(runs, seen))
    assert seen[0]["eval_dir"] == str(out_dir.resolve() / "eval_go_layouts_env")


def test_f2_gates_include_the_discovery_facts(out_dir):
    _m, runs = perfect_runs("go_layouts")
    report = evaluate.evaluate_fixture("go_layouts", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["verdicts"]["wrong"] == 0 and report["completeness"]["pass"]
    assert report["discovery"]["pass"] is False          # the synthetic runs have no Discovery
    assert report["discovery"]["problems"]["ws"] == ["no discovery (the pipeline did not run Phase 1)"]
    assert report["pass"] is False
    assert report["node_coverage"] is None and report["qa"] is None


def test_f3_cross_repo_set_comes_from_crossref_of_the_runs(out_dir, monkeypatch):
    _m, runs = perfect_runs("multi_repo")
    seen = []

    def fake_derive(repos, printer=print):
        seen.append(sorted(r["repo"] for r in repos))
        return {"CrossRepoCalls": [], "CrossRepoImports": [], "CrossRepoRef": [], "drops": [],
                "module_cache": [], "stats": {}}

    from graph_generator import crossref
    monkeypatch.setattr(crossref, "derive_cross_repo", fake_derive)
    report = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert seen == [["api", "shared", "web"]]
    assert report["cross_repo"]["expected"] == 28 and report["cross_repo"]["actual"] == 0
    assert report["cross_repo"]["pass"] is False and report["pass"] is False
    assert report["verdicts"]["wrong"] == 0


def test_a_wrong_answer_fails_the_fixture_and_is_listed(out_dir):
    m, runs = perfect_runs("multi_repo", offline=True)          # scored offline below
    case = next(c for c in m["cases"] if c["id"] == "NEG-002")
    repo, rel = "web", case["file"][len("web/"):]
    recs = runs[repo].resolutions["files"][rel]["calls"]
    rec = next(r for r in recs if (r["site"]["line"], r["site"]["col"]) == (case["line"], case["col"]))
    rec["status"], rec["target"] = "external", target("log/logger.go", "Logger.Info", repo="shared")
    rec["external"] = {"module": "example.com/iryo/shared", "version": "", "package": "", "qualname": "",
                       "peer": True}
    report = evaluate.evaluate_fixture("multi_repo", str(ROOT), env_mode=False, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["verdicts"]["wrong"] == 1 and report["pass"] is False
    miss = next(x for x in report["misses"] if x["id"] == "NEG-002")
    assert miss["verdict"] == "wrong"
    assert miss["got"]["records"][0]["target"]["file"] == "shared/log/logger.go"


def test_missing_manifest_is_an_error_report(out_dir, tmp_path):
    report = evaluate.evaluate_fixture("go_layouts", str(tmp_path), env_mode=False, printer=_quiet,
                                       ingest=_ingest_from({}))
    assert report["pass"] is False and "manifest.json not found" in report["error"]
    assert "generate_manifest.py" in report["hint"]


@pytest.mark.parametrize("exc, hint", [
    (go_toolchain.ToolchainError("no Go toolchain found"), "setup go"),
    (go_toolchain.HelperBuildError("building the helper failed: sites.go:465: undefined"), "gohelper"),
    (NotImplementedError("Phase 1.5 extraction (go_extract) is not implemented yet (WP-B2)"), "WP-B2"),
])
def test_ingest_failures_are_error_reports(out_dir, exc, hint):
    def ingest(*_a):
        raise exc
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(ROOT), env_mode=False, printer=_quiet,
                                       ingest=ingest)
    assert report["pass"] is False and str(exc) in report["error"]
    assert hint in report["error"] + report.get("hint", "")


def test_dump_edges_writes_the_rows(out_dir):
    _m, runs = perfect_runs("iryo-kanri-system")
    runs["iryo-kanri-system"].edges = {"FileDependsOn": [{"edge_id": "e1"}]}
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(ROOT), env_mode=True, printer=_quiet,
                                       ingest=_ingest_from(runs), dump_edges=True)
    dumped = json.loads(Path(report["edges_path"]).read_text(encoding="utf-8"))
    assert dumped == {"iryo-kanri-system": {"FileDependsOn": [{"edge_id": "e1"}]}}


# ═══════════════════════════════════════════════════════ mode settings


def test_offline_settings_hide_the_user_module_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GO_USER_MODCACHE", "/home/someone/go/pkg/mod")
    monkeypatch.setattr(config, "GO_BUILD_TAGS", "integration")
    monkeypatch.setattr(config, "PEER_REPOS", "x=/y")
    eval_dir = tmp_path / "eval_x"
    eval_dir.mkdir()
    with evaluate.pinned_settings(str(eval_dir), env_mode=False, repo_root=str(ROOT)):
        assert config.GO_MODULES_DOWNLOAD is False
        empty = Path(config.GO_USER_MODCACHE)
        assert empty.is_dir() and not any(empty.iterdir()) and empty.parent == eval_dir
        assert (config.GO_BUILD_GOOS, config.GO_BUILD_GOARCH, config.GO_BUILD_TAGS,
                config.GO_BUILD_CGO, config.GO_EXTRA_BUILDS) == ("linux", "amd64", "", False, "")
        assert config.PEER_REPOS is None
        from graph_generator import go_modcache
        assert go_modcache.user_modcache(None) is None
    assert config.GO_USER_MODCACHE == "/home/someone/go/pkg/mod"
    assert config.GO_BUILD_TAGS == "integration" and config.PEER_REPOS == "x=/y"


def test_env_settings_download_from_the_synthetic_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GO_PROXY", "https://proxy.golang.org")
    monkeypatch.setattr(config, "GO_SUMDB", "sum.golang.org", raising=False)
    eval_dir = tmp_path / "eval_x_env"
    eval_dir.mkdir()
    with evaluate.pinned_settings(str(eval_dir), env_mode=True, repo_root=str(ROOT)):
        assert config.GO_MODULES_DOWNLOAD is True
        proxy = (ROOT / "test_codes" / "_proxy").resolve().as_uri()
        assert config.GO_PROXY == proxy and proxy.startswith("file:///")
        assert go_toolchain.download_proxy() == proxy
        assert config.GO_SUMDB == "off"
    assert config.GO_PROXY == "https://proxy.golang.org"
    assert config.GO_SUMDB == "sum.golang.org"


@pytest.mark.parametrize("env_mode, proxy, sumdb", [
    (True, "proxy", "off"), (False, "off", "off")])
def test_the_go_environment_of_each_mode(tmp_path, env_mode, proxy, sumdb):
    """What the go command sees under evaluate's settings (env_for, WP-D)."""
    from graph_generator.go_model import BuildConfig, Group, Toolchain
    group = Group(id="m0", kind="module", dir=str(tmp_path), modules=["example.com/m"], gowork=None,
                  modfile=None, vendor=False, degraded=False, status="ok", pattern="./...")
    build = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)
    toolchain = Toolchain(go="/nonexistent/go", version="go1.27.2", goroot=str(tmp_path / "goroot"))
    with evaluate.pinned_settings(str(tmp_path), env_mode=env_mode, repo_root=str(ROOT)):
        from graph_generator import go_modcache
        env = go_toolchain.env_for(group, build, mode="download" if env_mode else "offline",
                                   output_dir=str(tmp_path), user_modcache=go_modcache.user_modcache(None),
                                   toolchain=toolchain)
    want = (ROOT / "test_codes" / "_proxy").resolve().as_uri() if proxy == "proxy" else "off"
    assert (env["GOPROXY"], env["GOSUMDB"]) == (want, sumdb)
    assert env["GOMODCACHE"].startswith(str(tmp_path.resolve()))
    assert (env["GOOS"], env["GOARCH"], env["CGO_ENABLED"]) == ("linux", "amd64", "0")


def test_offline_settings_leave_the_sumdb_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GO_SUMDB", "sum.example.com", raising=False)
    with evaluate.pinned_settings(str(tmp_path), env_mode=False, repo_root=str(ROOT)):
        assert config.GO_SUMDB == "sum.example.com"


def test_settings_are_restored_after_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GO_BUILD_GOOS", "darwin")
    with pytest.raises(RuntimeError):
        with evaluate.pinned_settings(str(tmp_path), env_mode=False, repo_root=str(ROOT)):
            raise RuntimeError("boom")
    assert config.GO_BUILD_GOOS == "darwin"


def test_helper_is_seeded_into_the_eval_dir(tmp_path):
    built = tmp_path / "base" / "go" / "bin" / "gohelper-go1.27.2-abc"
    built.parent.mkdir(parents=True)
    built.write_bytes(b"\x7fELF")
    built.chmod(0o755)
    eval_dir = tmp_path / "eval_x"
    evaluate.seed_helper(str(built), str(eval_dir))
    seeded = Path(go_toolchain.go_dir(str(eval_dir))) / "bin" / built.name
    assert seeded.read_bytes() == b"\x7fELF" and os.access(seeded, os.X_OK)


# ═══════════════════════════════════════════════════════ printing


def _report(**over) -> dict:
    base = {"fixture": "multi_repo", "mode": "offline", "cases": 31,
            "verdicts": {"correct": 30, "alternate": 0, "wrong": 1, "unanswered": 0},
            "completeness": {"value": 30, "required": 27, "pass": True},
            "node_coverage": None, "edge_rate": None, "qa": None,
            "cross_repo": {"expected": 2, "actual": 1, "missing": ["CrossRepoCalls a -> b"],
                           "extra": [], "pass": False},
            "discovery": None, "per_category": {"NEG": {"total": 1, "complete": 0, "sound": 0}},
            "edges": {"precision": 0.5, "recall": 1.0},
            "misses": [{"id": "NEG-002", "verdict": "wrong", "category": "NEG",
                        "expected": {"status": "resolved", "targets": [
                            {"qualname": "Logger.Info", "module": "m", "kind": "method", "file": "web/a.go"}]},
                        "got": {"records": [{"status": "external", "via": "none", "reason": None,
                                             "target": {"file": "shared/b.go", "qualname": "Logger.Info",
                                                        "kind": "method", "repo": "shared"},
                                             "external": {"module": "example.com/iryo/shared"},
                                             "candidates": []}]}}],
            "pass": False, "eval_dir": "/out/eval_multi_repo", "seconds": 2.5}
    base.update(over)
    return base


def test_print_report_shows_gates_wrong_answers_and_misses():
    lines: list[str] = []
    ok = evaluate.print_report(_report(), lines.append)
    text = "\n".join(lines)
    assert ok is False
    assert "EVALUATE — multi_repo  (mode: offline)" in text
    assert "[FAIL] Zero wrong     : 1 wrong" in text
    assert "[FAIL] Cross-repo set : 1 actual / 2 expected" in text
    assert "WRONG NEG-002: expected resolved Logger.Info@web/a.go; got external example.com/iryo/shared " \
           "shared:shared/b.go:Logger.Info" in text
    assert "missing cross-repo edge: CrossRepoCalls a -> b" in text
    assert "RESULT: FAIL" in text and "/out/eval_multi_repo" in text
    assert "Time: 2.5 s" in text


IMPORTS_FAIL = {"records": 6, "mapped": 1, "unmapped": 3, "ambiguous": 2, "gated": True, "pass": False,
                "unmapped_examples": ["app/main.go:4:2 example.com/fx/util: the record names no clause"],
                "ambiguous_examples": ["app/main.go:5:2 example.com/fx/two: two has clauses other, two"]}


def test_print_report_shows_the_imports_gate_and_the_row_counts():
    lines: list[str] = []
    evaluate.print_report(_report(imports=IMPORTS_FAIL, rows={"PackageImports": 0, "ClassEmbeds": 4,
                                                               "ClassImplements": 2}),
                          lines.append, quiet_misses=True)
    text = "\n".join(lines)
    assert "[FAIL] Imports        : 1 of 6 in-repo import records mapped (3 unmapped, 2 ambiguous)" in text
    assert "Rows: PackageImports 0  ClassEmbeds 4  ClassImplements 2" in text
    assert "unmapped import: app/main.go:4:2 example.com/fx/util: the record names no clause" in text
    assert "ambiguous import" not in text                 # quiet: only what fails is listed
    lines.clear()
    evaluate.print_report(_report(imports={**IMPORTS_FAIL, "gated": False}), lines.append)
    text = "\n".join(lines)
    assert "[INFO] Imports        : 1 of 6" in text
    assert "ambiguous import: app/main.go:5:2 example.com/fx/two: two has clauses other, two" in text


def test_print_report_of_an_error():
    lines: list[str] = []
    assert evaluate.print_report({"fixture": "f", "mode": "env", "pass": False, "error": "boom",
                                  "hint": "do x"}, lines.append) is False
    assert lines == ["  ERROR [f, env]: boom", "  HINT:  do x"]


def test_print_report_of_discovery_problems():
    lines: list[str] = []
    rep = _report(fixture="go_layouts", cross_repo=None, misses=[],
                  verdicts={"correct": 31, "alternate": 0, "wrong": 0, "unanswered": 0},
                  discovery={"pass": False, "layouts": 23, "problems": {"ws": ["module source: none, expected proxy"]}})
    evaluate.print_report(rep, lines.append)
    text = "\n".join(lines)
    assert "[FAIL] Discovery      : 1 of 23 layouts differ" in text
    assert "discovery ws: module source: none, expected proxy" in text


def test_run_evaluation_returns_whether_every_fixture_passed(out_dir, monkeypatch):
    reports = {"a": _report(fixture="a", **{"pass": True}), "b": _report(fixture="b")}
    calls = []

    def fake(fixture, repo_root, *, env_mode, printer, dump_edges=False):
        calls.append((fixture, env_mode))
        return reports[fixture]
    monkeypatch.setattr(evaluate, "evaluate_fixture", fake)
    assert evaluate.run_evaluation(["a"], str(ROOT), env_mode=True, printer=_quiet) is True
    assert evaluate.run_evaluation(["a", "b"], str(ROOT), printer=_quiet) is False
    assert calls == [("a", True), ("a", False), ("b", False)]


def test_fixture_names_match_the_cli():
    from graph_generator import __main__ as cli
    assert evaluate.FIXTURES == ("iryo-kanri-system", "go_layouts", "multi_repo")
    assert tuple(cli.EVAL_FIXTURES) == evaluate.FIXTURES


def test_records_of_a_planted_unresolved_site_are_unanswered(out_dir):
    # offline records for an offline score: env answers can be wrong offline (IFC-018, spec R52)
    m, runs = perfect_runs("iryo-kanri-system", offline=True)
    case = next(c for c in m["cases"] if evaluate.case_expect(c, "offline")["status"] == "resolved")
    recs = runs["iryo-kanri-system"].resolutions["files"][case["file"]]["calls"]
    for r in recs:
        if (r["site"]["line"], r["site"]["col"]) == (case["line"], case["col"]):
            r.update(record(case["line"], case["col"], "unresolved", reason="missing-module"))
    report = evaluate.evaluate_fixture("iryo-kanri-system", str(ROOT), env_mode=False, printer=_quiet,
                                       ingest=_ingest_from(runs))
    assert report["verdicts"]["unanswered"] == 1 and report["verdicts"]["wrong"] == 0
