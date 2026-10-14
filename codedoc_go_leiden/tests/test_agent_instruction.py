"""graph_query_agent: the instruction matches the Go schema (spec §6.5), the cookbook
queries fit it, the topic layer switches in one place for codedoc_go_leiden,
settings come from the root .env, and the tool shapes its results."""
from __future__ import annotations

import asyncio
import importlib
import re
import types
from pathlib import Path

import pytest

from graph_generator import setup_spanner_graph as real_schema
from graph_query_agent import agent
from graph_query_agent import config as agent_config
from scripts.gql_lint import lint_gql
from tests import go_schema_spec as go

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION = agent.GRAPH_AGENT_INSTRUCTION
TOPIC_SOURCES = ("llm", "leiden")
# The real schema module carries the Go tables once WP-B3 lands; until then the
# fixture copied from spec §6.1/§6.2 stands in for it.
GO_SCHEMA_LANDED = "Packages" in real_schema.NODE_SPEC
needs_b3 = pytest.mark.skipif(not GO_SCHEMA_LANDED,
                              reason="WP-B3 has not landed the Go NODE_SPEC / EDGE_SPEC yet")

REQUIRED_RECIPES = {
    # spec §6.5 cookbook item -> (recipe key, edge table it must use)
    "who calls X": ("callers", "MethodCalls"),
    "what X calls": ("callees", "MethodCalls"),
    "interface candidates": ("possible_callees", "PossiblyCalls"),
    "implementers of an interface": ("implementers", "ClassImplements"),
    "embedding tree": ("embeds", "ClassEmbeds"),
    "package dependencies": ("package_imports", "PackageImports"),
    "calls between two modules": ("module_calls", "MethodCalls"),
    "calls between repos": ("cross_repo_calls", "CrossRepoCalls"),
    "imports between repos": ("cross_repo_imports", "CrossRepoImports"),
    "types used across repos": ("cross_repo_refs", "CrossRepoRef"),
}
LEIDEN_RECIPES = {
    # leiden spec §4 topic cookbook
    "members of a topic": ("topic_members", "ClassBelongsToTopic"),
    "topic of a class": ("class_topic", "ClassBelongsToTopic"),
    "dependencies between topics": ("topic_dependencies", "TopicDependsOn"),
    "the topic tree": ("topic_tree", "TopicContainsTopic"),
    "types per topic": ("topic_type_counts", "ClassBelongsToTopic"),
}


def _fixture(topics: str) -> tuple[dict, dict]:
    if topics == "llm":
        return go.NODE_SPEC, go.EDGE_SPEC
    return go.LEIDEN_NODE_SPEC, go.LEIDEN_EDGE_SPEC


def _schema(topics: str) -> tuple[dict, dict]:
    """The schema the instruction for `topics` must describe: the real module
    once it carries this variant's tables, else the spec fixture."""
    if GO_SCHEMA_LANDED and topics == agent.TOPIC_SOURCE:
        return real_schema.NODE_SPEC, real_schema.EDGE_SPEC
    return _fixture(topics)


def _listed_tables(text: str) -> dict[str, list[str]]:
    return {m.group(1): [c.strip() for c in m.group(2).split(",")]
            for m in re.finditer(r"^- (\w+) \(([^)]*)\)", text, re.MULTILINE)}


def _columns(spec: dict) -> dict[str, list[str]]:
    return {table: [col for col, _type in entry[1]] for table, entry in spec.items()}


def test_codedoc_go_uses_the_llm_topics():
    assert agent.TOPIC_SOURCE == "llm"
    assert agent.GQL_COOKBOOK == agent.cookbook("llm")
    assert INSTRUCTION == agent.build_instruction(agent.GRAPH_NAME, "llm")


@pytest.mark.parametrize("topics", TOPIC_SOURCES)
def test_every_table_is_listed_with_exactly_its_columns(topics):
    node, edge = _schema(topics)
    assert _listed_tables(agent.build_instruction("g", topics)) == _columns({**node, **edge})


@pytest.mark.parametrize("topics", TOPIC_SOURCES)
def test_edges_are_listed_with_their_direction(topics):
    text = agent.build_instruction("g", topics)
    _node, edge = _schema(topics)
    for table, (_pk, _cols, (_s, src), (_d, dst)) in edge.items():
        assert re.search(rf"^- {table} \([^)]*\): {src} → {dst}", text, re.MULTILINE), table


@needs_b3
def test_fixture_matches_the_real_schema():
    node, edge = _fixture(agent.TOPIC_SOURCE)
    assert {t: set(c) for t, c in _columns(real_schema.NODE_SPEC).items()} == \
        {t: set(c) for t, c in _columns(node).items()}
    assert {t: set(c) for t, c in _columns(real_schema.EDGE_SPEC).items()} == \
        {t: set(c) for t, c in _columns(edge).items()}
    assert {t: (e[2][1], e[3][1]) for t, e in real_schema.EDGE_SPEC.items()} == \
        {t: (e[2][1], e[3][1]) for t, e in edge.items()}


