"""CLI (C9): parsing, exit codes, peers, the hand-offs to later work packages."""

import inspect
import json
import os
import re
import sys
import types

import pytest

from graph_generator import __main__ as m
from graph_generator import config, pipeline
from graph_generator.pipeline import Check, PipelineData
from tests.conftest import SAMPLE_TREE, FakeSpannerDb, write_tree


def _args(**kw):
    return types.SimpleNamespace(**kw)


def _run_main(monkeypatch, argv, command):
    seen = {}
    monkeypatch.setattr(m, command, lambda args: seen.setdefault("args", args))
    m.main(argv)
    return seen["args"]


def _exit_code(argv) -> int:
    with pytest.raises(SystemExit) as ei:
        m.main(argv)
    return ei.value.code


# ── parsing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("flag,mode", [("--all", "all"), ("--graph", "graph"), ("--wiki", "wiki")])
def test_analyze_takes_exactly_one_mode(monkeypatch, flag, mode):
    args = _run_main(monkeypatch, ["analyze", "/src/web", flag], "cmd_analyze")
    assert {"all": args.all, "graph": args.graph, "wiki": args.wiki} == {
        k: k == mode for k in ("all", "graph", "wiki")}


@pytest.mark.parametrize("argv", [
    ["analyze", "/src/web"],                       # no mode
    ["analyze", "/src/web", "--graph", "--wiki"],  # two modes
    ["analyze", "/src/web", "--all", "--graph"],
    ["analyze", "/src/web", "generate"],           # one positional only
    ["generate", "wiki", "/src/web"],              # codedoc_python commands are gone
    ["analyze", "/src/web", "--all", "--provision-env"],
    ["setup"],                                     # setup needs go or spanner
    ["setup", "python"],
    ["trial", "/src/etcd"],                        # --repo-name is required
    ["evaluate", "--fixture", "py_layouts"],
    ["nope"],
])
def test_usage_errors_exit_2(argv, capsys):
    assert _exit_code(argv) == 2


def test_main_without_command_prints_help(capsys):
    assert _exit_code([]) == 2
    assert "Available commands" in capsys.readouterr().out


def test_analyze_flags(monkeypatch):
    args = _run_main(monkeypatch, [
        "analyze", "/src/web", "--graph", "--repo-name", "web", "--peer", "shared=/src/shared",
        "--peer", "/src/api", "--download-modules", "--no-spanner"], "cmd_analyze")
    assert (args.target_dir, args.repo_name, args.repos) == ("/src/web", "web", None)
    assert args.peer == ["shared=/src/shared", "/src/api"]
    assert args.download_modules is True and args.no_spanner is True
    args = _run_main(monkeypatch, ["analyze", "--repos", "repos.json", "--wiki"], "cmd_analyze")
    assert args.target_dir is None and args.repos == "repos.json" and args.peer == []
    assert args.download_modules is False and args.no_spanner is False


def test_other_commands_parse(monkeypatch):
    assert _run_main(monkeypatch, ["doctor"], "cmd_doctor").target_dir is None
    assert _run_main(monkeypatch, ["doctor", "/src/web"], "cmd_doctor").target_dir == "/src/web"
    assert _run_main(monkeypatch, ["setup", "go"], "cmd_setup_go").setup_command == "go"
    args = _run_main(monkeypatch, ["setup", "spanner", "--skip-instance"], "cmd_setup_spanner")
    assert args.skip_instance is True
    args = _run_main(monkeypatch, ["trial", "/src/etcd", "--repo-name", "etcd", "--packages",
                                   "./server/...", "./pkg/...", "--download-modules"], "cmd_trial")
    assert (args.target_dir, args.repo_name) == ("/src/etcd", "etcd")
    assert args.packages == ["./server/...", "./pkg/..."] and args.download_modules is True
    args = _run_main(monkeypatch, ["evaluate", "--fixture", "go_layouts", "--env"], "cmd_evaluate")
    assert args.fixture == "go_layouts" and args.env is True
    assert _run_main(monkeypatch, ["evaluate"], "cmd_evaluate").fixture == "all"
    assert _run_main(monkeypatch, ["crossref", "--dry-run"], "cmd_crossref").dry_run is True
    assert _run_main(monkeypatch, ["init", "--force"], "cmd_init").force is True
    _run_main(monkeypatch, ["validate"], "cmd_validate")


