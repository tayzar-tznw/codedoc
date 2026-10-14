"""Preflight (spec §2.7): every check at once, each problem with its fix;
checks that call into WP-D modules show "todo" until those land."""

import os
import sys
import types

import pytest

from graph_generator import config, pipeline
from graph_generator.pipeline import Check, build_configs, format_checks, preflight
from tests.conftest import FakeSpannerDb

TOOLCHAIN = types.SimpleNamespace(go="/opt/go/bin/go", version="go1.27.2", goroot="/opt/go")


from graph_generator import go_toolchain, go_units  # noqa: E402  (WP-D's real modules)


def _module(monkeypatch, name, **attrs):
    """Replace attributes of WP-D's real module *name* (its other functions stay)."""
    module = {"go_toolchain": go_toolchain, "go_units": go_units}[name]
    for key, value in attrs.items():
        monkeypatch.setattr(module, key, value, raising=False)
    return module


@pytest.fixture(autouse=True)
def plenty_of_memory(tmp_path, monkeypatch):
    path = tmp_path / "meminfo"
    path.write_text("MemAvailable: 67108864 kB\n", encoding="ascii")
    monkeypatch.setattr(pipeline, "_MEMINFO", str(path))


@pytest.fixture
def go_tools(monkeypatch):
    calls = {}

    def ensure_helper(toolchain, output_dir):
        calls["helper"] = (toolchain, output_dir)
        return os.path.join(output_dir, "go", "bin", "gohelper-go1.27.2-abc")
    _module(monkeypatch, "go_toolchain", find_toolchain=lambda: TOOLCHAIN,
            ensure_helper=ensure_helper)
    return calls


def _spec_columns(tables):
    """INFORMATION_SCHEMA.COLUMNS rows (table, column, type) of the spec's schema."""
    from graph_generator.setup_spanner_graph import _TYPE_SQL, EDGE_SPEC, NODE_SPEC
    return [[t, c, _TYPE_SQL[typ]] for t in tables
            for c, typ in (NODE_SPEC.get(t) or EDGE_SPEC[t])[1]]


@pytest.fixture
def schema(monkeypatch):
    from graph_generator.setup_spanner_graph import EDGE_TABLES, NODE_TABLES
    db = FakeSpannerDb(sql_rows={
        "INFORMATION_SCHEMA.TABLES": [[t] for t in NODE_TABLES + EDGE_TABLES],
        "INFORMATION_SCHEMA.COLUMNS": _spec_columns(NODE_TABLES + EDGE_TABLES)})
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    return db


@pytest.fixture
def adc(monkeypatch):
    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))


def _rows(checks):
    return {c.name: c.status for c in checks}


# ── builds (go_units.builds_from_config, WP-D) ───────────────────

def test_builds_come_from_go_units(monkeypatch):
    monkeypatch.setattr(config, "GO_EXTRA_BUILDS", "windows/amd64 darwin/arm64+ui")
    assert [(b.id, b.primary, b.cgo) for b in build_configs()] == [
        ("linux-amd64", True, False), ("windows-amd64", False, False),
        ("darwin-arm64+ui", False, False)]
    row = pipeline._check_builds()
    assert (row.status, row.detail) == ("ok", "linux-amd64 (primary), windows-amd64, darwin-arm64+ui")


def test_malformed_build_settings_stop_the_run(monkeypatch):
    monkeypatch.setattr(config, "GO_EXTRA_BUILDS", "windows")
    [row] = [c for c in preflight("graph", no_spanner=True)[0] if c.name == "builds"]
    assert row.status == "error" and "is not goos/goarch" in row.detail
    assert "GO_EXTRA_BUILDS" in row.fix


def test_builds_todo_without_go_units(monkeypatch):
    monkeypatch.setitem(sys.modules, "graph_generator.go_units", None)
    row = pipeline._check_builds()
    assert row.status == "todo" and "(WP-D)" in row.detail


# ── memory (R15: a warning only) ─────────────────────────────────

