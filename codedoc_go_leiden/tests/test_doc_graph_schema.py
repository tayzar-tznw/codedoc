"""docs/GRAPH_SCHEMA.md owns the tables, the worked example, the ID formula, the
GQL cookbook and the limits (spec §11); its examples must match F1 and the code.
Tables, columns and GQL are checked by scripts.doccheck."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
F1 = ROOT / "test_codes" / "iryo-kanri-system"
TEXT = (ROOT / "docs" / "GRAPH_SCHEMA.md").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif("pyright" in TEXT,
                                reason="WP-C2 has not written the Go GRAPH_SCHEMA.md yet")


@pytest.mark.parametrize("fact", [
    "_make_id(kind, repo, relpath, qualname)", "ID_SCHEME", "<package>", "<init>", "init#1",
    "F.func1", "POSSIBLY_CALLS_MAX_CANDIDATES", "codedoc-go", "Files.module", "open_world",
    "FeeCalculator.Calculate", "example.com/iryo/billing"])
def test_owned_facts_are_stated(fact):
    assert fact in TEXT


def test_go_examples_are_f1_code():
    sources = [p.read_text(encoding="utf-8") for p in F1.rglob("*.go")]
    blocks = re.findall(r"^```go\n(.*?)^```", TEXT, re.MULTILINE | re.DOTALL)
    assert blocks
    for block in blocks:
        assert any(block in src for src in sources), block[:80]


@pytest.mark.parametrize("kind, qualname", [
    ("file", ""), ("class", "FeeCalculator"), ("method", "FeeCalculator.Calculate")])
def test_worked_example_ids_match_make_id(kind, qualname, monkeypatch):
    from graph_generator import config, pipeline
    monkeypatch.setattr(config, "ID_PREFIX", "a")
    node_id = pipeline._make_id(kind, "iryo-kanri-system", "billing/fee.go", qualname)
    assert f"`{node_id}`" in TEXT