def test_go_model_vocabularies_match_the_spec():
    from graph_generator import go_model
    published = [name for name in go.SPEC_VOCABULARY if hasattr(go_model, name)]
    if not published:
        pytest.skip("WP-D has not published the vocabularies in go_model.py yet")
    assert published == list(go.SPEC_VOCABULARY)
    for name, spec in go.SPEC_VOCABULARY.items():
        assert set(getattr(go_model, name)) == set(spec), name


@pytest.mark.parametrize("term", [
    *go.STATUSES, *go.UNRESOLVED_REASONS, *go.POSSIBLY_REASONS,
    *(v for v in go.VIA if v != "none"), *go.SITE_KINDS, *go.MEMBER_KINDS, *go.CLASS_KINDS,
    *go.ORIGINS, *go.IMPORT_KINDS, *go.CROSS_REF_KINDS,
    "<package>", "<init>", "init#1", "init#2", "_#1", "F.func1", "F.func1.func1",
    "<init>.func1", "T.M", "I.M", "receiver", "type_params", "entry_point", "open_world",
    "pointer_receiver", "build_included", "build_constraint", "generated", "has_body",
    "POSSIBLY_CALLS_MAX_CANDIDATES", "go.work", "replace", "--download-modules",
    "func(){…}()", "`_test`",
])
def test_instruction_explains_the_go_representation(term):
    assert term in INSTRUCTION


@pytest.mark.parametrize("vocabulary", ["statuses", "unresolved", "possibly", "via"])
def test_each_term_is_explained_on_its_own_line(vocabulary):
    terms = {"statuses": go.STATUSES, "unresolved": go.UNRESOLVED_REASONS,
             "possibly": go.POSSIBLY_REASONS,
             "via": tuple(v for v in go.VIA if v != "none")}[vocabulary]
    for term in terms:
        assert re.search(rf"^  - {re.escape(term)}: \S", INSTRUCTION, re.MULTILINE), term


def test_types_and_modules_are_not_confused():
    assert "`Classes` は Go の型" in INSTRUCTION
    for topics in TOPIC_SOURCES:
        text = agent.build_instruction("g", topics)
        assert "Go のモジュール(go.mod)ではありません" in text, topics
        assert "`Files.module`" in text and "`Packages`" in text


def test_interface_calls_are_explained_as_open_world():
    text = INSTRUCTION
    assert "実装が 1 つしかなくても MethodCalls にはなりません" in text
    assert "`open_world` = true" in text
    assert "インターフェースがリポジトリの外" in text


def _bullet(text: str, head: str) -> str:
    return next(line for line in text.splitlines() if line.strip().startswith(head))


def test_error_of_the_predeclared_error_interface_is_builtin():
    # ruling 2026-10-13 (gohelper resolve sites.go): no candidates, so no PossiblyCalls
    text = INSTRUCTION
    assert "`error`、`io.Writer`" not in text
    assert "error" in _bullet(text, "- builtin:") and "Error" in _bullet(text, "- builtin:")


def test_calls_through_unnamed_interfaces_are_ambiguous_or_dynamic():
    # spec R2: interface literals, function-local interfaces and inline constraints have no node
    text = INSTRUCTION
    ambiguous = _bullet(text, "- ambiguous:")
    assert "'variant'" in ambiguous and "'implementation'" in ambiguous
    assert "名前の無いインターフェース" in _bullet(text, "- dynamic:")
    assert "インラインの制約" in text and "関数の中で宣言したインターフェース" in text


def test_per_file_names_are_disambiguated_by_path():
    assert re.search(r"`<package>`、`<init>`、`init#k`、`_#k`.*`Files.path`", INSTRUCTION)


BOOL_COLUMNS = {"Files": ("generated", "build_included"), "Methods": ("has_body",),
                "ClassEmbeds": ("pointer",), "ClassImplements": ("pointer_receiver",),
                "PossiblyCalls": ("open_world",)}


def test_bool_columns_are_named_with_their_comparison():
    line = next(l for l in INSTRUCTION.splitlines() if "は BOOL です" in l)
    for cols in BOOL_COLUMNS.values():
        for col in cols:
            assert f"`{col}`" in line
    assert "= true" in line


@needs_b3
def test_bool_columns_are_bool_in_the_real_schema():
    # The instruction tells the agent to compare these with true / false.
    spec = {**real_schema.NODE_SPEC, **real_schema.EDGE_SPEC}
    for table, cols in BOOL_COLUMNS.items():
        types = dict(spec[table][1])
        assert {col: types[col] for col in cols} == {col: "bool" for col in cols}, table


