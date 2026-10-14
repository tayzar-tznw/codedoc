"""README.md: the usage manual owns prerequisites, setup, settings, the CLI and the
three run modes, outputs, freshness and resume, offline vs download, peers, the
query surfaces and troubleshooting (spec §11)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEXT = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif("pyright" in TEXT, reason="WP-C2 has not written the Go README yet")


@pytest.mark.parametrize("fact", [
    "setup go", "GO_TOOLCHAIN", "go1.27.2", "uv venv --python 3.13 .venv", "doctor",
    "--all", "--graph", "--wiki", "--no-spanner", "--download-modules", "GO_MODULES_DOWNLOAD",
    "GO_USER_MODCACHE", "--peer [name=]path", "PEER_REPOS", "--repos", "units.json",
    "entities.json", "resolutions.json", "graph_checkpoint.json", "graph_rows/", "summaries/nodes/",
    "crossref_report.json", "codedoc-go-mcp", "graph_query_agent/.env"])
def test_owned_facts_are_stated(fact):
    assert fact in TEXT


def test_one_project_setting_without_a_hardcoded_default():
    row = re.search(r"^\| `GOOGLE_CLOUD_PROJECT` \|(.*)$", TEXT, re.MULTILINE).group(1)
    assert "ADC / gcloud" in row
    assert "development-459201" not in TEXT and "claude-cws-498905" not in TEXT