def test_help_shows_the_go_cli(capsys):
    assert _exit_code(["--help"]) == 0
    out = capsys.readouterr().out
    for word in ("analyze", "doctor", "setup", "trial", "evaluate", "crossref", "validate",
                 "init", "Exit codes"):
        assert word in out
    assert _exit_code(["analyze", "--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("--all", "--graph", "--wiki", "--repo-name", "--repos", "--peer",
                 "--download-modules", "--no-spanner"):
        assert flag in out


# ── exit codes and analyze ───────────────────────────────────────

@pytest.mark.parametrize("argv,message", [
    (["analyze", "--graph"], "provide a target directory"),
    (["analyze", "{repo}", "--repos", "{manifest}", "--graph"], "not both"),
    (["analyze", "--repos", "{manifest}", "--repo-name", "x", "--graph"], "--repo-name"),
    (["analyze", "{repo}/missing", "--graph"], "directory not found"),
    (["analyze", "--repos", "{repo}/none.json", "--graph"], "cannot read the --repos manifest"),
    (["analyze", "{repo}", "--graph", "--peer", "{repo}/missing"], "directory not found"),
    (["analyze", "{repo}", "--graph", "--peer", "{repo}"], "is the repo being ingested"),
    (["doctor", "{repo}/missing"], "directory not found"),
    (["trial", "{repo}/missing", "--repo-name", "x"], "directory not found"),
])
def test_bad_inputs_exit_2(tmp_path, monkeypatch, capsys, argv, message):
    monkeypatch.setattr(config, "PEER_REPOS", None)
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    manifest = tmp_path / "repos.json"
    manifest.write_text(json.dumps([repo]), encoding="utf-8")
    assert _exit_code([a.format(repo=repo, manifest=manifest) for a in argv]) == 2
    assert message in capsys.readouterr().err


def test_preflight_errors_exit_3_before_any_work(tmp_path, out_dir, monkeypatch, capsys):
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    monkeypatch.setattr(pipeline, "preflight", lambda mode, **kw: ([
        Check("toolchain", "error", "go1.24.3 is older than go1.25", "run setup go"),
        Check("spanner", "ok", "codedoc-instance/codedoc-go")], None))
    monkeypatch.setattr(pipeline, "run_graph_track", lambda data: pytest.fail("work started"))
    assert _exit_code(["analyze", repo, "--graph"]) == 3
    captured = capsys.readouterr()
    assert "go1.24.3 is older than go1.25" in captured.out and "fix: run setup go" in captured.out
    assert "1 check(s) failed: toolchain" in captured.err


def test_pending_hand_offs_exit_1_naming_the_work_package(tmp_path, out_dir, monkeypatch, capsys):
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    monkeypatch.setattr(pipeline, "preflight", lambda mode, **kw: ([], None))
    monkeypatch.setattr(pipeline, "phase1_scan", lambda data: None)
    monkeypatch.setitem(sys.modules, "graph_generator.go_extract", None)  # not delivered
    assert _exit_code(["analyze", repo, "--graph", "--no-spanner"]) == 1
    assert "go_extract.extract_phase is not implemented yet (WP-B2)" in capsys.readouterr().err


def test_analyze_passes_options_and_peers_to_the_graph_track(tmp_path, out_dir, monkeypatch):
    monkeypatch.setattr(config, "PEER_REPOS", None)
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    shared = write_tree(tmp_path / "shared", {"go.mod": "module example.com/shared\n"})
    toolchain = types.SimpleNamespace(go="/go/bin/go", version="go1.27.2")
    seen = {}
    monkeypatch.setattr(pipeline, "preflight", lambda mode, **kw: (
        seen.update(mode=mode, **kw) or [], toolchain))
    monkeypatch.setattr(m, "_build_peer", lambda name, root: ("peer", name, root))
    runs = []
    monkeypatch.setattr(pipeline, "run_graph_track", runs.append)
    m.main(["analyze", repo, "--graph", "--repo-name", "iryo", "--download-modules",
            "--no-spanner", "--peer", f"shared={shared}"])
    assert seen == {"mode": "graph", "no_spanner": True, "modules": "download",
                    "repos": [("iryo", os.path.abspath(repo))],
                    "peers": {"iryo": [("peer", "shared", os.path.realpath(shared))]}}
    [data] = runs
    assert (data.repo, data.mode, data.no_spanner, data.toolchain) == (
        "iryo", "download", True, toolchain)
    assert data.peers == [("peer", "shared", os.path.realpath(shared))]
    assert os.path.isfile(os.path.join(out_dir, "repos", "iryo", "timing_report.json"))


def test_download_mode_from_the_setting(tmp_path, out_dir, monkeypatch):
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    monkeypatch.setattr(config, "GO_MODULES_DOWNLOAD", True)
    monkeypatch.setattr(pipeline, "preflight", lambda mode, **kw: ([], None))
    runs = []
    monkeypatch.setattr(pipeline, "run_graph_track", runs.append)
    m.main(["analyze", repo, "--graph"])
    assert runs[0].mode == "download" and runs[0].no_spanner is False


def test_repos_manifest_runs_each_repo_in_its_own_output_dir(tmp_path, out_dir, monkeypatch):
    monkeypatch.setattr(config, "PEER_REPOS", None)
    web = write_tree(tmp_path / "web", SAMPLE_TREE)
    api = write_tree(tmp_path / "api", SAMPLE_TREE)
    manifest = tmp_path / "repos.json"
    manifest.write_text(json.dumps([{"name": "web-app", "path": web}, api]), encoding="utf-8")
    monkeypatch.setattr(pipeline, "preflight", lambda mode, **kw: ([], None))
    monkeypatch.setattr(m, "_build_peer", lambda name, root: name)
    runs = []
    monkeypatch.setattr(pipeline, "run_graph_track",
                        lambda data: runs.append((data.repo, data.peers, config.OUTPUT_DIR)))
    m.main(["analyze", "--repos", str(manifest), "--graph"])
    assert runs == [("web-app", ["api"], os.path.join(out_dir, "repos", "web-app")),
                    ("api", ["web-app"], os.path.join(out_dir, "repos", "api"))]


# ── peers ────────────────────────────────────────────────────────

def _peer_dir(root, rel):
    path = os.path.join(str(root), rel)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "go.mod"), "w", encoding="utf-8") as f:
        f.write(f"module example.com/{os.path.basename(path)}\n")
    return os.path.realpath(path)


