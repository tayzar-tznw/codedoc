"""Unit tests for graph_generator.resolution — stub LSP client, no live jdtls.

Canned payload shapes come from the real S2 spike fixtures in
tests/fixtures/jdtls_spike/ (jdtls 1.61: definition / MarkedString hover /
java/classFileContents / jdt:// URI contract), so these tests pin the resolver
to observed server behavior.
"""

import json
import os

import pytest

from graph_generator import config
from graph_generator import resolution as R

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE = os.path.join(HERE, "fixtures", "jdtls_spike")


def spike(name: str) -> dict:
    with open(os.path.join(SPIKE, name), encoding="utf-8") as f:
        return json.load(f)


_HANDSHAKE = spike("02_handshake_notifications.json")["no_sources_session"]
STATUS_SEQUENCE = [s["params"] for s in _HANDSHAKE["language_statuses"]]
STATUS_BEFORE_READY = [p for p in STATUS_SEQUENCE if p.get("type") != "ServiceReady"]

_CROSS_JAR = spike("05_cross_jar_without_sources.json")
FROZEN_JAR_URI = _CROSS_JAR["jdt_uri_exact"]
FROZEN_JAR_DEFINITION = _CROSS_JAR["definition"]["response"]
FROZEN_JAR_SOURCE = _CROSS_JAR["class_file_contents"]["response"]
FROZEN_JRT_URI = spike("07_jdk_jrt_definition.json")["jdt_uri_exact"]
FROZEN_JRT_DEFINITION = spike("07_jdk_jrt_definition.json")["list_get"]["response"]
HOVERS = spike("08_hover_payloads.json")
RECORDS = spike("09_record_definitions.json")


def jdt_jar_uri(container, package, cls, group, artifact, version):
    """The frozen in-feed jar URI shape with its GAV/type segments swapped."""
    u = FROZEN_JAR_URI
    u = u.replace("jp.iryo.spike.lib", package)
    u = u.replace("=/maven.groupId=/jp.iryo.spike=/", f"=/maven.groupId=/{group}=/")
    u = u.replace("=/maven.artifactId=/mini-lib=/", f"=/maven.artifactId=/{artifact}=/")
    u = u.replace("=/maven.version=/1.0.0=/", f"=/maven.version=/{version}=/")
    u = u.replace("Calculations", cls)
    u = u.replace("mini-lib-1.0.0.jar", container)
    return u


def jdt_plain_uri(container, package, cls):
    """A jdt:// URI with NO maven.* query segments (unmapped container)."""
    return f"jdt://contents/{container}/{package}/{cls}.java?=app/x=/{package}%28{cls}.class"


# ---------------------------------------------------------------------
# stub client
# ---------------------------------------------------------------------


class StubClient:
    """Duck-typed LspClient. responses: {(method, uri_suffix, line, char): value}.
    A value may be a dict/list/str (result), an Exception instance (raised), or
    a callable(params)->result. Requests with no match return None."""

    def __init__(self, responses, fail_on_start: Exception | None = None,
                 statuses=None, **kwargs):
        self.responses = responses
        self.fail_on_start = fail_on_start
        self.kwargs = kwargs
        self.statuses = list(statuses) if statuses is not None else [
            {"type": "ServiceReady", "message": "ServiceReady"}]
        self.opened: list[str] = []
        self.requests: list[tuple] = []
        self.progress_ends = 1
        self.notification_counts = {R._STATUS_NOTIFICATION: len(self.statuses)}
        self.is_alive = True
        self.pending_count = 0

    async def start(self):
        if self.fail_on_start:
            raise self.fail_on_start

    async def stop(self):
        self.is_alive = False

    async def notify(self, method, params):
        if method == "textDocument/didOpen":
            self.opened.append(params["textDocument"]["uri"])

    def notification_count(self, method, predicate=None):
        if method != R._STATUS_NOTIFICATION:
            return 0
        if predicate is None:
            return len(self.statuses)
        return sum(1 for p in self.statuses if predicate(p))

    async def wait_notification(self, method, predicate=None, timeout=60.0):
        if self.notification_count(method, predicate) > 0:
            return
        raise R.LspTimeout(f"no matching {method} notification within {timeout}s")

    async def request(self, method, params, timeout=None):
        uri = ""
        line = char = None
        if isinstance(params, dict):
            td = params.get("textDocument") or {}
            uri = td.get("uri") or params.get("uri") or ""
            pos = params.get("position") or {}
            line, char = pos.get("line"), pos.get("character")
        self.requests.append((method, uri, line, char))
        if method == "textDocument/documentSymbol":
            for (m, suffix, _, _), val in self.responses.items():
                if m == method and (not suffix or suffix in uri):
                    return val
            return [{"name": "stub", "kind": 5}]
        for (m, suffix, ln, ch), val in self.responses.items():
            if m != method:
                continue
            if suffix and suffix not in uri:
                continue
            if ln is not None and ln != line:
                continue
            if ch is not None and ch != char:
                continue
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(params)
            return val
        return None


def make_factory(responses_by_project, fail_projects=(), statuses=None,
                 fail_exc=None):
    """responses_by_project: {project-root dirname: responses dict}."""
    made = {}
    cmds = []
    kwargs_list = []

    def factory(cmd, cwd, config_sections=None, default_timeout=None, **kwargs):
        cmds.append(list(cmd))
        kwargs_list.append(dict(kwargs, config_sections=config_sections, cwd=cwd))
        name = os.path.basename(cwd.rstrip("/"))
        if name in fail_projects:
            client = StubClient({}, fail_on_start=fail_exc or R.LspConnectionError("boom"),
                                statuses=statuses, **kwargs)
        else:
            client = StubClient(responses_by_project.get(name, {}),
                                statuses=statuses, **kwargs)
        made[name] = client
        return client

    factory.made = made
    factory.cmds = cmds
    factory.kwargs = kwargs_list
    return factory


# ---------------------------------------------------------------------
# synthetic project scaffolding
# ---------------------------------------------------------------------


def pom_xml(artifact, group="jp.iryo", version="1.0.0"):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{group}</groupId>
  <artifactId>{artifact}</artifactId>
  <version>{version}</version>
