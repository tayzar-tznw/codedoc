"""The codedoc-query skill describes this variant's graph: the Go schema and its
representation (spec §6.5), in the skill's English form."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from graph_generator import setup_spanner_graph as real_schema
from tests import go_schema_spec as go

SKILL = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "codedoc-query"
         / "SKILL.md").read_text(encoding="utf-8")
# The real schema once WP-B3 lands, the spec fixture until then.
NODE_SPEC, EDGE_SPEC = ((real_schema.NODE_SPEC, real_schema.EDGE_SPEC)
                        if "Packages" in real_schema.NODE_SPEC else (go.NODE_SPEC, go.EDGE_SPEC))


def test_front_matter_names_the_skill():
    assert SKILL.startswith("---\nname: codedoc-query\n")


def test_counts_and_every_table_are_listed():
    assert f"**Nodes** ({len(NODE_SPEC)})" in SKILL
    assert f"**Edges** ({len(EDGE_SPEC)})" in SKILL
    for table in [*NODE_SPEC, *EDGE_SPEC]:
        assert f"`{table}`" in SKILL, table


@pytest.mark.parametrize("term", [
    *go.POSSIBLY_REASONS, *go.SITE_KINDS, "types", "convention:<rule>",
    "external", "builtin", "dynamic", "unresolved", "ambiguous", "open_world",
    "<package>", "<init>", "init#1", "F.func1", "T.M", "fqcn", "fqmn", "Files.module",
    "import path", "go.work", "replace", "codedoc_go", "Go types",
])
def test_go_representation_is_explained(term):
    assert term in SKILL


def test_ambiguous_and_builtin_match_the_resolver():
    # ambiguous: reason variant, or implementation for unnamed interfaces (R2);
    # Error() of the predeclared error interface is builtin (ruling 2026-10-13)
    flat = " ".join(SKILL.split())
    assert re.search(r"`ambiguous`[^.]*`variant`[^.]*`implementation`", flat)
    assert "inline constraint" in flat and "function-local interface" in flat
    assert re.search(r"`builtin`[^.]*`error`", flat)


@pytest.mark.parametrize("question, pattern", [
    ("who calls X", r"what calls `<"),
    ("what X calls", r"what does `<[^`]+>` call"),
    ("implementers", r"implement(s|ers of)? (the )?interface `<"),
    ("embedding", r"embed"),
    ("package dependencies", r"import(s|ed)? .*package `<"),
    ("calls between modules", r"from module `<[^`]+>` to `<"),
    ("calls between repos", r"other repositories"),
])
def test_example_questions_cover_the_cookbook(question, pattern):
    section = SKILL.split("## Asking good questions", 1)[1].split("\n## ", 1)[0]
    assert re.search(pattern, section), question


@pytest.mark.parametrize("stale", [
    "PEP 3155", "PEP 503", "distribution", "<module>", "<lambda@L>", "Cls.prop{get}", "pyright",
    "iface-impl", "import-table", "codedoc_python", "`Modules`", "FileImports", "ClassInherits",
    "FileBelongsToModule", "DbTables", "CakePHP", "migration"])
def test_no_leftovers_from_other_variants(stale):
    assert stale not in SKILL


def test_no_lsp_resolution_value():
    assert not re.search(r"\blsp\b", SKILL)