@pytest.mark.parametrize("meminfo,status,detail", [
    ("MemTotal: 131072000 kB\nMemAvailable: 125829120 kB\n", "ok", "120.0 GiB available"),
    ("MemAvailable: 2097152 kB\n", "warn", "2.0 GiB available"),
    ("MemTotal: 1 kB\n", "skip", "MemAvailable not readable"),
])
def test_memory_check(tmp_path, monkeypatch, meminfo, status, detail):
    path = tmp_path / "meminfo"
    path.write_text(meminfo, encoding="ascii")
    monkeypatch.setattr(pipeline, "_MEMINFO", str(path))
    row = pipeline._check_memory()
    assert row.status == status and row.detail.startswith(detail)


def test_memory_check_without_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_MEMINFO", str(tmp_path / "missing"))
    assert pipeline._check_memory().status == "skip"


# ── toolchain and helper ─────────────────────────────────────────

def test_missing_wp_d_modules_are_todo_rows(monkeypatch):
    for name in ("go_toolchain", "go_units"):
        monkeypatch.setitem(sys.modules, f"graph_generator.{name}", None)
    checks, toolchain = preflight("graph", no_spanner=True, repos=[("iryo", "/r")])
    assert toolchain is None
    assert _rows(checks) == {"builds": "todo", "toolchain": "todo", "helper": "skip",
                             "memory": "ok", "groups": "skip"}
    assert "(WP-D)" in checks[0].detail and "(WP-D)" in checks[1].detail


