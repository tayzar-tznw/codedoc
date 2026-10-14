"""The variant defaults of codedoc_go_leiden (leiden spec §7): its own Spanner
database, Cloud Run service and image, the Leiden topic layer of the agent
and the skill, the topic settings, and the pinned Leiden dependencies."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from graph_query_agent import agent
from tests import go_schema_spec as go

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _text(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _defaults(module: str, names: list[str]) -> dict:
    """The module's values in a fresh interpreter with no .env read and the
    Spanner and topic keys unset."""
    code = ("import json, dotenv; dotenv.load_dotenv = lambda *a, **k: False; "
            f"import {module} as m; print(json.dumps({{n: getattr(m, n) for n in {names!r}}}))")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SPANNER_", "TOPIC_", "SUBTOPIC_"))}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout)


def test_pipeline_defaults():
    assert _defaults("graph_generator.config", ["SPANNER_DATABASE", "TOPIC_MIN", "TOPIC_MAX",
                                                "SUBTOPIC_MIN_ATOMS", "TOPIC_PROFILE",
                                                "TOPIC_SEEDS"]) == {
        "SPANNER_DATABASE": "codedoc-go-leiden", "TOPIC_MIN": "5", "TOPIC_MAX": "25",
        "SUBTOPIC_MIN_ATOMS": "12", "TOPIC_PROFILE": "strict", "TOPIC_SEEDS": "42,43,44,45,46"}


def test_agent_defaults():
    assert _defaults("graph_query_agent.config", ["SPANNER_DATABASE"]) == {
        "SPANNER_DATABASE": "codedoc-go-leiden"}


def test_the_agent_uses_the_leiden_topic_layer():
    assert agent.TOPIC_SOURCE == "leiden"
    assert agent.GQL_COOKBOOK == agent.cookbook("leiden")
    assert agent.GRAPH_AGENT_INSTRUCTION == agent.build_instruction(agent.GRAPH_NAME, "leiden")
    for table in ("ClassBelongsToTopic", "TopicDependsOn", "TopicContainsTopic"):
        assert table in agent.GRAPH_AGENT_INSTRUCTION


def test_deploy_defaults():
    script = _text("mcp_server/deploy.sh")
    assert 'SERVICE_NAME="${SERVICE_NAME:-codedoc-go-leiden-mcp}"' in script
    assert 'SPANNER_DATABASE="${SPANNER_DATABASE:-codedoc-go-leiden}"' in script
    assert '/codedoc-go-leiden-mcp"' in script
    assert not re.search(r"codedoc-go(-mcp)?[\"} ]", script)
    build = _text("mcp_server/cloudbuild.yaml")
    assert "codedoc-go-leiden-mcp" in build and "codedoc-go-mcp" not in build


def test_deploy_script_parses():
    subprocess.run(["bash", "-n", os.path.join(ROOT, "mcp_server", "deploy.sh")], check=True)


def test_env_example_defaults():
    example = _text(".env.example")
    assert "SPANNER_DATABASE=codedoc-go-leiden\n" in example
    for line in ("TOPIC_MIN=5", "TOPIC_MAX=25", "SUBTOPIC_MIN_ATOMS=12", "TOPIC_PROFILE=strict",
                 "TOPIC_SEEDS=42,43,44,45,46"):
        assert f"\n{line}\n" in example


def test_the_skill_describes_the_leiden_topics():
    skill = _text(".claude/skills/codedoc-query/SKILL.md")
    assert "`codedoc_go_leiden`" in skill and "**Edges** (17)" in skill
    edges = re.search(r"\*\*Edges\*\* \(17\): (.*?)\.\s*<!--", skill, re.S).group(1)
    assert sorted(re.findall(r"`(\w+)`", edges)) == sorted(go.LEIDEN_EDGE_SPEC)
    assert "structural topics" in skill and "functional topics" not in skill


def test_requirements_pin_the_leiden_dependencies():
    pins = dict(line.split("==") for line in _text("requirements.txt").splitlines()
                if "==" in line and not line.startswith("#"))
    assert {k: pins[k] for k in ("graspologic-native", "networkx", "numpy", "scipy")} == {
        "graspologic-native": "1.3.1", "networkx": "3.7", "numpy": "2.5.4", "scipy": "1.18.1"}


def test_the_pytest_plugin_is_loaded():
    assert "-p tests.leiden_plugin" in _text("pytest.ini")


def test_env_example_lists_every_setting():
    keys = set(re.findall(r'_env(?:_int|_opt|_flag)?\("([A-Z0-9_]+)"', _text("graph_generator/config.py")))
    listed = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", _text(".env.example"), flags=re.M))
    assert {"TOPIC_MIN", "TOPIC_MAX", "SUBTOPIC_MIN_ATOMS", "TOPIC_PROFILE", "TOPIC_SEEDS"} <= keys
    assert keys <= listed, sorted(keys - listed)


def test_gql_lint_passes_on_the_instruction_and_the_skill_with_the_leiden_schema(capsys):
    from scripts import gql_lint
    assert gql_lint.main(["--no-docs"]) == 0
    assert "gql_lint: OK" in capsys.readouterr().out


def test_topics_phase_uses_no_wall_clock():
    assert "time.time()" not in _text("graph_generator/topics_phase.py")


def test_the_suite_is_hermetic_for_the_leiden_settings():
    import conftest
    from graph_generator import config
    assert os.environ["CODEDOC_DOTENV"] == "0"
    assert conftest.is_config_env("TOPIC_MIN") and conftest.is_config_env("SUBTOPIC_MIN_ATOMS")
    assert [name for name in os.environ if conftest.is_config_env(name)] == []
    assert {k: getattr(config, k) for k in ("SPANNER_DATABASE", "TOPIC_MIN", "TOPIC_MAX", "SUBTOPIC_MIN_ATOMS",
                                            "TOPIC_PROFILE", "TOPIC_SEEDS")} == {
        "SPANNER_DATABASE": "codedoc-go-leiden", "TOPIC_MIN": "5", "TOPIC_MAX": "25", "SUBTOPIC_MIN_ATOMS": "12",
        "TOPIC_PROFILE": "strict", "TOPIC_SEEDS": "42,43,44,45,46"}


def test_mcp_readme_states_this_variants_defaults():
    text = _text("mcp_server/README.md")
    for fact in ("codedoc_go_leiden", "codedoc-go-leiden-mcp",
                 "`codedoc-instance` / `codedoc-go-leiden` / `code_graph_a`",
                 "`graph_query_agent/config.py`", "there is no `graph_query_agent/.env`"):
        assert fact in text, fact
    assert "codedoc-db" not in text and "development-459201" not in text
    assert not re.search(r"codedoc-mcp(?!-sa)", text)
    # codedoc_go's names appear only as the sibling's defaults
    assert "`codedoc-instance` / `codedoc-go` / `code_graph_a`" not in text