@pytest.mark.parametrize("spec,name,rel", [
    ("shared={root}/x/shared", "shared", "x/shared"),
    ("{root}/web", "web", "web"),
    (" api = {root}/api ", "api", "api"),
    ("{root}/a=b/repo", "repo", "a=b/repo"),
    ("会計={root}/会計 repo", "会計", "会計 repo"),
])
def test_peer_spec_forms(tmp_path, monkeypatch, spec, name, rel):
    monkeypatch.setattr(config, "PEER_REPOS", None)
    root = _peer_dir(tmp_path, rel)
    pairs = m._peer_specs("web-app", str(tmp_path), _args(peer=[spec.format(root=tmp_path)]))
    assert pairs == [(name, root)]


def test_peer_relative_path_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PEER_REPOS", None)
    root = _peer_dir(tmp_path, "libs/shared")
    monkeypatch.chdir(tmp_path)
    assert m._peer_specs("web", str(tmp_path), _args(peer=["libs/shared"])) == [("shared", root)]


def test_peers_from_flags_then_peer_repos(tmp_path, monkeypatch):
    shared, api = _peer_dir(tmp_path, "shared"), _peer_dir(tmp_path, "api")
    monkeypatch.setattr(config, "PEER_REPOS", f"iryo-api={api}")
    assert m._peer_specs("web", str(tmp_path), _args(peer=[shared])) == [
        ("shared", shared), ("iryo-api", api)]
    monkeypatch.setattr(config, "PEER_REPOS", None)
    assert m._peer_specs("web", str(tmp_path), _args()) == []
    monkeypatch.setattr(config, "PEER_REPOS", f"shared={api}")
    with pytest.raises(m.UsageError, match="names two paths"):
        m._peer_specs("web", str(tmp_path), _args(peer=[f"shared={shared}"]))
    monkeypatch.setattr(config, "PEER_REPOS", None)
    with pytest.raises(m.UsageError, match="has no path"):
        m._peer_specs("web", str(tmp_path), _args(peer=["shared="]))
    with pytest.raises(m.UsageError, match="two names"):
        m._peer_specs("web", str(tmp_path), _args(peer=[f"a={shared}", f"b={shared}"]))
    with pytest.raises(m.UsageError, match="is the repo being ingested"):
        m._peer_specs("web", str(tmp_path), _args(peer=[f"web={api}"]))