@pytest.mark.parametrize("topics", TOPIC_SOURCES)
def test_cookbook_queries_are_in_the_instruction_and_fit_the_schema(topics):
    node, edge = _schema(topics)
    text = agent.build_instruction("code_graph_t", topics)
    for key, (title, query) in agent.cookbook(topics).items():
        block = f"### {title}:\nGRAPH code_graph_t\n{query}"
        assert block in text, key
        assert lint_gql(f"GRAPH code_graph_t\n{query}", node, edge) == [], key


@pytest.mark.parametrize("question, recipe", sorted(REQUIRED_RECIPES.items()))
def test_cookbook_covers_the_spec_questions(question, recipe):
    key, table = recipe
    for topics in TOPIC_SOURCES:
        assert f":{table}]" in agent.cookbook(topics)[key][1], (question, topics)


@pytest.mark.parametrize("question, recipe", sorted(LEIDEN_RECIPES.items()))
def test_leiden_cookbook_covers_the_topic_questions(question, recipe):
    key, table = recipe
    assert f":{table}]" in agent.cookbook("leiden")[key][1], question
    assert key not in agent.cookbook("llm")


def test_topic_layer_is_the_only_difference_between_the_variants():
    llm, leiden = agent.cookbook("llm"), agent.cookbook("leiden")
    shared = set(llm) & set(leiden)
    assert {k for k in shared if llm[k] != leiden[k]} <= {"topic_files"}
    assert set(llm) - shared == set()
    assert "ClassBelongsToTopic" not in agent.build_instruction("g", "llm")
    with pytest.raises(ValueError, match="unknown topic source 'louvain'"):
        agent.build_instruction("g", "louvain")


@pytest.mark.parametrize("term", [*go.LEIDEN_CLASS_ROLES, *go.LEIDEN_FILE_ROLES,
                                  *go.LEIDEN_LABEL_SOURCES, "level", "「未分類」", "structure"])
def test_leiden_instruction_explains_the_topic_columns(term):
    assert term in agent.build_instruction("g", "leiden")


def _table_line(text: str, table: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"- {table} ("))


def test_leiden_class_membership_is_per_topic_level():
    # leiden-core topic_rows: a row for the level-1 topic and, when it is split, one subtopic
    text = agent.build_instruction("g", "leiden")
    line = _table_line(text, "ClassBelongsToTopic")
    assert "1 つの型は 1 つのトピックに属する" not in text
    assert "レベル 1 のトピック" in line and "サブトピック" in line
    assert "`t.level = 1`" in text                      # the note on counting each type once


def test_topic_dependency_call_count_counts_method_pairs():
    # leiden-core topic_dependencies: rows from calls, embeds and implements; call_count may be 0
    line = _table_line(agent.build_instruction("g", "leiden"), "TopicDependsOn")
    assert "呼び出しの数" not in line and "メソッドの組" in line
    assert "埋め込み" in line and "実装" in line and "0" in line


def test_file_topics_include_the_unassigned_topic():
    line = _table_line(agent.build_instruction("g", "leiden"), "FileBelongsToTopic")
    assert "「未分類」を含む" in line and "クロージャを除く" in line


def test_leiden_recipes_over_class_membership_pick_a_level():
    book = agent.cookbook("leiden")
    assert "ORDER BY t.level" in book["class_topic"][1]
    assert "t.level = 1" in book["topic_type_counts"][1]


def test_instruction_is_a_provider_so_adk_never_templates_it():
    # A string instruction goes through ADK's {state} templating; the cookbook
    # holds GQL property maps and quantifiers in braces, and qualnames such as
    # `init#1` or `<init>.func1` must reach the model verbatim.
    assert "{fqmn: '" in INSTRUCTION and "->{1,3}" in INSTRUCTION
    assert callable(agent.graph_agent.instruction)
    assert asyncio.run(agent.graph_agent.canonical_instruction(None)) == (INSTRUCTION, True)


def test_root_agent_delegates_to_graph_agent():
    assert agent.root_agent.sub_agents == [agent.graph_agent]
    assert agent.graph_agent.tools == [agent.run_gql_query]
    assert agent.graph_agent.model == agent_config.MODEL
    for text in (agent.graph_agent.description, agent.root_agent.instruction,
                 agent.run_gql_query.__doc__, INSTRUCTION):
        assert "Python" not in text and "継承" not in text