</project>
"""


def scaffold(tmp_path, files: dict[str, str], poms: dict[str, str] | None = None):
    """files: relpath -> content. poms: project dir -> artifactId."""
    root = tmp_path / "src"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for d, artifact in (poms or {"app": "app"}).items():
        p = root / d / "pom.xml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(pom_xml(artifact), encoding="utf-8")
    return str(root)


def run(root, out_dir, factory, files=None):
    java = files or [os.path.join(dp, f) for dp, _, fs in os.walk(root)
                     for f in fs if f.endswith(".java")]
    return R.run_resolution(root, sorted(java), str(out_dir), client_factory=factory)


def uri(root, rel):
    return "file://" + os.path.join(root, rel)


def loc(root, rel, line0, c0, c1):
    return [{"uri": uri(root, rel),
             "range": {"start": {"line": line0, "character": c0},
                       "end": {"line": line0, "character": c1}}}]


def jdt_loc(u, line0=0, char0=0):
    return [{"uri": u, "range": {"start": {"line": line0, "character": char0},
                                 "end": {"line": line0, "character": char0}}}]


def hover(sig):
    """jdtls answers legacy MarkedString[] — a java block plus a source link."""
    return {"contents": [{"language": "java", "value": sig},
                         "Source: *[x](file:///x#1)*"]}


def site_by(res, token, kind=None):
    for s in res["sites"]:
        if s["token"] == token and (kind is None or s["site_kind"] == kind):
            return s
    return None


FLOW = "app/src/main/java/app/Flow.java"
CALC = "app/src/main/java/app/calc/Calc.java"

FLOW_JAVA = """package app;

import app.calc.Calc;

public class Flow {
    public void run() {
        Calc fee = new Calc();
        int total = fee.add(1, 2);
    }
}
"""

CALC_JAVA = """package app.calc;

public class Calc {
    public int add(int x, int y) { return x + y; }
    public int add(int... xs) { return 0; }
}
"""


def base_project(tmp_path):
    return scaffold(tmp_path, {FLOW: FLOW_JAVA, CALC: CALC_JAVA})


def col_of(source, line0, token, occurrence=0):
    line = source.splitlines()[line0]
    at = -1
    for _ in range(occurrence + 1):
        at = line.index(token, at + 1)
    return at


# ---------------------------------------------------------------------
# launch / readiness
# ---------------------------------------------------------------------


def test_launch_argv_is_isolated_per_session_and_pins_jdk21(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JAVA_LSP_CMD", "jdtls")
    root = scaffold(tmp_path, {
        FLOW: FLOW_JAVA,
        "lib/src/main/java/lib/L.java": "package lib;\npublic class L { }\n",
    }, poms={"app": "app", "lib": "lib"})
    factory = make_factory({})
    run(root, tmp_path / "out", factory)

    assert len(factory.cmds) == 2
    dirs = set()
    for cmd in factory.cmds:
        assert cmd[0] == "jdtls"
        assert cmd[1] == "-configuration" and cmd[3] == "-data"
        cfg, data = cmd[2], cmd[4]
        assert cfg != data
        assert f"--jvm-arg=-Dosgi.configuration.area={cfg}" in cmd
        assert "--jvm-arg=-Xms128m" in cmd and "--jvm-arg=-Xmx700m" in cmd
        dirs |= {cfg, data}
    assert len(dirs) == 4, dirs      # unique -data AND -configuration per session

    kw = factory.kwargs[0]
    assert kw["env"]["JAVA_HOME"] == R._JDK21_HOME
    assert kw["env"]["JAVA_HOME"].endswith("java-21-openjdk-amd64")
    assert kw["env"].get("PATH")     # inherits the caller environment
    assert kw["watch_notifications"] == {R._STATUS_NOTIFICATION}
    assert kw["config_sections"]["java"]["import"]["gradle"]["enabled"] is False
    assert R._init_options()["extendedClientCapabilities"]["classFileContentsSupport"] is True


def test_ready_requires_service_ready_not_merely_starting(tmp_path, monkeypatch):
    """The real language/status stream is mostly Starting/Started events; only
    params.type == 'ServiceReady' may end the wait."""
    assert any(p.get("type") == "Starting" for p in STATUS_BEFORE_READY)
    assert any(p.get("type") == "Started" for p in STATUS_BEFORE_READY)
    assert not any(p.get("type") == "ServiceReady" for p in STATUS_BEFORE_READY)

    monkeypatch.setattr(config, "JAVA_LSP_TIMEOUT_S", 0)
    root = base_project(tmp_path)
    responses = {("textDocument/documentSymbol", "", None, None): []}
    factory = make_factory({"app": responses}, statuses=STATUS_BEFORE_READY)
    res = run(root, tmp_path / "out", factory)
    assert res["solutions"][0]["status"] == "fallback"
    assert "LspTimeout" in res["solutions"][0]["reason"]


def test_empty_document_symbols_after_service_ready_counts_as_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JAVA_LSP_TIMEOUT_S", 0)
    root = base_project(tmp_path)
    line = 7
    c = col_of(FLOW_JAVA, line, "add")
    responses = {
        ("textDocument/documentSymbol", "", None, None): [],
        ("textDocument/definition", "Flow.java", line, c):
            loc(root, CALC, 3, col_of(CALC_JAVA, 3, "add"), 0),
        ("textDocument/hover", "Flow.java", line, c):
            hover("int app.calc.Calc.add(int x, int y)"),
    }
    factory = make_factory({"app": responses}, statuses=STATUS_SEQUENCE)
    res = run(root, tmp_path / "out", factory)
    assert res["solutions"][0]["status"] == "resolved", res["solutions"][0]
    assert site_by(res, "add", "invocation")["status"] == "resolved"


def test_warmup_prefers_a_cross_artifact_site(tmp_path):
    local = """package app;

public class Local {
    void run() { helper(); }
    void helper() { }
}
"""
    uses = """package app;

import iryo.kernel.Logger;

public class Uses {
    void run() {
        Logger l = new Logger();
        l.log("x");
    }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/app/Local.java": local,
                               "app/src/main/java/app/Uses.java": uses})
    proj = R.discover_projects(root, [
        os.path.join(root, "app/src/main/java/app/Local.java"),
        os.path.join(root, "app/src/main/java/app/Uses.java")]).projects[0]
    chosen = R._select_warmup_site(proj)
    assert chosen is not None
    fp, site = chosen
    assert fp.endswith("Uses.java")          # NOT the alphabetically-first file
    assert site.token == "Logger" and site.site_kind == "object_creation"

    # no cross-artifact import anywhere -> first call site is still probed
    root2 = scaffold(tmp_path / "v2", {"app/src/main/java/app/Local.java": local})
    proj2 = R.discover_projects(
        root2, [os.path.join(root2, "app/src/main/java/app/Local.java")]).projects[0]
    fp2, site2 = R._select_warmup_site(proj2)
    assert fp2.endswith("Local.java") and site2.token == "helper"