def test_peers_are_discovered_by_go_units(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PEER_REPOS", None)
    shared = _peer_dir(tmp_path, "shared")
    [peer] = m._peers_for("web", str(tmp_path / "web"), _args(peer=[shared]))
    assert (peer.name, peer.root) == ("shared", shared)
    assert [u.key for u in peer.units] == ["example.com/shared"]
    monkeypatch.setitem(sys.modules, "graph_generator.go_units", None)
    with pytest.raises(NotImplementedError, match=r"go_units\.discover_peer .*\(WP-D\)"):
        m._peers_for("web", str(tmp_path / "web"), _args(peer=[shared]))


# ── commands that hand off ───────────────────────────────────────

def _install(monkeypatch, module, **attrs):
    fake = types.ModuleType(f"graph_generator.{module}")
    for name, value in attrs.items():
        setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, f"graph_generator.{module}", fake)


@pytest.mark.parametrize("fixture,ok,expected_fixtures,code", [
    ("all", True, ["iryo-kanri-system", "go_layouts", "multi_repo"], 0),
    ("multi_repo", False, ["multi_repo"], 1),
])
def test_cmd_evaluate_uses_contract_signature(monkeypatch, fixture, ok, expected_fixtures, code):
    calls = {}

    def run_evaluation(fixtures, repo_root, *, env_mode=False, printer=print,
                       dump_edges=False, quiet_misses=False):
        calls.update(fixtures=fixtures, repo_root=repo_root, env_mode=env_mode,
                     dump_edges=dump_edges, quiet_misses=quiet_misses)
        return ok
    _install(monkeypatch, "evaluate", run_evaluation=run_evaluation)
    assert _exit_code(["evaluate", "--fixture", fixture, "--env"]) == code
    assert calls["fixtures"] == expected_fixtures and calls["env_mode"] is True
    assert calls["dump_edges"] is False and calls["quiet_misses"] is False
    assert os.path.isdir(os.path.join(calls["repo_root"], "graph_generator"))


def test_cmd_crossref_dry_run(monkeypatch):
    seen = {}
    _install(monkeypatch, "crossref",
             run_crossref=lambda printer=print, write=True: seen.setdefault("write", write))
    m.main(["crossref", "--dry-run"])
    assert seen["write"] is False


def test_cmd_trial_calls_wp_b6(tmp_path, monkeypatch):
    calls = {}

    def run_trial(target_dir, repo, *, packages, download, skip_oracle):
        calls.update(target_dir=target_dir, repo=repo, packages=packages, download=download,
                     skip_oracle=skip_oracle)
        return True
    _install(monkeypatch, "trial", run_trial=run_trial)
    assert _exit_code(["trial", str(tmp_path), "--repo-name", "etcd", "--packages", "./a"]) == 0
    assert calls == {"target_dir": str(tmp_path), "repo": "etcd", "packages": ["./a"],
                     "download": False, "skip_oracle": False}
    assert _exit_code(["trial", str(tmp_path), "--repo-name", "etcd", "--skip-oracle"]) == 0
    assert calls["skip_oracle"] is True


def test_cmd_setup_go_installs_into_tools(monkeypatch, capsys):
    seen = {}

    def setup_go(dest):
        seen["dest"] = dest
        return types.SimpleNamespace(go=os.path.join(dest, "bin", "go"), version="go1.27.2",
                                     goroot=dest)
    _install(monkeypatch, "go_toolchain", setup_go=setup_go)
    m.main(["setup", "go"])
    root = os.path.dirname(os.path.dirname(os.path.abspath(m.__file__)))
    assert seen["dest"] == os.path.join(root, ".tools", "go")
    assert "Installed go1.27.2" in capsys.readouterr().out