def test_toolchain_and_helper_ok(go_tools, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out" / "repos" / "iryo"))
    checks, toolchain = preflight("graph", no_spanner=True)
    assert toolchain is TOOLCHAIN
    assert _rows(checks) == {"builds": "ok", "toolchain": "ok", "helper": "ok", "memory": "ok"}
    assert checks[1].detail == "go1.27.2 (/opt/go/bin/go)"
    # The helper is cached in the shared OUTPUT_DIR/go, not under repos/<repo>
    assert go_tools["helper"] == (TOOLCHAIN, str(tmp_path / "out"))


def test_toolchain_failure_names_the_fix(monkeypatch):
    def find_toolchain():
        raise RuntimeError("go1.24.3 at /usr/bin/go is older than the minimum go1.25")
    _module(monkeypatch, "go_toolchain", find_toolchain=find_toolchain)
    checks, toolchain = preflight("graph", no_spanner=True)
    row = checks[1]
    assert (row.status, toolchain) == ("error", None)
    assert "older than the minimum go1.25" in row.detail
    assert "GO_TOOLCHAIN" in row.fix and "setup go" in row.fix
    assert checks[2].status == "skip"


def test_helper_build_failure(monkeypatch):
    def ensure_helper(toolchain, output_dir):
        raise RuntimeError("compile error")
    _module(monkeypatch, "go_toolchain", find_toolchain=lambda: TOOLCHAIN,
            ensure_helper=ensure_helper)
    row = preflight("graph", no_spanner=True)[0][2]
    assert row.status == "error" and "compile error" in row.detail


# ── doctor <dir>: discovery rows ─────────────────────────────────

def _group(gid, kind="module", modules=("a",), status="ok", missing=(), degraded=False):
    return types.SimpleNamespace(id=gid, kind=kind, modules=list(modules), status=status,
                                 missing_modules=list(missing), degraded=degraded)


def _discovery(groups, warnings=()):
    return types.SimpleNamespace(units=[1, 2, 3], groups=groups, warnings=list(warnings),
                                 mode="offline", module_source="file-proxy", toolchain="go1.27.2")


def test_doctor_reports_groups_go_lines_and_missing_modules(go_tools, monkeypatch, tmp_path,
                                                            schema, adc):
    seen = {}
    groups = [_group("ws0", "workspace", ["a", "b"]), _group("m0", status="toolchain"),
              _group("m1", missing=["example.com/x@v1.0.0"]), _group("m2", degraded=True)]

    def discover(repo_root, repo, *, builds, peers, mode, output_dir, toolchain):
        seen.update(repo_root=repo_root, repo=repo, mode=mode, output_dir=output_dir, peers=peers)
        return _discovery(groups, ["group m0: go 1.28 in tools/go.mod is newer than go1.27.2; "
                                   "set GO_TOOLCHAIN or run setup go"])
    _module(monkeypatch, "go_units", discover=discover)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    repo = tmp_path / "iryo"
    repo.mkdir()
    checks, _ = preflight("doctor", repos=[("iryo", str(repo))])
    assert [(c.name, c.status) for c in checks] == [
        ("builds", "ok"), ("toolchain", "ok"), ("helper", "ok"), ("memory", "ok"),
        ("modules", "ok"), ("group ws0", "ok"), ("group m0", "warn"), ("group m1", "warn"),
        ("group m2", "warn"), ("discovery", "warn"), ("spanner", "ok"), ("gemini", "ok")]
    by_name = {c.name: c for c in checks}
    assert "at least as new as the go line" in by_name["group m0"].fix
    assert "1 required modules are missing offline" in by_name["group m1"].detail
    assert "--download-modules" in by_name["group m1"].fix
    assert by_name["group m2"].detail.endswith(", degraded")
    assert "go 1.28 in tools/go.mod" in by_name["discovery"].detail
    assert seen == {"repo_root": os.path.realpath(repo), "repo": "iryo", "mode": "offline",
                    "output_dir": str(tmp_path / "out"), "peers": []}
    assert by_name["gemini"].detail == "gemini-3.5-flash in global, project adc-project"


def test_doctor_discovery_todo_and_errors(go_tools, monkeypatch, tmp_path):
    monkeypatch.delattr(go_units, "discover")
    rows = pipeline._check_repo(str(tmp_path), "x", TOOLCHAIN)
    assert [(r.name, r.status) for r in rows] == [("groups", "todo")]

    def discover(*a, **k):
        raise ValueError("go.work: malformed use directive")
    _module(monkeypatch, "go_units", discover=discover)
    [row] = pipeline._check_repo(str(tmp_path), "x", TOOLCHAIN)
    assert row.status == "error" and "malformed use directive" in row.detail
    assert pipeline._check_repo(str(tmp_path), "x", None)[0].status == "skip"


# ── Spanner and Gemini ───────────────────────────────────────────

def test_spanner_schema_present(schema):
    row = pipeline._check_spanner(soft=False)
    assert row.status == "ok"
    assert row.detail == "codedoc-instance/codedoc-go (graph code_graph_a, ID prefix a): schema present"


def test_spanner_missing_tables(monkeypatch):
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: FakeSpannerDb(
        sql_rows={"INFORMATION_SCHEMA.TABLES": [["Files"]],
                  "INFORMATION_SCHEMA.COLUMNS": _spec_columns(["Files"])}))
    row = pipeline._check_spanner(soft=False)
    assert row.status == "error" and "tables missing (" in row.detail and " more)" in row.detail
    assert "setup spanner" in row.fix and "--no-spanner" in row.fix
    assert pipeline._check_spanner(soft=True).status == "warn"


def test_spanner_unreachable():
    # conftest disables ADC, so the real client cannot authenticate
    row = pipeline._check_spanner(soft=False)
    assert row.status == "error" and "credentials" in row.fix


def test_gemini_without_credentials():
    row = pipeline._check_gemini(soft=False)
    assert row.status == "error" and "application-default login" in row.fix
    assert pipeline._check_gemini(soft=True).status == "warn"


def test_explicit_project_wins(adc, monkeypatch):
    monkeypatch.setattr(config, "GCP_PROJECT", "my-project")
    assert pipeline._check_gemini(soft=False).detail.endswith("project my-project")


# ── modes and the table ──────────────────────────────────────────

@pytest.mark.parametrize("mode,no_spanner,names", [
    ("graph", False, ["builds", "toolchain", "helper", "memory", "spanner"]),
    ("graph", True, ["builds", "toolchain", "helper", "memory"]),
    ("wiki", False, ["spanner", "gemini"]),
    ("wiki", True, ["gemini"]),
    ("all", False, ["builds", "toolchain", "helper", "memory", "spanner", "gemini"]),
    ("doctor", False, ["builds", "toolchain", "helper", "memory", "spanner", "gemini"]),
])
def test_checks_per_mode(go_tools, schema, adc, mode, no_spanner, names):
    checks, _ = preflight(mode, no_spanner=no_spanner)
    assert [c.name for c in checks] == names
    assert all(c.status == "ok" for c in checks)