# ---------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------


def test_discovery_uses_reactor_root_for_sessions_and_nearest_pom_for_artifacts(tmp_path):
    root = scaffold(tmp_path, {
        "reactor/mod-a/src/main/java/a/A.java": "package a;\npublic class A { }\n",
        "reactor/mod-b/src/main/java/b/B.java": "package b;\npublic class B { }\n",
        "solo/src/main/java/s/S.java": "package s;\npublic class S { }\n",
    }, poms={"reactor": "reactor-root", "reactor/mod-a": "mod-a",
             "reactor/mod-b": "mod-b", "solo": "solo"})
    files = [os.path.join(dp, f) for dp, _, fs in os.walk(root)
             for f in fs if f.endswith(".java")]
    d = R.discover_projects(root, sorted(files))

    assert [os.path.relpath(p.pom, root) for p in d.projects] == [
        "reactor/pom.xml", "solo/pom.xml"]          # one session per reactor root
    artifacts = {os.path.basename(f): a for f, a in d.artifact_of_file.items()}
    assert artifacts == {"A.java": "mod-a", "B.java": "mod-b", "S.java": "solo"}
    assert ("jp.iryo", "mod-a", "1.0.0") in d.gav_map
    assert d.gav_map[("jp.iryo", "solo", "1.0.0")] == "solo"


def test_files_without_any_ancestor_pom_go_to_fallback(tmp_path):
    root = str(tmp_path / "src")
    os.makedirs(os.path.join(root, "loose"), exist_ok=True)
    with open(os.path.join(root, "loose", "A.java"), "w") as f:
        f.write("public class A { }\n")
    res = R.run_resolution(root, [os.path.join(root, "loose", "A.java")],
                           str(tmp_path / "out"), client_factory=make_factory({}))
    assert res["fallback_files"] == ["loose/A.java"]
    assert res["solutions"] == []


# ---------------------------------------------------------------------
# in-repo resolution + hover crosscheck
# ---------------------------------------------------------------------


