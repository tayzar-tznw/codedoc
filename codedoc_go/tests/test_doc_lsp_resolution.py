"""docs/LSP_RESOLUTION.md owns Phase 1.6: the problem cases, why go/types and not
gopls, groups and the runtime truth, classification, interface dispatch, rules,
gates, statuses, resolutions.json, verification with dates and the limits
(spec §11). The family file name is kept; the first paragraph names the engine."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import go_schema_spec as go

TEXT = (Path(__file__).resolve().parents[1] / "docs" / "LSP_RESOLUTION.md").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif("pyright" in TEXT,
                                reason="WP-C2 has not written the Go LSP_RESOLUTION.md yet")


def test_first_paragraph_says_go_types_not_a_language_server():
    first = next(p for p in re.split(r"\n\s*\n", TEXT) if p.strip() and not p.startswith("#"))
    assert "go/types" in first and ("言語サーバー" in first or "LSP" in first)


@pytest.mark.parametrize("fact", [
    *(f"G{i}" for i in range(1, 11)), *go.STATUSES, *go.UNRESOLVED_REASONS, *go.POSSIBLY_REASONS,
    *(v for v in go.VIA if v != "none"), "gopls", "resolutions.json", "open_world", "go.work",
    "cgo", "GOPATH", "goracle", "generate_manifest.py", "check_ground_truth.py"])
def test_owned_facts_are_stated(fact):
    assert fact in TEXT


def test_classification_table_has_the_spec_rows():
    assert re.findall(r"^\| (\d+) \|", TEXT, re.MULTILINE) == [str(i) for i in range(1, 10)]