@pytest.mark.parametrize("argv,module,wp", [
    (["evaluate"], "evaluate", "WP-B5"),
    (["crossref"], "crossref", "WP-B5"),
    (["trial", ".", "--repo-name", "x"], "trial", "WP-B6"),
    (["setup", "go"], "go_toolchain", "WP-D"),
])
def test_missing_hand_offs_exit_1_naming_the_work_package(monkeypatch, capsys, argv, module, wp):
    monkeypatch.setitem(sys.modules, f"graph_generator.{module}", None)  # not importable
    assert _exit_code(argv) == 1
    err = capsys.readouterr().err
    assert "not implemented yet" in err and wp in err


def test_handoff_keeps_third_party_import_errors(monkeypatch):
    def broken(name, *a, **k):
        raise ModuleNotFoundError("No module named 'grpc'", name="grpc")
    monkeypatch.setattr(pipeline.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="grpc"):
        pipeline._handoff("go_units", "discover", "WP-D")


def test_handoff_missing_attribute(monkeypatch):
    _install(monkeypatch, "go_units")
    with pytest.raises(NotImplementedError, match=r"go_units\.discover is not implemented yet \(WP-D\)"):
        pipeline._handoff("go_units", "discover", "WP-D")


# ── doctor ───────────────────────────────────────────────────────