def test_service_problems_are_errors_in_analyze_and_warnings_in_doctor(go_tools, monkeypatch):
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: FakeSpannerDb())
    assert _rows(preflight("all")[0])["spanner"] == "error"
    assert _rows(preflight("all")[0])["gemini"] == "error"
    assert _rows(preflight("doctor")[0])["spanner"] == "warn"
    assert _rows(preflight("doctor")[0])["gemini"] == "warn"


def test_format_checks_shows_fixes_for_problems_only():
    table = format_checks([Check("toolchain", "ok", "go1.27.2", "never shown"),
                           Check("spanner", "error", "codedoc-go: 3 tables missing", "run setup spanner"),
                           Check("helper", "todo", "not implemented yet (WP-D)", "hidden too")])
    lines = table.splitlines()
    assert lines[0].split() == ["Check", "Status", "Detail"]
    assert "never shown" not in table and "hidden too" not in table
    assert lines[3].split()[:2] == ["spanner", "error"]
    assert lines[4].strip() == "fix: run setup spanner"


def test_base_output_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out" / "repos" / "iryo"))
    assert pipeline._base_output_dir() == str(tmp_path / "out")
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    assert pipeline._base_output_dir() == str(tmp_path / "out")



# ── R15: what stops a run ────────────────────────────────────────

def _discover_with(monkeypatch, groups, seen=None):
    def discover(repo_root, repo, *, builds, peers, mode, output_dir, toolchain):
        if seen is not None:
            seen.setdefault(repo, dict(peers=peers, mode=mode))
        return _discovery(groups)
    _module(monkeypatch, "go_units", discover=discover)


def test_every_group_too_new_stops_the_run(go_tools, monkeypatch, tmp_path):
    _discover_with(monkeypatch, [_group("ws0", status="toolchain"), _group("m0", status="toolchain")])
    checks, _ = preflight("graph", no_spanner=True, repos=[("iryo", str(tmp_path))])
    [stop] = [c for c in checks if c.status == "error"]  # go_units.stops (WP-D) owns the rule
    assert stop.name == "go lines"
    assert "every group needs a newer Go than the toolchain go1.27.2" in stop.detail
    assert "GO_TOOLCHAIN" in stop.detail and "setup go" not in stop.detail


@pytest.mark.parametrize("groups", [
    [_group("ws0", status="toolchain"), _group("m0")],                  # one group too new
    [_group("m0", missing=["example.com/x@v1.0.0"]), _group("m1", degraded=True)],
    [_group("m0", status="error")],                                     # e.g. inconsistent vendor
    [],                                                                 # no module at all
])
def test_other_repo_problems_only_warn(go_tools, monkeypatch, tmp_path, groups):
    _discover_with(monkeypatch, groups)
    checks, _ = preflight("graph", no_spanner=True, repos=[("iryo", str(tmp_path))])
    assert not [c for c in checks if c.status == "error"]


def test_analyze_preflight_discovers_every_repo_with_its_peers(go_tools, monkeypatch, tmp_path):
    seen = {}
    _discover_with(monkeypatch, [_group("m0")], seen)
    peer = types.SimpleNamespace(name="shared")
    checks, _ = preflight("graph", no_spanner=True, modules="download",
                          repos=[("web", str(tmp_path)), ("api", str(tmp_path))],
                          peers={"web": [peer]})
    assert seen == {"web": {"peers": [peer], "mode": "download"},
                    "api": {"peers": [], "mode": "download"}}
    assert [c.name for c in checks if ":" in c.name] == [
        "web: modules", "web: group m0", "api: modules", "api: group m0"]
    # --wiki rebuilds no graph, so it discovers nothing
    seen.clear()
    preflight("wiki", no_spanner=True, repos=[("web", str(tmp_path))])
    assert seen == {}
