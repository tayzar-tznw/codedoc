"""Surfaces: the query agent reads the root .env settings with this variant's
defaults; the MCP server, webapp and deploy files are present."""

import importlib
import os
import subprocess
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def agent_module(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for key in ("GEMINI_MODEL", "SPANNER_INSTANCE", "SPANNER_DATABASE", "GRAPH_NAME",
                "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    monkeypatch.setenv("GRAPH_NAME", "code_graph_t")
    import graph_query_agent.agent as agent
    import graph_query_agent.config as agent_config
    importlib.reload(agent_config)
    yield importlib.reload(agent)
    monkeypatch.undo()
    importlib.reload(agent_config)
    importlib.reload(agent)


def test_agent_reads_model_database_and_graph_from_env(agent_module):
    assert agent_module.MODEL == "gemini-test-model"
    assert agent_module.SPANNER_DATABASE == "codedoc-go"
    assert agent_module.graph_agent.model == "gemini-test-model"
    assert "GRAPH code_graph_t" in agent_module.GRAPH_AGENT_INSTRUCTION
    assert "code_graph_a" not in agent_module.run_gql_query.__doc__
    assert agent_module.PROJECT_ID is None  # no default project


@pytest.mark.parametrize("resolved,shown", [
    ("adc-project", "Spanner project: adc-project"),
    (None, "Spanner project: ADC / gcloud default (unresolved)"),
])
def test_agent_prints_the_project_its_client_uses(agent_module, monkeypatch, capsys,
                                                  resolved, shown):
    made = {}

    def fake_client(**kw):
        made.update(kw)
        return types.SimpleNamespace(
            project=resolved,
            instance=lambda iid: types.SimpleNamespace(database=lambda name: "db-handle"))

    monkeypatch.setattr(agent_module.spanner, "Client", fake_client)
    monkeypatch.setattr(agent_module, "_db", None)
    assert agent_module._get_database() == "db-handle"
    assert made["project"] is None  # the client library resolves it
    assert shown in capsys.readouterr().err


@pytest.mark.parametrize("rel", [
    "mcp_server/server.py", "mcp_server/runner.py", "mcp_server/sessions.py",
    "mcp_server/__main__.py", "mcp_server/Dockerfile", "mcp_server/cloudbuild.yaml",
    "mcp_server/deploy.sh", "mcp_server/.gcloudignore", "mcp_server/requirements.txt",
    "mcp_server/README.md", "webapp/main.py", "webapp/Dockerfile", "webapp/requirements.txt",
    "webapp/frontend/package.json", "Dockerfile", ".dockerignore", ".gcloudignore",
    "sample_prompt.txt", ".mcp.json", ".env.example",
])
def test_surface_file_present(rel):
    assert os.path.isfile(os.path.join(ROOT, rel))


def _git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not available")


@pytest.mark.parametrize("rel", ["graph_query_agent/.adk/", "graph_query_agent/.adk/session.db",
                                 "graph_query_agent/.env"])
def test_agent_state_and_a_nested_env_stay_out_of_git(rel):
    # `adk run graph_query_agent` keeps its session state in .adk/, and a nested .env
    # would shadow the root one: both may exist locally, never in git. (The trailing
    # slash lets the directory pattern match when .adk/ does not exist yet.)
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a git work tree")
    assert _git("check-ignore", "-q", rel).returncode == 0, f"{rel} is not git-ignored"
    assert _git("ls-files", "--", rel.rstrip("/")).stdout == "", f"{rel} is tracked"


def test_deploy_script_is_executable():
    assert os.access(os.path.join(ROOT, "mcp_server", "deploy.sh"), os.X_OK)
