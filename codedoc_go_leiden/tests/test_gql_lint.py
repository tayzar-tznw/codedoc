"""scripts.gql_lint: each mistake class is caught, valid queries pass, GQL is found
in fenced and instruction-style text, and the CLI lints the surfaces against a
schema module (the real one, or the Go-schema fixture until WP-B3 lands)."""
from __future__ import annotations

import pytest

from scripts import gql_lint
from scripts.gql_lint import lint_gql
from tests.go_schema_spec import EDGE_SPEC, NODE_SPEC

FIXTURE = "tests.go_schema_spec"


def _lint(text: str) -> list[str]:
    return lint_gql(text, NODE_SPEC, EDGE_SPEC)


@pytest.mark.parametrize("query", [
    "GRAPH g\nMATCH (c:Methods)-[e:MethodCalls]->(m:Methods {fqmn: 'example.com/a.T.M'})\n"
    "RETURN c.fqmn, e.resolution, e.call_line",
    "GRAPH g\nMATCH (m:Methods)<-[e:MethodCalls]-(c:Methods)\nWHERE m.name = 'f'\nRETURN c.fqmn",
    "GRAPH g\nMATCH (t:Classes {fqcn: 'example.com/a.Server'})-[:ClassEmbeds]->{1,3}(x:Classes)\n"
    "RETURN DISTINCT x.fqcn",
    "GRAPH g\nMATCH (s:Classes)-[:ClassEmbeds]->{ 1, 3 }(x:Classes)\nRETURN x.fqcn",
    "GRAPH g\nMATCH (a:Methods)-[e:MethodCalls]->(b:Methods)\nWHERE a.module <> b.module\n"
    "RETURN a.module, b.module, COUNT(*) AS calls\n"
    "GROUP BY a.module, b.module ORDER BY calls DESC LIMIT 20",
    "GRAPH g\nMATCH (p:Packages)-[:PackageContainsFile]->(f:Files)-[e:PackageImports]->(d:Packages)\n"
    "RETURN DISTINCT d.import_path, e.alias",
    "-- comment line\nGRAPH g MATCH (x:Files) RETURN COUNT(x) AS n;\n"
    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES",
    "GRAPH g\nMATCH (a:Methods {fqmn: 'example.com/a.init#1'})-[e:MethodCalls]->(b)\n"
    "RETURN b.fqmn",
    "GRAPH g\nMATCH (a:Methods {qualname: '<init>.func1'})-[e:MethodCalls]->(b:Methods)\n"
    "RETURN b.fqmn",
])
def test_valid_queries_pass(query):
    assert _lint(query) == []


@pytest.mark.parametrize("query, expected", [
    ("MATCH (a:Files) RETURN a.path", "does not start with GRAPH"),
    ("GRAPH g\nRETURN 1", "no MATCH"),
    ("GRAPH g\nMATCH (a:Files)", "no RETURN"),
    ("GRAPH g\nMATCH (a:Files RETURN a.path", "unbalanced brackets"),
    ("GRAPH g\nMATCH (a:Modules) RETURN a.name", "unknown node label Modules"),
    ("GRAPH g\nMATCH (a:Methods)-[e:Calls]->(b:Methods) RETURN a.name", "unknown edge label Calls"),
    ("GRAPH g\nMATCH (a:Classes) RETURN a.namespace", "Classes has no column namespace"),
    ("GRAPH g\nMATCH (a:Classes {fqmn: 'x'}) RETURN a.name", "Classes has no column fqmn"),
    ("GRAPH g\nMATCH (c:Classes)-[:FileDefinesClass]->(f:Files) RETURN f.path",
     "FileDefinesClass goes Files -> Classes, not Classes -> Files"),
    ("GRAPH g\nMATCH (p:Packages)-[:PackageImports]->(f:Files) RETURN f.path",
     "PackageImports goes Files -> Packages, not Packages -> Files"),
    ("GRAPH g\nMATCH (a:Classes)-[i:ClassEmbeds]->{1,3}(b:Classes) RETURN i.pointer",
     "i is a quantified edge variable"),
    ("GRAPH g\nMATCH (a:Methods) RETURN a", "RETURN a returns a graph element"),
    ("GRAPH g\nMATCH (a:Methods) RETURN b.name", "unknown variable b"),
    ("GRAPH g\nMATCH (a:Methods)-[x:MethodCalls]->(a:Files) RETURN a.name",
     "variable a bound to Methods and Files"),
    ("GRAPH g\nMATCH (a:Methods) ~~ (b:Methods) RETURN a.name", "unrecognized pattern text"),
    ("GRAPH g\nMATCH (a:Methods)-[:MethodCalls]->(b) RETURN b.fqcn", "Methods has no column fqcn"),
])
def test_mistakes_are_reported(query, expected):
    errors = _lint(query)
    assert any(expected in e for e in errors), errors


def test_statements_are_numbered():
    errors = _lint("GRAPH g MATCH (a:Files) RETURN a.path;\nGRAPH g MATCH (b:Nope) RETURN b.x")
    assert errors and all(e.startswith("statement 2:") for e in errors)


