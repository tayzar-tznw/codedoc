"""docs/CROSS_PROJECT.md owns modules and repos, identity, peers, crossref, the
verification runbook and the limits (spec §8, §11). Its GQL is checked by
scripts.doccheck."""
from __future__ import annotations

from pathlib import Path

import pytest

TEXT = (Path(__file__).resolve().parents[1] / "docs" / "CROSS_PROJECT.md").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif("pyright" in TEXT,
                                reason="WP-C2 has not written the Go CROSS_PROJECT.md yet")


@pytest.mark.parametrize("fact", [
    "no owner", "two owners", "node missing", "repo not ingested", "crossref_report.json",
    "PEER_REPOS", "--peer [name=]path", "--repos", "CrossRepoCalls", "CrossRepoRef",
    "CrossRepoImports", "replace", "go.work", "example.com/iryo/shared"])
def test_owned_facts_are_stated(fact):
    assert fact in TEXT
