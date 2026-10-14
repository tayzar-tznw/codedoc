"""CLAUDE.md (English) gives a coding agent this variant's commands and invariants."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEXT = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif("pyright" in TEXT,
                                reason="CLAUDE.md is still the codedoc_python copy (WP-B1/C2)")


def test_sections_follow_the_sibling_format():
    assert re.findall(r"^## (.+)$", TEXT, re.MULTILINE) == [
        "Project overview", "Environment & commands", "Architecture",
        "Test fixtures (`test_codes/`)"]


@pytest.mark.parametrize("fact", [
    "go1.27.2", "gohelper", "uv venv --python 3.13 .venv", "Never create `graph_query_agent/.env`",
    "-m go", "-m network --no-cov", "--cov-fail-under=85", "evaluate --env", "InstructionProvider",
    "ID_SCHEME = 1", "codedoc-go-mcp", "scripts/gql_lint.py",
    "`GOOGLE_CLOUD_PROJECT` has no hardcoded default", "never add comments to fixture code"])
def test_key_facts_are_stated(fact):
    assert fact in TEXT


def test_written_in_english():
    japanese = [line for line in TEXT.splitlines() if re.search(r"[぀-ヿ一-鿿]", line)]
    assert len(japanese) <= 2, japanese
