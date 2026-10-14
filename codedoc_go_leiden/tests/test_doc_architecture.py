"""docs/ARCHITECTURE.md owns phases and tracks, components, data flow, checkpoints,
the helper, the query stack and the design decisions (spec §11)."""
from __future__ import annotations

from pathlib import Path

import pytest

TEXT = (Path(__file__).resolve().parents[1] / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif("pyright" in TEXT,
                                reason="WP-C2 has not written the Go ARCHITECTURE.md yet")


@pytest.mark.parametrize("fact", [
    "--graph", "--wiki", "--all", "Phase 1.6", "gohelper", "go/parser", "go/packages", "go/types",
    "go_units.py", "go_toolchain.py", "go_modcache.py", "go_extract.py", "resolution.py",
    "go_classify.py", "go_gates.py", "node_summaries.py", "build_node_rows", "derive_edge_rows",
    "graph_checkpoint.json", "ID_SCHEME", "NODE_SPEC", "EDGE_SPEC", "InstructionProvider",
    "scripts/gql_lint.py"])
def test_owned_facts_are_stated(fact):
    assert fact in TEXT