def test_in_repo_definition_and_hover_agree(tmp_path):
    root = base_project(tmp_path)
    line = 7
    c = col_of(FLOW_JAVA, line, "add")
    responses = {
        ("textDocument/definition", "Flow.java", line, c):
            loc(root, CALC, 3, col_of(CALC_JAVA, 3, "add"), 0),
        ("textDocument/hover", "Flow.java", line, c):
            hover("int app.calc.Calc.add(int x, int y)"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "add", "invocation")
    assert s["status"] == "resolved", s
    assert s["target"]["symbol"] == "app.calc.Calc.add(int, int)"
    assert s["target"]["artifact"] == "app"
    assert s["target"]["file"] == os.path.join("app", "src", "main", "java", "app",
                                               "calc", "Calc.java")
    assert s["enclosing"] == "app.Flow.run()"
    assert s["enclosing_artifact"] == "app"
    assert s["resolved_via"] == "lsp"


def test_hover_arity_mismatch_degrades_to_unresolved(tmp_path):
    root = base_project(tmp_path)
    line = 7
    c = col_of(FLOW_JAVA, line, "add")
    responses = {
        ("textDocument/definition", "Flow.java", line, c):
            loc(root, CALC, 3, col_of(CALC_JAVA, 3, "add"), 0),
        ("textDocument/hover", "Flow.java", line, c):
            hover("int app.calc.Calc.add(int x, int y, int z)"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    assert site_by(res, "add", "invocation")["status"] == "unresolved"


def test_lying_definition_range_corrected_via_sibling_overload_pool(tmp_path):
    """definition points at the varargs overload but hover shows the bound
    (int, int) one — the sibling pool must correct it."""
    root = base_project(tmp_path)
    line = 7
    c = col_of(FLOW_JAVA, line, "add")
    responses = {
        ("textDocument/definition", "Flow.java", line, c):
            loc(root, CALC, 4, col_of(CALC_JAVA, 4, "add"), 0),      # add(int...)
        ("textDocument/hover", "Flow.java", line, c):
            hover("int app.calc.Calc.add(int x, int y)"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "add", "invocation")
    assert s["status"] == "resolved", s
    assert s["target"]["symbol"] == "app.calc.Calc.add(int, int)"


def test_multi_candidate_group_converges_by_param_types(tmp_path):
    """jdtls normally answers ONE overload; the convergence guard stays for
    servers/versions that return the whole group."""
    calc = """package app.calc;

public class Calc {
    public int add(int x, int y) { return 0; }
    public int add(long x, long y) { return 1; }
    public int add(double x, double y) { return 2; }
}
"""
    root = scaffold(tmp_path, {FLOW: FLOW_JAVA, CALC: calc})
    line = 7
    c = col_of(FLOW_JAVA, line, "add")
    group = (loc(root, CALC, 3, col_of(calc, 3, "add"), 0)
             + loc(root, CALC, 4, col_of(calc, 4, "add"), 0)
             + loc(root, CALC, 5, col_of(calc, 5, "add"), 0))
    responses = {
        ("textDocument/definition", "Flow.java", line, c): group,
        ("textDocument/hover", "Flow.java", line, c):
            hover("int app.calc.Calc.add(long x, long y)"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "add", "invocation")
    assert s["status"] == "resolved", s
    assert s["target"]["symbol"] == "app.calc.Calc.add(long, long)"

    responses2 = dict(responses)
    responses2[("textDocument/hover", "Flow.java", line, c)] = \
        hover("int app.calc.Calc.add(String x, String y)")
    res2 = run(root, tmp_path / "out2", make_factory({"app": responses2}))
    s2 = site_by(res2, "add", "invocation")
    assert s2["status"] == "ambiguous"
    assert s2["candidate_count"] == 3


# ---------------------------------------------------------------------
# jdt:// binary bridge
# ---------------------------------------------------------------------


EXT_APP = """package app;

import jp.iryo.spike.lib.Calculations;

public class Ext {
    public void run() {
        long widened = Calculations.calculate(1, 2L);
    }
}
"""


def test_jdt_bridge_external_jar_yields_exact_overload_and_artifact_label(tmp_path):
    root = scaffold(tmp_path, {"app/src/main/java/app/Ext.java": EXT_APP})
    line = 6
    c = col_of(EXT_APP, line, "calculate")
    responses = {
        ("textDocument/definition", "Ext.java", line, c): FROZEN_JAR_DEFINITION,
        ("textDocument/hover", "Ext.java", line, c): HOVERS["overload"]["response"],
        ("java/classFileContents", "", None, None): FROZEN_JAR_SOURCE,
    }
    factory = make_factory({"app": responses})
    res = run(root, tmp_path / "out", factory)
    s = site_by(res, "calculate", "invocation")
    assert s["status"] == "external", s
    assert s["target"]["artifact"] == "mini-lib"        # GAV not in repo -> artifactId
    assert s["target"]["symbol"] == "jp.iryo.spike.lib.Calculations.calculate(long, long)"

    # the opaque jdt:// URI must round-trip VERBATIM into classFileContents
    sent = [r for r in factory.made["app"].requests if r[0] == "java/classFileContents"]
    assert sent and sent[0][1] == FROZEN_JAR_URI


def test_jdt_bridge_maps_in_repo_gav_back_to_the_source_declaration(tmp_path):
    kernel_src = """package iryo.kernel;

public class Logger {
    public void log(String message) { }
}
"""
    app_src = """package app;

import iryo.kernel.Logger;

public class Uses {
    public void run(Logger l) {
        l.log("x");
    }
}
"""
    root = scaffold(tmp_path, {
        "app/src/main/java/app/Uses.java": app_src,
        "iryo-kernel/src/main/java/iryo/kernel/Logger.java": kernel_src,
    }, poms={"app": "app", "iryo-kernel": "iryo-kernel"})
    u = jdt_jar_uri("iryo-kernel-1.0.0.jar", "iryo.kernel", "Logger",
                    "jp.iryo", "iryo-kernel", "1.0.0")
    line = 6
    c = col_of(app_src, line, "log")
    responses = {
        ("textDocument/definition", "Uses.java", line, c):
            jdt_loc(u, 3, kernel_src.splitlines()[3].index("log")),
        ("textDocument/hover", "Uses.java", line, c):
            hover("void iryo.kernel.Logger.log(String message)"),
        ("java/classFileContents", "", None, None): kernel_src,
    }
    res = run(root, tmp_path / "out",
              make_factory({"app": responses, "iryo-kernel": {}}))
    s = [x for x in res["sites"] if x["token"] == "log" and "Uses.java" in x["file"]][0]
    assert s["status"] == "resolved", s
    assert s["target"]["symbol"] == "iryo.kernel.Logger.log(String)"
    assert s["target"]["artifact"] == "iryo-kernel"
    assert s["target"]["file"].endswith(os.path.join("iryo", "kernel", "Logger.java"))
    assert s["target"]["line"] == 4
    assert s["enclosing_artifact"] == "app"


def test_jdk_module_target_is_external_java_base(tmp_path):
    src = """package app;

import java.util.List;

public class Jdk {
    public void run(List<String> visits) {
        String first = visits.get(0);
    }
}
"""
    jdk_source = """package java.util;

public interface List<E> {
    E get(int index);
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/app/Jdk.java": src})
    line = 6
    c = col_of(src, line, "get")
    responses = {
        ("textDocument/definition", "Jdk.java", line, c): FROZEN_JRT_DEFINITION,
        ("textDocument/hover", "Jdk.java", line, c): hover("E java.util.List.get(int index)"),
        ("java/classFileContents", "", None, None): jdk_source,
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "get", "invocation")
    assert s["status"] == "external", s
    assert s["target"]["artifact"] == "java.base"       # module segment verbatim
    assert s["target"]["symbol"] == "java.util.List<E>.get(int)"


def test_unmapped_jar_label_strips_the_version_suffix(tmp_path):
    src = """package app;

public class Legacy {
    public void run(ext.Tool t) {
        t.exec();
    }
}
"""
    tool = """package ext;

public class Tool {
    public void exec() { }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/app/Legacy.java": src})
    u = jdt_plain_uri("legacy-tools-2.1.0.jar", "ext", "Tool")
    line = 4
    c = col_of(src, line, "exec")
    responses = {
        ("textDocument/definition", "Legacy.java", line, c):
            jdt_loc(u, 3, tool.splitlines()[3].index("exec")),
        ("textDocument/hover", "Legacy.java", line, c): hover("void ext.Tool.exec()"),
        ("java/classFileContents", "", None, None): tool,
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "exec", "invocation")
    assert s["status"] == "external", s
    assert s["target"]["artifact"] == "legacy-tools"
    assert s["target"]["symbol"] == "ext.Tool.exec()"


def test_artifact_label_rules_from_the_frozen_contract():
    gav_map = {("jp.iryo.spike", "mini-lib", "1.0.0"): "mini-lib"}
    assert R._artifact_from_jdt_uri(FROZEN_JAR_URI, gav_map) == "mini-lib"
    assert R._artifact_from_jdt_uri(FROZEN_JAR_URI, {}) == "mini-lib"   # GAV artifactId
    assert R._artifact_from_jdt_uri(FROZEN_JRT_URI, {}) == "java.base"
    assert R._artifact_from_jdt_uri(
        jdt_plain_uri("commons-lang3-3.14.0.jar", "org.apache", "X"), {}) == "commons-lang3"
    assert R._artifact_from_jdt_uri(
        jdt_plain_uri("weird-lib-2.1.0-SNAPSHOT.jar", "w", "X"), {}) == "weird-lib"
    assert R._artifact_from_jdt_uri(jdt_plain_uri("noversion.jar", "w", "X"), {}) == "noversion"
    assert _fqn(FROZEN_JAR_URI) == "jp.iryo.spike.lib.Calculations"
    assert _fqn(FROZEN_JRT_URI) == "java.util.List"


def _fqn(u):
    return R._uri_type_name(u)


def test_degenerate_record_ranges_use_hover_to_pick_ctor_and_accessor(tmp_path):
    rec_source = """package jp.iryo.spike.lib;

public record Rec(int amount) {
}
"""
    src = """package app;

import jp.iryo.spike.lib.Rec;

public class Recs {
    public void run() {
        Rec rec = new Rec(5);
        int amount = rec.amount();
    }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/app/Recs.java": src})
    ctor_line, acc_line = 6, 7
    ctor_col = col_of(src, ctor_line, "Rec", 1)
    acc_col = col_of(src, acc_line, "amount", 1)
    responses = {
        # both spike ⑨ responses carry a degenerate 0,0 range
        ("textDocument/definition", "Recs.java", ctor_line, ctor_col):
            RECORDS["constructor"]["response"],
        ("textDocument/hover", "Recs.java", ctor_line, ctor_col):
            HOVERS["constructor"]["response"],
        ("textDocument/definition", "Recs.java", acc_line, acc_col):
            RECORDS["accessor"]["response"],
        ("textDocument/hover", "Recs.java", acc_line, acc_col):
            HOVERS["record_accessor"]["response"],
        ("java/classFileContents", "", None, None): rec_source,
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    ctor = site_by(res, "Rec", "object_creation")
    assert ctor["status"] == "external", ctor
    assert ctor["target"]["symbol"] == "jp.iryo.spike.lib.Rec.<init>(int)"
    assert ctor["target"]["kind"] == "constructor"
    assert ctor["target"]["artifact"] == "mini-lib"
    acc = site_by(res, "amount", "invocation")
    assert acc["status"] == "external", acc
    assert acc["target"]["symbol"] == "jp.iryo.spike.lib.Rec.amount()"


def test_implicit_constructor_yields_the_bare_type_symbol(tmp_path):
    logger = """package jp.iryo.spike.lib;

public class Logger {
    public void log(String m) { }
}
"""
    src = """package app;

import jp.iryo.spike.lib.Logger;

public class Impl {
    public void run() {
        Logger ledger = new Logger();
    }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/app/Impl.java": src})
    line = 6
    c = col_of(src, line, "Logger", 1)
    u = jdt_jar_uri("mini-lib-1.0.0.jar", "jp.iryo.spike.lib", "Logger",
                    "jp.iryo.spike", "mini-lib", "1.0.0")
    responses = {
        ("textDocument/definition", "Impl.java", line, c): jdt_loc(u, 2, 13),
        ("textDocument/hover", "Impl.java", line, c):
            hover("jp.iryo.spike.lib.Logger"),
        ("java/classFileContents", "", None, None): logger,
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "Logger", "object_creation")
    assert s["status"] == "external", s
    assert s["target"]["symbol"] == "jp.iryo.spike.lib.Logger"
    assert s["target"]["kind"] == "type"


def test_ctor_argcount_guard_refuses_varargs_risk(tmp_path):
    decompiled = """package ext;

public class X {
    public X(int... xs) { }
    public X(int a, boolean b) { }
}
"""
    src = """package app;

public class Guard {
    public void run() {
        ext.X x = new ext.X(1, 2);
    }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/app/Guard.java": src})
    line = 4
    c = col_of(src, line, "X", 1)      # the X of `new ext.X(`
    u = jdt_plain_uri("ext-tools-1.0.0.jar", "ext", "X")
    responses = {
        ("textDocument/definition", "Guard.java", line, c): jdt_loc(u, 0, 0),
        ("java/classFileContents", "", None, None): decompiled,
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "X", "object_creation")
    assert s["status"] == "external", s
    assert "<init>" not in s["target"]["symbol"]        # never fabricate a ctor
    assert s["target"]["symbol"] == "ext.X"

    # control: without the varargs risk the same shape DOES pick the ctor
    safe = decompiled.replace("public X(int... xs) { }", "public X(int a) { }")
    responses2 = dict(responses)
    responses2[("java/classFileContents", "", None, None)] = safe
    res2 = run(root, tmp_path / "out2", make_factory({"app": responses2}))
    s2 = site_by(res2, "X", "object_creation")
    assert s2["target"]["symbol"] == "ext.X.<init>(int, boolean)", s2


# ---------------------------------------------------------------------
# hover rescue corroboration
# ---------------------------------------------------------------------


NESTED = """package app.data;

public class Archive {
    public static class Compactor {
        public Compactor start() { return this; }
    }
}
"""

NESTED_CALLER = """package app;

import app.data.Archive;

public class Caller {
    public void run() {
        Archive.Compactor c = new Archive.Compactor().start();
    }
}
"""


def test_hover_rescue_on_definition_error(tmp_path):
    root = scaffold(tmp_path, {"app/src/main/java/app/data/Archive.java": NESTED,
                               "app/src/main/java/app/Caller.java": NESTED_CALLER})
    line = 6
    c = col_of(NESTED_CALLER, line, "start")
    responses = {
        ("textDocument/definition", "Caller.java", line, c):
            R.LspError(-32603, "Internal error"),
        ("textDocument/hover", "Caller.java", line, c):
            hover("Compactor app.data.Archive.Compactor.start()"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "start", "invocation")
    assert s["status"] == "resolved", s
    assert s["target"]["symbol"] == "app.data.Archive$Compactor.start()"


def test_hover_rescue_needs_jdt_uri_type_corroboration(tmp_path):
    """classFileContents flaked (empty): a jdt:// URI naming the candidate's
    own type corroborates the rescue; an unrelated type must block it."""
    root = scaffold(tmp_path, {"app/src/main/java/app/data/Archive.java": NESTED,
                               "app/src/main/java/app/Caller.java": NESTED_CALLER})
    line = 6
    c = col_of(NESTED_CALLER, line, "start")

    good = jdt_plain_uri("other-1.0.0.jar", "app.data", "Compactor")
    responses = {
        ("textDocument/definition", "Caller.java", line, c): jdt_loc(good, 0, 0),
        ("textDocument/hover", "Caller.java", line, c):
            hover("Compactor app.data.Archive.Compactor.start()"),
        ("java/classFileContents", "", None, None): "",
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "start", "invocation")
    assert s["status"] == "resolved", s
    assert s["target"]["symbol"] == "app.data.Archive$Compactor.start()"

    bad = jdt_plain_uri("other-1.0.0.jar", "totally.unrelated", "Widget")
    responses2 = dict(responses)
    responses2[("textDocument/definition", "Caller.java", line, c)] = jdt_loc(bad, 0, 0)
    res2 = run(root, tmp_path / "out2", make_factory({"app": responses2}))
    s2 = site_by(res2, "start", "invocation")
    assert s2["status"] == "external", s2                 # honest external answer
    assert s2["target"]["symbol"] == "totally.unrelated.Widget"


def test_no_rescue_on_clean_null_definition(tmp_path):
    src = """package app;

public class Printer {
    public void emit(String s) { }
}
"""
    caller = """package app;

public class NullDef {
    public void run(Object w) {
        unknownThing(w);
    }
    void unknownThing(Object o) { }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/app/Printer.java": src,
                               "app/src/main/java/app/NullDef.java": caller})
    line = 4
    c = col_of(caller, line, "unknownThing")
    responses = {
        # definition answers null cleanly; hover points at a unique in-repo member
        ("textDocument/hover", "NullDef.java", line, c):
            hover("void app.Printer.emit(String s)"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "unknownThing", "invocation")
    assert s["status"] == "unresolved", s


# ---------------------------------------------------------------------
# interface -> unique implementation upgrade
# ---------------------------------------------------------------------


IFACE_SRC = """package app;

public class Wire {
    public interface Repository { String load(); }
    public interface Exporter { String export(); }
    public interface Policy { default int fee(int a) { return a; } }

    public static class SqlRepository implements Repository {
        public String load() { return "x"; }
    }
    public static class CsvExporter implements Exporter {
        public String export() { return "c"; }
    }
    public static class XmlExporter implements Exporter {
        public String export() { return "x"; }
    }
    public static class Standard implements Policy {
        public int fee(int a) { return a * 2; }
    }

    public void run(Repository r, Exporter e, Policy p) {
        r.load();
        e.export();
        p.fee(10);
    }
}
"""


def test_interface_member_upgrades_only_to_a_unique_concrete_impl(tmp_path):
    root = scaffold(tmp_path, {"app/src/main/java/app/Wire.java": IFACE_SRC})
    lines = IFACE_SRC.splitlines()
    load_line = next(i for i, l in enumerate(lines) if "r.load();" in l)
    export_line = next(i for i, l in enumerate(lines) if "e.export();" in l)
    fee_line = next(i for i, l in enumerate(lines) if "p.fee(10);" in l)
    responses = {
        ("textDocument/definition", "Wire.java", load_line, lines[load_line].index("load")):
            loc(root, "app/src/main/java/app/Wire.java", 3, lines[3].index("load"), 0),
        ("textDocument/hover", "Wire.java", load_line, lines[load_line].index("load")):
            hover("String app.Wire.Repository.load()"),
        ("textDocument/definition", "Wire.java", export_line, lines[export_line].index("export")):
            loc(root, "app/src/main/java/app/Wire.java", 4, lines[4].index("export"), 0),
        ("textDocument/hover", "Wire.java", export_line, lines[export_line].index("export")):
            hover("String app.Wire.Exporter.export()"),
        ("textDocument/definition", "Wire.java", fee_line, lines[fee_line].index("fee")):
            loc(root, "app/src/main/java/app/Wire.java", 5, lines[5].index("fee"), 0),
        ("textDocument/hover", "Wire.java", fee_line, lines[fee_line].index("fee")):
            hover("int app.Wire.Policy.fee(int a)"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))

    load = site_by(res, "load", "invocation")
    assert load["status"] == "resolved", load
    assert load["target"]["symbol"] == "app.Wire$SqlRepository.load()"
    assert load["static_target"]["symbol"] == "app.Wire$Repository.load()"
    assert load["resolved_via"] == "iface-impl"

    export = site_by(res, "export", "invocation")
    assert export["target"]["symbol"] == "app.Wire$Exporter.export()"   # 2 impls: refuse
    assert export["resolved_via"] == "lsp"
    assert export["static_target"] is None

    fee = site_by(res, "fee", "invocation")
    assert fee["target"]["symbol"] == "app.Wire$Policy.fee(int)"        # default method
    assert fee["resolved_via"] == "lsp"


def test_interface_upgrade_refused_when_simple_name_is_ambiguous(tmp_path):
    a = """package one;

public interface Repository { String load(); }
"""
    b = """package two;

public interface Repository { String load(); }

class SqlRepository implements Repository {
    public String load() { return "x"; }
}

class Caller {
    void run(Repository r) {
        r.load();
    }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/one/Repository.java": a,
                               "app/src/main/java/two/Repository.java": b})
    lines = b.splitlines()
    line = next(i for i, l in enumerate(lines) if "r.load();" in l)
    responses = {
        ("textDocument/definition", "two/Repository.java", line, lines[line].index("load")):
            loc(root, "app/src/main/java/two/Repository.java", 2,
                lines[2].index("load"), 0),
        ("textDocument/hover", "two/Repository.java", line, lines[line].index("load")):
            hover("String two.Repository.load()"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    s = site_by(res, "load", "invocation")
    assert s["status"] == "resolved"
    assert s["target"]["symbol"] == "two.Repository.load()"     # NOT upgraded
    assert s["resolved_via"] == "lsp"


# ---------------------------------------------------------------------
# DI registry
# ---------------------------------------------------------------------


def _di_source(container_type="ServiceCollection", registrations=None):
    registrations = registrations or [
        "        services.addSingleton(Notifier.class, MailNotifier.class);",
        "        services.addSingleton(Notifier.class, SmsNotifier.class);",
    ]
    body = "\n".join(registrations)
    return f"""package app;

public class Boot {{
    public interface Notifier {{ void send(String m); }}
    public static class MailNotifier implements Notifier {{
        public void send(String m) {{ }}
    }}
    public static class SmsNotifier implements Notifier {{
        public void send(String m) {{ }}
    }}
    public static class {container_type} {{
        public {container_type} addSingleton(Class<?> service, Class<?> impl) {{ return this; }}
        public {container_type} addSingleton(Class<?> service, java.util.function.Supplier<?> f) {{ return this; }}
    }}

    public void build({container_type} services) {{
{body}
    }}

    public void use(Notifier n) {{
        n.send("hi");
    }}
}}
"""


def _di_responses(root, src, container_type="ServiceCollection"):
    path = "app/src/main/java/app/Boot.java"
    lines = src.splitlines()
    idx = {name: next(i for i, l in enumerate(lines) if marker in l)
           for name, marker in (
               ("iface", "interface Notifier"),
               ("mail", "class MailNotifier"),
               ("sms", "class SmsNotifier"),
               ("add", f"public {container_type} addSingleton(Class<?> service, Class<?> impl)"),
               ("send", 'n.send("hi")'))}
    responses = {
        ("textDocument/definition", "Boot.java", idx["send"], lines[idx["send"]].index("send")):
            loc(root, path, idx["iface"], lines[idx["iface"]].index("send"), 0),
        ("textDocument/hover", "Boot.java", idx["send"], lines[idx["send"]].index("send")):
            hover("void app.Boot.Notifier.send(String m)"),
    }
    for i, line in enumerate(lines):
        if "addSingleton(" not in line or "public " in line:
            continue
        add_col = line.index("addSingleton")
        responses[("textDocument/definition", "Boot.java", i, add_col)] = \
            loc(root, path, idx["add"], lines[idx["add"]].index("addSingleton"), 0)
        responses[("textDocument/hover", "Boot.java", i, add_col)] = hover(
            f"{container_type} app.Boot.{container_type}."
            "addSingleton(Class<?> service, Class<?> impl)")
        for name, key in (("Notifier", "iface"), ("MailNotifier", "mail"),
                          ("SmsNotifier", "sms")):
            token = f"{name}.class"
            if token not in line:
                continue
            col = line.index(token)
            responses[("textDocument/definition", "Boot.java", i, col)] = \
                loc(root, path, idx[key], lines[idx[key]].index(name), 0)
    return responses


def test_di_registry_last_wins_with_static_target(tmp_path):
    src = _di_source()
    root = scaffold(tmp_path, {"app/src/main/java/app/Boot.java": src})
    res = run(root, tmp_path / "out",
              make_factory({"app": _di_responses(root, src)}))
    s = site_by(res, "send", "invocation")
    assert s["status"] == "resolved", s
    assert s["resolved_via"] == "di"
    assert s["target"]["symbol"] == "app.Boot$SmsNotifier.send(String)"   # last wins
    assert s["static_target"]["symbol"] == "app.Boot$Notifier.send(String)"


def test_di_registration_ignored_when_the_container_is_not_the_micro_di_type(tmp_path):
    """HARDENING: a same-named addSingleton on some other type must not
    rewire anything."""
    src = _di_source(container_type="Services")
    root = scaffold(tmp_path, {"app/src/main/java/app/Boot.java": src})
    res = run(root, tmp_path / "out",
              make_factory({"app": _di_responses(root, src, container_type="Services")}))
    s = site_by(res, "send", "invocation")
    assert s["status"] == "resolved", s
    assert s["resolved_via"] == "lsp"
    assert s["target"]["symbol"] == "app.Boot$Notifier.send(String)"
    assert s["static_target"] is None


def test_di_splice_refused_when_the_impl_member_is_abstract(tmp_path):
    """HARDENING: the implementation member must be concrete before the target
    is rewired — an abstract override would be a fabricated call edge."""
    src = _di_source().replace(
        """    public static class SmsNotifier implements Notifier {
        public void send(String m) { }
    }""",
        """    public abstract static class SmsNotifier implements Notifier {
        public abstract void send(String m);
    }""")
    assert "abstract void send" in src
    root = scaffold(tmp_path, {"app/src/main/java/app/Boot.java": src})
    res = run(root, tmp_path / "out",
              make_factory({"app": _di_responses(root, src)}))
    s = site_by(res, "send", "invocation")
    assert s["status"] == "resolved", s
    assert s["resolved_via"] == "lsp"
    assert s["target"]["symbol"] == "app.Boot$Notifier.send(String)"
    assert s["static_target"] is None


def test_di_factory_lambda_registration_is_ignored(tmp_path):
    src = _di_source(registrations=[
        "        services.addSingleton(Notifier.class, () -> new SmsNotifier());",
    ])
    root = scaffold(tmp_path, {"app/src/main/java/app/Boot.java": src})
    res = run(root, tmp_path / "out",
              make_factory({"app": _di_responses(root, src)}))
    assert not [s for s in res["sites"] if s["site_kind"] == "class_literal"]
    s = site_by(res, "send", "invocation")
    assert s["resolved_via"] == "lsp"
    assert s["target"]["symbol"] == "app.Boot$Notifier.send(String)"


# ---------------------------------------------------------------------
# degradation, retries, artifact
# ---------------------------------------------------------------------


def test_duplicate_member_symbols_demoted_and_listed(tmp_path):
    prog = """package tpl;

public class Program {
    public static void helper() { }
    public static void main(String[] args) {
        helper();
    }
}
"""
    root = scaffold(tmp_path, {"app/src/main/java/a/Program.java": prog,
                               "app/src/main/java/b/Program.java": prog})
    lines = prog.splitlines()
    line = 5
    c = lines[line].index("helper")
    responses = {}
    for sub in ("a", "b"):
        responses[("textDocument/definition", f"{sub}/Program.java", line, c)] = \
            loc(root, f"app/src/main/java/{sub}/Program.java", 3,
                lines[3].index("helper"), 0)
        responses[("textDocument/hover", f"{sub}/Program.java", line, c)] = \
            hover("void tpl.Program.helper()")
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    dups = {(d["artifact"], d["symbol"]) for d in res["ambiguous_symbols"]}
    assert ("app", "tpl.Program.helper()") in dups
    for s in res["sites"]:
        if s["token"] == "helper" and s["status"] == "resolved":
            assert s["target"]["file"] is None and s["target"]["line"] is None


def test_definition_error_degrades_the_site_not_the_project(tmp_path):
    root = base_project(tmp_path)
    line = 7
    c = col_of(FLOW_JAVA, line, "add")
    responses = {
        ("textDocument/definition", "Flow.java", line, c):
            R.LspError(-32603, "Internal error"),
    }
    res = run(root, tmp_path / "out", make_factory({"app": responses}))
    assert res["solutions"][0]["status"] == "resolved"
    assert site_by(res, "add", "invocation")["status"] == "unresolved"


def test_project_fallback_on_client_failure(tmp_path):
    root = base_project(tmp_path)
    res = run(root, tmp_path / "out", make_factory({}, fail_projects=("app",)))
    assert len(res["solutions"]) == 1
    sln = res["solutions"][0]
    assert sln["status"] == "fallback"
    assert sln["sln"] == os.path.join("app", "pom.xml")
    assert os.path.join("app", "src", "main", "java", "app", "Flow.java") in sln["files"]
    assert res["sites"] == []
    assert res["declarations"] == []      # no resolved project -> no LSP-grade identity
    assert "jdtls" in R.GUIDANCE_JA and "JDK 21" in R.GUIDANCE_JA
    assert "pom.xml" in R.GUIDANCE_JA and "JAVA_LSP_ENABLED=false" in R.GUIDANCE_JA


def test_ready_timeout_gets_one_serial_retry(tmp_path):
    root = base_project(tmp_path)
    line = 7
    c = col_of(FLOW_JAVA, line, "add")
    responses = {
        ("textDocument/definition", "Flow.java", line, c):
            loc(root, CALC, 3, col_of(CALC_JAVA, 3, "add"), 0),
        ("textDocument/hover", "Flow.java", line, c):
            hover("int app.calc.Calc.add(int x, int y)"),
    }
    attempts = {"n": 0}

    def factory(cmd, cwd, config_sections=None, default_timeout=None, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return StubClient({}, fail_on_start=R.LspTimeout("project not ready within 300s"),
                              **kwargs)
        return StubClient(responses, **kwargs)

    res = run(root, tmp_path / "out", factory)
    assert attempts["n"] == 2
    assert res["solutions"][0]["status"] == "resolved", res["solutions"][0]
    assert site_by(res, "add", "invocation")["status"] == "resolved"


def test_connection_errors_are_not_retried(tmp_path):
    root = base_project(tmp_path)
    attempts = {"n": 0}

    def factory(cmd, cwd, config_sections=None, default_timeout=None, **kwargs):
        attempts["n"] += 1
        return StubClient({}, fail_on_start=R.LspConnectionError("no such binary"), **kwargs)

    res = run(root, tmp_path / "out", factory)
    assert attempts["n"] == 1
    assert res["solutions"][0]["status"] == "fallback"


def test_artifact_v2_written_atomically_with_relative_paths(tmp_path):
    root = base_project(tmp_path)
    out = tmp_path / "out"
    res = run(root, out, make_factory({"app": {}}))
    path = out / "resolution.json"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 2
    assert on_disk["lsp"] == {"enabled": True, "cmd": config.JAVA_LSP_CMD}
    assert on_disk["sites"] == res["sites"]
    assert on_disk["solutions"][0]["sln"] == os.path.join("app", "pom.xml")
    for d in on_disk["declarations"]:
        assert not os.path.isabs(d["file"])
    for s in on_disk["sites"]:
        assert s["enclosing_artifact"] == "app"
    assert not list(out.glob("*.tmp"))


def test_disabled_mode_produces_empty_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JAVA_LSP_ENABLED", False)
    root = base_project(tmp_path)
    res = R.run_resolution(root, [os.path.join(root, FLOW)],
                           str(tmp_path / "out"), client_factory=make_factory({}))
    assert res["lsp"]["enabled"] is False
    assert res["solutions"] == [] and res["sites"] == [] and res["declarations"] == []
    assert res["fallback_files"] == [os.path.join("app", "src", "main", "java",
                                                  "app", "Flow.java")]


# ---------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------


def test_hover_signature_takes_the_first_java_marked_string():
    assert R._hover_signature(HOVERS["plain_method"]["response"]) == \
        "String jp.iryo.spike.app.App.localPlain(String value)"
    assert R._hover_signature(HOVERS["constructor"]["response"]) == "jp.iryo.spike.lib.Rec"
    assert R._hover_signature({"contents": ["only a string"]}) == "only a string"
    assert R._hover_signature(None) == ""


def test_hover_signature_parsing_and_param_normalization():
    parsed = R._parse_hover_signature(
        "long jp.iryo.spike.lib.Calculations.calculate(long a, long b)")
    assert parsed == ("Calculations", "calculate", ["long a", "long b"])
    assert R._parse_hover_signature("jp.iryo.spike.lib.Rec") is None   # ctor hover
    assert R._hover_type_fqn("jp.iryo.spike.lib.Rec") == "jp.iryo.spike.lib.Rec"
    assert R._hover_type_fqn("int jp.iryo.spike.lib.Rec.amount()") == ""

    assert R._norm_hover_param("java.lang.String value") == "String"
    assert R._norm_hover_param("int... values") == "int..."
    assert R._norm_hover_param("Map<String, Integer> m") == "Map<String, Integer>"
    assert R._norm_hover_param("java.util.Map<java.lang.String,Integer> m") == \
        "Map<String, Integer>"
    assert R._norm_hover_param("String[] names") == "String[]"
    assert R._norm_hover_param("final List<? extends Number> xs") == "List<? extends Number>"


def test_symbol_helpers_speak_java_grammar():
    assert R._symbol_member_name("iryo.billing.FeeCalculator.calculate(int, int)") == "calculate"
    assert R._symbol_member_name("iryo.kernel.Logger.<init>(String)") == "<init>"
    assert R._symbol_member_name("iryo.data.ChartArchive$Compactor.start()") == "start"
    assert R._symbol_member_name("iryo.data.Registry.create<T>()") == "create"
    assert R._symbol_param_count("iryo.billing.PointMath.toYen(int)") == 1
    assert R._symbol_param_count("iryo.billing.Calc.calculate(int...)") == 1
    assert R._symbol_param_count("iryo.kernel.Logger.<init>()") == 0
    assert R._simple_type_name("iryo.data.ChartArchive$Compactor") == "Compactor"
    assert R._simple_type_name("iryo.data.ChartCache<K,V>") == "ChartCache<K,V>"
    assert R._symbol_container("iryo.kernel.ServiceCollection.addSingleton(Class<?>)") == \
        "iryo.kernel.ServiceCollection"
    assert R._symbol_container("iryo.kernel.Logger.<init>(String)") == "iryo.kernel.Logger"


def test_no_csharp_vocabulary_remains():
    text = open(R.__file__, encoding="utf-8").read().lower()
    for dead in ("csharp", "roslyn", "metadataassource", "extension_method"):
        assert dead not in text, dead


def test_maven_settings_knob_flows_into_init_options(monkeypatch):
    """JAVA_LSP_MAVEN_SETTINGS points m2e at a repo-local settings.xml; unset → None."""
    from graph_generator import config as cfg
    monkeypatch.setattr(cfg, "JAVA_LSP_MAVEN_SETTINGS", "/repo/settings.xml")
    opts = R._init_options()
    assert opts["settings"]["java"]["configuration"]["maven"]["userSettings"] == "/repo/settings.xml"
    monkeypatch.setattr(cfg, "JAVA_LSP_MAVEN_SETTINGS", "")
    assert R._init_options()["settings"]["java"]["configuration"]["maven"]["userSettings"] is None