def test_doctor_stub_prints_a_table(monkeypatch, capsys, tmp_path):
    for module in ("go_toolchain", "go_units"):
        monkeypatch.setitem(sys.modules, f"graph_generator.{module}", None)
    monkeypatch.setattr(pipeline, "_MEMINFO", str(tmp_path / "no-meminfo"))
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: FakeSpannerDb(sql_rows={
        "INFORMATION_SCHEMA.TABLES": [[t] for t in pipeline_tables()],
        "INFORMATION_SCHEMA.COLUMNS": pipeline_columns()}))
    m.main(["doctor"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if line.split() == ["Check", "Status", "Detail"])
    rows = {line.split()[0]: line.split()[1] for line in lines[header + 2:]
            if line.strip() and not line.strip().startswith(("fix:", "Not implemented"))}
    assert rows == {"builds": "todo", "toolchain": "todo", "helper": "skip", "memory": "skip",
                    "spanner": "ok", "gemini": "warn"}
    assert "go_toolchain.find_toolchain is not implemented yet (WP-D)" in out
    assert "Not implemented yet: builds, toolchain" in out


def test_doctor_with_a_repo_and_errors_exits_3(tmp_path, monkeypatch, capsys):
    repo = write_tree(tmp_path / "web", SAMPLE_TREE)
    seen = {}
    monkeypatch.setattr(pipeline, "preflight", lambda mode, **kw: (
        seen.update(mode=mode, **kw) or [Check("builds", "error", "bad entry", "fix it")], None))
    assert _exit_code(["doctor", repo]) == 3
    assert seen == {"mode": "doctor", "repos": [("web", os.path.abspath(repo))]}
    assert "fix: fix it" in capsys.readouterr().out


def pipeline_tables():
    from graph_generator.setup_spanner_graph import EDGE_TABLES, NODE_TABLES
    return NODE_TABLES + EDGE_TABLES


def pipeline_columns():
    """INFORMATION_SCHEMA.COLUMNS rows (table, column, type) of the spec's schema."""
    from graph_generator.setup_spanner_graph import _TYPE_SQL, EDGE_SPEC, NODE_SPEC
    return [[t, c, _TYPE_SQL[typ]] for t in pipeline_tables()
            for c, typ in (NODE_SPEC.get(t) or EDGE_SPEC[t])[1]]


# ── kept commands ────────────────────────────────────────────────

def test_cmd_init_defaults_and_force(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(config, "GCP_PROJECT", None)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    m.cmd_init(_args(force=False))
    env = (tmp_path / ".env").read_text()
    assert f"SPANNER_DATABASE={config.SPANNER_DATABASE}" in env
    assert "# GOOGLE_CLOUD_PROJECT=" in env  # no project given: ADC / gcloud default
    m.cmd_init(_args(force=False))  # exists, no --force: unchanged
    monkeypatch.setattr("builtins.input", lambda *a: "myproj")
    m.cmd_init(_args(force=True))
    assert "GOOGLE_CLOUD_PROJECT=myproj" in (tmp_path / ".env").read_text()


def test_timing_report_banner_and_monotonic_clock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    data = PipelineData(target_dir="/x", repo="iryo",
                        timings={"phase1_scan": 1.2, "phase1c_resolve": 63.4,
                                 "phase4_topics": 2.0},
                        resolutions={"stats": {"by_status": {"resolved": 7}}},
                        graph_counts={"Files": 3})
    m._print_timing_report(data, 65.1)
    m._print_banner("all", data)
    out = capsys.readouterr().out
    assert "TIMING REPORT" in out and "Phase 1.6: Resolution" in out and "Phase 4: Topics" in out
    assert out.index("Phase 1: Scan") < out.index("Phase 1.6") < out.index("Phase 4: Topics")
    assert "resolved=7" in out and "Files=3" in out and "analyze --all (repo: iryo)" in out
    with open(tmp_path / "out" / "timing_report.json", encoding="utf-8") as f:
        assert json.load(f)["statistics"]["sites_by_status"] == {"resolved": 7}
    for module in (m, pipeline):
        assert "time.time()" not in inspect.getsource(module)


def test_timing_report_has_the_gemini_calls_and_tokens_per_phase(tmp_path, monkeypatch, capsys):
    """R49: calls and input, output and thinking tokens of every wiki phase,
    thinking apart from the answer although both are billed as output."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    usage = {"phase2_node_summaries": {"calls": 74, "input_tokens": 42137, "output_tokens": 13527,
                                       "thinking_tokens": 32392},
             "phase4_topics": {"calls": 14, "input_tokens": 10714, "output_tokens": 8126,
                               "thinking_tokens": 11591}}
    data = PipelineData(target_dir="/x", repo="iryo", gemini_usage=usage)
    m._print_timing_report(data, 65.1)
    out = capsys.readouterr().out
    assert "Gemini calls and tokens (input / output / thinking):" in out
    assert re.search(r"Phase 2: Node summaries\s+74 calls\s+42,137 / 13,527 / 32,392", out)
    assert re.search(r"TOTAL\s+88 calls\s+52,851 / 21,653 / 43,983", out)
    with open(tmp_path / "out" / "timing_report.json", encoding="utf-8") as f:
        gemini = json.load(f)["statistics"]["gemini"]
    assert gemini["phase4_topics"] == usage["phase4_topics"]
    assert gemini["total"] == {"calls": 88, "input_tokens": 52851, "output_tokens": 21653,
                               "thinking_tokens": 43983}
    m._print_timing_report(PipelineData(target_dir="/x", repo="iryo"), 1.0)  # a graph-only run
    assert "Gemini calls" not in capsys.readouterr().out


def test_cmd_setup_spanner_and_project_report(monkeypatch, capsys):
    import graph_generator.setup_spanner_graph as s
    calls = []
    for name in ("create_instance", "create_database", "verify"):
        monkeypatch.setattr(s, name, lambda *a, _n=name, **k: calls.append((_n, a[0])))
    monkeypatch.setattr(config, "GCP_PROJECT", None)
    m.main(["setup", "spanner", "--skip-instance"])
    out = capsys.readouterr().out
    assert "Skipping instance creation" in out and "ADC / gcloud default" in out
    assert calls == [("create_database", None), ("verify", None)]
    monkeypatch.setattr(config, "GCP_PROJECT", "my-project")
    m._check_config()  # reports, never exits
    assert "Project:     my-project" in capsys.readouterr().out


def test_project_label_uses_the_adc_project_or_says_unresolved(monkeypatch):
    import google.auth
    monkeypatch.setattr(config, "GCP_PROJECT", None)
    assert m._project_label() == "ADC / gcloud default (unresolved)"  # lookup fails
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))
    assert m._project_label() == "adc-project (ADC / gcloud default)"
    monkeypatch.setattr(config, "GCP_PROJECT", "my-project")
    assert m._project_label() == "my-project"


def test_cmd_validate_with_fake_db(monkeypatch, capsys):
    db = FakeSpannerDb(sql_rows={
        "GROUP BY source_repo": [["web", "shared", 4]],
        "GROUP BY repo": [["web", 10], ["api", 7]],
        "COUNT(*)": [[3]],
    })
    monkeypatch.setattr(m, "_get_spanner_db", lambda: db)
    m.main(["validate"])
    out = capsys.readouterr().out
    assert "GRAPH VALIDATION" in out and "Orphan checks" in out and "Project:" in out
    assert "web=10" in out and "api=7" in out and "web → shared" in out