@pytest.fixture
def reload_config(monkeypatch):
    import dotenv
    seen = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda path=None, **kw: seen.append(path))
    monkeypatch.delenv("CODEDOC_DOTENV")      # the suite's switch would skip the (faked) load
    for key in ("GEMINI_MODEL", "SPANNER_INSTANCE", "SPANNER_DATABASE", "GRAPH_NAME",
                "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    yield lambda: (importlib.reload(agent_config), seen)
    monkeypatch.undo()
    importlib.reload(agent_config)


def test_config_reads_only_the_root_dotenv_and_defaults(reload_config):
    cfg, seen = reload_config()
    assert seen == [ROOT / ".env"]
    assert (cfg.MODEL, cfg.SPANNER_INSTANCE, cfg.SPANNER_DATABASE, cfg.GRAPH_NAME) == (
        "gemini-3.5-flash", "codedoc-instance", "codedoc-go", "code_graph_a")
    assert cfg.PROJECT_ID is None


@pytest.mark.parametrize("value, expected", [("my-proj", "my-proj"), ("", None), ("  ", None)])
def test_config_env_overrides(reload_config, monkeypatch, value, expected):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-x")
    monkeypatch.setenv("SPANNER_DATABASE", "other-db")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", value)
    cfg, _seen = reload_config()
    assert (cfg.MODEL, cfg.SPANNER_DATABASE, cfg.PROJECT_ID) == ("gemini-x", "other-db", expected)


@pytest.mark.parametrize("project, shown", [
    (None, "project: ADC / gcloud default (GOOGLE_CLOUD_PROJECT is not set)"),
    ("my-proj", "project: my-proj")])
def test_settings_line_names_the_project_or_the_adc_default(monkeypatch, project, shown):
    monkeypatch.setattr(agent, "PROJECT_ID", project)
    line = agent._settings_line()
    assert line.startswith("[graph_query_agent] graph ") and line.endswith(shown)


@pytest.mark.parametrize("project, resolved, shown", [
    (None, "adc-proj", "adc-proj (ADC / gcloud default)"),
    (None, None, "ADC / gcloud default (unresolved)"),
    ("my-proj", "my-proj", "my-proj")])
def test_first_query_prints_the_project_in_use(monkeypatch, capsys, project, resolved, shown):
    made = {}

    def fake_client(**kw):
        made.update(kw)
        return types.SimpleNamespace(
            project=resolved,
            instance=lambda name: types.SimpleNamespace(database=lambda db: ("db", name, db)))

    monkeypatch.setattr(agent, "_db", None)
    monkeypatch.setattr(agent, "PROJECT_ID", project)
    monkeypatch.setattr(agent.spanner, "Client", fake_client)
    assert agent._get_database() == ("db", agent.SPANNER_INSTANCE, agent.SPANNER_DATABASE)
    assert made["project"] == project
    assert capsys.readouterr().err.splitlines() == [f"[graph_query_agent] Spanner project: {shown}"]


def test_no_nested_dotenv_in_the_agent_package():
    assert not (ROOT / "graph_query_agent" / ".env").exists()


def test_adk_run_finds_the_root_dotenv(tmp_path, monkeypatch):
    # `adk run graph_query_agent` walks up from the agent directory and loads
    # the first .env, which is the root one here.
    from google.adk.cli.utils.envs import load_dotenv_for_agent
    (tmp_path / "graph_query_agent").mkdir()
    (tmp_path / ".env").write_text("CODEDOC_C1_PROBE=root\n", encoding="utf-8")
    monkeypatch.setenv("CODEDOC_C1_PROBE", "x")
    monkeypatch.delenv("CODEDOC_C1_PROBE")
    load_dotenv_for_agent("graph_query_agent", str(tmp_path))
    import os
    assert os.environ["CODEDOC_C1_PROBE"] == "root"


class _Field:
    def __init__(self, name):
        self.name = name


class _ResultSet:
    def __init__(self, rows):
        self.fields = [_Field("fqmn"), _Field("line")]
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _Snapshot:
    def __init__(self, result):
        self.result = result
        self.sql = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_sql(self, sql):
        self.sql = sql
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Db:
    def __init__(self, result):
        self.snap = _Snapshot(result)

    def snapshot(self):
        return self.snap


def test_run_gql_query_caps_rows_and_values(monkeypatch):
    rows = [["a." * 300, i] for i in range(60)] + [[None, 1]]
    db = _Db(_ResultSet(rows))
    monkeypatch.setattr(agent, "_get_database", lambda: db)
    out = agent.run_gql_query("GRAPH code_graph_a MATCH (m:Methods) RETURN m.fqmn", None)
    assert out["status"] == "success" and out["row_count"] == 61
    assert len(out["results"]) == 50
    assert len(out["results"][0]["fqmn"]) == 500 and out["results"][0]["line"] == "0"
    assert db.snap.sql.startswith("GRAPH code_graph_a")


def test_run_gql_query_reports_errors(monkeypatch):
    monkeypatch.setattr(agent, "_get_database", lambda: _Db(RuntimeError("boom " * 200)))
    out = agent.run_gql_query("GRAPH g MATCH (x) RETURN 1", None)
    assert out["status"] == "error" and len(out["error_message"]) == 500
