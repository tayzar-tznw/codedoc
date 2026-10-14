"""Every query entry point reads only the root .env (spec §10)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["mcp_server.runner", "webapp.main"])
def test_entry_point_loads_only_the_root_dotenv(module, monkeypatch):
    import dotenv
    seen = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda path=None, **kw: seen.append(Path(path)))
    monkeypatch.delenv("CODEDOC_DOTENV")      # the suite's switch would skip the (faked) load
    for name in (module, "graph_query_agent.config"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    importlib.import_module(module)
    assert seen and set(seen) == {ROOT / ".env"}
