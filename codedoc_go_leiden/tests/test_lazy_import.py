"""graph_generator.lazy_import: GCP modules load on first use, and patches on
the stand-in reach the real module."""

import os
import subprocess
import sys

from graph_generator.lazy_import import LazyModule

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _target(tmp_path, monkeypatch, name):
    (tmp_path / f"{name}.py").write_text("VALUE = 1\nOTHER = 2\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)


def test_imports_on_first_attribute_read(tmp_path, monkeypatch):
    _target(tmp_path, monkeypatch, "lazy_target_read")
    lazy = LazyModule("lazy_target_read")
    assert "lazy_target_read" not in sys.modules
    assert "not loaded" in repr(lazy)
    assert lazy.VALUE == 1
    assert "lazy_target_read" in sys.modules
    assert "(loaded)" in repr(lazy)


def test_writes_and_deletes_reach_the_module(tmp_path, monkeypatch):
    _target(tmp_path, monkeypatch, "lazy_target_write")
    lazy = LazyModule("lazy_target_write")
    with monkeypatch.context() as m:
        m.setattr(lazy, "VALUE", 5)
        assert sys.modules["lazy_target_write"].VALUE == 5
        assert lazy.VALUE == 5
    assert sys.modules["lazy_target_write"].VALUE == 1     # monkeypatch undid it on the module
    del lazy.OTHER
    assert not hasattr(sys.modules["lazy_target_write"], "OTHER")


def test_graph_modules_import_without_gcp_libraries():
    code = ("import sys, graph_generator.pipeline, graph_generator.setup_spanner_graph, "
            "graph_generator.graph_rows; "
            "print(sorted(m for m in ('google.genai', 'google.cloud.spanner', 'google.api_core.exceptions') "
            "if m in sys.modules))")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "CODEDOC_DOTENV": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
