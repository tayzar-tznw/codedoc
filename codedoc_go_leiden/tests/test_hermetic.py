"""The suite is hermetic: a root .env or exported settings never change a test.

conftest.py sets CODEDOC_DOTENV=0 before any project module is imported (so
graph_generator/config.py, graph_query_agent/config.py, webapp/main.py and
mcp_server/runner.py skip the root .env), removes the configuration variables
a user may have exported, and resets graph_generator.config to its defaults
before every test.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

import conftest
from graph_generator import config

DEFAULTS = {"GCP_PROJECT": None, "SPANNER_INSTANCE": "codedoc-instance", "SPANNER_DATABASE": "codedoc-go",
            "GRAPH_NAME": "code_graph_a", "ID_PREFIX": "a", "OUTPUT_DIR": "output_docs_pipeline",
            "GO_TOOLCHAIN": None, "GO_MODULES_DOWNLOAD": False, "PEER_REPOS": None}


def test_no_configuration_variable_reaches_a_test():
    assert os.environ["CODEDOC_DOTENV"] == "0"
    assert [name for name in os.environ if conftest.is_config_env(name)] == []
    assert {k: getattr(config, k) for k in DEFAULTS} == DEFAULTS


@pytest.mark.parametrize("name, config_env", [
    ("GOOGLE_CLOUD_PROJECT", True), ("GOOGLE_GENAI_USE_VERTEXAI", True), ("SPANNER_INSTANCE", True),
    ("GRAPH_NAME", True), ("ID_PREFIX", True), ("OUTPUT_DIR", True), ("GO_TOOLCHAIN", True),
    ("GO_EXTRA_BUILDS", True), ("WIKI_SUMMARY_CACHE", True), ("PEER_REPOS", True),
    ("EMBED_CONCURRENCY", True), ("GEMINI_MODEL", True), ("WEBAPP_REPO", True),
    ("GOPATH", False), ("GOFLAGS", False), ("HOME", False), ("CODEDOC_TEST_GO", False),
    ("CODEDOC_DOTENV", False),
])
def test_which_variables_count_as_configuration(name, config_env):
    assert conftest.is_config_env(name) is config_env


def test_every_key_config_reads_is_cleared():
    # a new setting must not slip past the clearing (the leiden fork adds TOPIC_*)
    from scripts import doccheck
    with open(os.path.join(conftest.REPO_ROOT, "graph_generator", "config.py"), encoding="utf-8") as f:
        keys = doccheck.config_keys(f.read())
    assert keys and [k for k in sorted(keys) if not conftest.is_config_env(k)] == []


def test_leaks_part_1_assign_config_and_export_settings():
    config.SPANNER_INSTANCE = "leaked"                  # no monkeypatch, on purpose
    config.SOURCE_EXTENSIONS.add(".leak")
    os.environ["GRAPH_NAME"] = "leaked"


def test_leaks_part_2_start_from_the_defaults_again():
    assert config.SPANNER_INSTANCE == "codedoc-instance" and config.SOURCE_EXTENSIONS == {".go"}
    assert "GRAPH_NAME" not in os.environ


@pytest.mark.parametrize("switch, expected", [("0", "codedoc-instance"), (None, "from-dotenv")])
def test_config_reads_a_dotenv_unless_the_switch_is_off(tmp_path, monkeypatch, switch, expected):
    import dotenv
    env = tmp_path / ".env"
    env.write_text("SPANNER_INSTANCE=from-dotenv\n", encoding="utf-8")
    real = dotenv.load_dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: real(env))   # config.py: load_dotenv()
    monkeypatch.setenv("SPANNER_INSTANCE", "x")
    monkeypatch.delenv("SPANNER_INSTANCE")              # undo removes what the .env sets
    if switch is None:
        monkeypatch.delenv("CODEDOC_DOTENV")
    else:
        monkeypatch.setenv("CODEDOC_DOTENV", switch)
    try:
        assert importlib.reload(config).SPANNER_INSTANCE == expected
    finally:
        monkeypatch.undo()
        importlib.reload(config)
    assert config.SPANNER_INSTANCE == "codedoc-instance"


def _fresh_import(module: str, monkeypatch) -> None:
    """Import `module` (and graph_query_agent.config) anew; monkeypatch puts the
    old modules back in sys.modules and on their packages afterwards."""
    for name in dict.fromkeys((module, "graph_query_agent.config")):
        package, _, attr = name.rpartition(".")
        if package in sys.modules and hasattr(sys.modules[package], attr):
            monkeypatch.setattr(sys.modules[package], attr, getattr(sys.modules[package], attr))
        monkeypatch.delitem(sys.modules, name, raising=False)
    importlib.import_module(module)


@pytest.mark.parametrize("module", ["graph_query_agent.config", "mcp_server.runner", "webapp.main"])
def test_entry_points_skip_the_dotenv_under_the_switch(module, monkeypatch):
    import dotenv
    seen = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda path=None, **kw: seen.append(path))
    _fresh_import(module, monkeypatch)
    assert seen == []