def test_statements_skip_sql_and_blank_parts():
    text = "GRAPH g MATCH (a:Files) RETURN a.path;\n;\nSELECT 1;\nGRAPH g MATCH (b:Files) RETURN b.path"
    assert len(gql_lint.statements(text)) == 2


def test_gql_blocks_finds_fenced_and_instruction_style_text():
    text = (
        "前置き。`GRAPH g` で始めます。\n"
        "```sql\nSELECT 1 FROM INFORMATION_SCHEMA.TABLES;\n```\n"
        "```sql\nGRAPH g\nMATCH (a:Files) RETURN a.path;\n```\n"
        "```\nGRAPH g MATCH (b:Files) RETURN b.path\n```\n"
        "```go\nfunc main() {}\n```\n"
        "### 呼び出し元:\nGRAPH g\nMATCH (c:Methods)\nRETURN c.fqmn\n\n"
        "### 次:\nGRAPH g\nMATCH (d:Methods)\nRETURN d.fqmn\n"
        "## 見出し\nGRAPH は句の名前です。\n")
    assert gql_lint.gql_blocks(text) == [
        "GRAPH g\nMATCH (a:Files) RETURN a.path;\n",
        "GRAPH g MATCH (b:Files) RETURN b.path\n",
        "GRAPH g\nMATCH (c:Methods)\nRETURN c.fqmn",
        "GRAPH g\nMATCH (d:Methods)\nRETURN d.fqmn",
    ]


def test_gql_blocks_ignore_graph_lines_inside_other_fences():
    # An instruction-style paragraph is only looked for outside fences.
    assert gql_lint.gql_blocks("```text\nGRAPH g\n```\n") == []


def test_load_spec_reads_a_schema_module():
    node, edge = gql_lint.load_spec(FIXTURE)
    assert node is NODE_SPEC and edge is EDGE_SPEC


def test_load_spec_names_what_is_missing():
    with pytest.raises(SystemExit, match="scripts.gql_lint has no NODE_SPEC / EDGE_SPEC"):
        gql_lint.load_spec("scripts.gql_lint")


def test_lint_sources_counts_and_labels(tmp_path):
    sources = [("a.md", "```sql\nGRAPH g MATCH (a:Files) RETURN a.path;\n"
                        "GRAPH g MATCH (b:Classes) RETURN b.namespace\n```\n"),
               ("b.md", "no queries here\n")]
    errors, counts = gql_lint.lint_sources(sources, NODE_SPEC, EDGE_SPEC)
    assert counts == {"a.md": 2, "b.md": 0}
    assert errors == ["a.md: statement 2: Classes has no column namespace"]


def test_cli_lints_named_files_against_the_fixture(tmp_path, capsys):
    good = tmp_path / "good.md"
    good.write_text("```sql\nGRAPH g\nMATCH (p:Packages) RETURN p.import_path\n```\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("```sql\nGRAPH g\nMATCH (m:Modules) RETURN m.name\n```\n", encoding="utf-8")
    assert gql_lint.main(["--spec", FIXTURE, str(good)]) == 0
    out = capsys.readouterr().out
    assert f"{good}: 1 statements" in out and "gql_lint: OK (1 statements in 1 sources" in out
    assert gql_lint.main(["--spec", FIXTURE, str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert f"{bad}: statement 1: unknown node label Modules" in out
    assert "gql_lint: FAIL" in out


def test_default_sources_are_the_instruction_the_skill_and_the_docs():
    labels = [label for label, _text in gql_lint.default_sources()]
    assert labels[:2] == ["graph_query_agent instruction", ".claude/skills/codedoc-query/SKILL.md"]
    assert set(labels[2:]) <= {"README.md", "docs/ARCHITECTURE.md", "docs/LSP_RESOLUTION.md",
                               "docs/GRAPH_SCHEMA.md", "docs/CROSS_PROJECT.md"}
    assert not any("superpowers" in label for label in labels)


def test_cli_passes_on_the_instruction_and_the_skill(capsys):
    # The WP-C1 gate: `python -m scripts.gql_lint --spec tests.go_schema_spec`
    # minus the Japanese docs, which WP-C2 rewrites for Go.
    from graph_query_agent import agent
    sources = gql_lint.default_sources()[:2]
    errors, counts = gql_lint.lint_sources(sources, NODE_SPEC, EDGE_SPEC)
    assert errors == []
    assert counts["graph_query_agent instruction"] == len(agent.GQL_COOKBOOK)


def test_cli_no_docs_lints_the_instruction_and_the_skill_only(capsys):
    assert gql_lint.main(["--spec", FIXTURE, "--no-docs"]) == 0
    out = capsys.readouterr().out
    assert "graph_query_agent instruction:" in out and ".claude/skills/codedoc-query/SKILL.md:" in out
    assert "README.md" not in out and "docs/" not in out
    assert "gql_lint: OK" in out
