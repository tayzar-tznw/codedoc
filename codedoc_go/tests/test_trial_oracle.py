"""trial: the real precision check on a tiny module (marker `go`: builds goracle
and runs `go test` on an instrumented copy with the Go toolchain)."""
from __future__ import annotations

import pytest

from graph_generator import config, evaluate, trial
from tests.eval_worlds import record, resolutions, target
from tests.go_worlds import GO, write_tree

pytestmark = pytest.mark.go

LIB = """package tiny

func A() int { return 1 }

func B() int { return 2 }

func Use() int { return A() }
"""
LIB_TEST = """package tiny

import "testing"

func TestUse(t *testing.T) {
	if Use() != 1 {
		t.Fatal("bad")
	}
}
"""


@pytest.fixture
def tiny(tmp_path, monkeypatch):
    root = write_tree(tmp_path / "tiny", {"go.mod": "module example.com/tiny\n\ngo 1.26\n",
                                          "lib.go": LIB, "lib_test.go": LIB_TEST})
    (tmp_path / "no-cache").mkdir()
    monkeypatch.setattr(config, "GO_TOOLCHAIN", GO)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(config, "GO_USER_MODCACHE", str(tmp_path / "no-cache"))
    return root


def _run(*recs) -> evaluate.RepoRun:
    return evaluate.RepoRun(name="tiny", dir=".", entities={"lib.go": {"types": [], "funcs": []}},
                            resolutions=resolutions({"lib.go": list(recs)}, "tiny"))


def test_the_oracle_confirms_the_right_edge_and_catches_a_planted_wrong_one(tiny, tmp_path):
    col = LIB.splitlines()[6].index("A()") + 1           # `func Use() int { return A() }`
    right = record(7, col, "resolved", name="A", caller="Use", target=target("lib.go", "A", line=3))
    planted = record(7, col, "resolved", name="A", caller="Use", target=target("lib.go", "B", line=5))
    before = trial.snapshot(tiny)
    lines: list[str] = []
    got = trial.run_oracle(tiny, "tiny", _run(right, planted), ["."], str(tmp_path / "oracle"), lines.append)
    assert got["error"] is None, got
    assert got["packages"]["."]["status"] == "ok"
    assert got["edges"] == 2 and got["wrong"] == 1
    assert got["verdicts"].get("WRONG-OTHER") == 1
    assert sum(got["verdicts"].get(v, 0) for v in ("CONFIRMED", "CONFIRMED-NOEXEC")) == 1
    assert trial.tree_changes(before, trial.snapshot(tiny)) == []     # goracle only read the repo
    (tmp_path / "tiny" / "go.work.sum").write_text("planted", encoding="utf-8")
    assert trial.tree_changes(before, trial.snapshot(tiny)) == ["added: go.work.sum", "changed: ./"]
