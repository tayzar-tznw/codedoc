"""evaluate's runtime oracle for real on a tiny fixture (marker `go`): goracle is
built, scans the site table, observes the fixture's entries and judges every
resolved record; a planted wrong edge and a planted wrong caller fail."""
from __future__ import annotations

import json

import pytest

from graph_generator import config, evaluate
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
COL = LIB.splitlines()[6].index("A()") + 1          # `func Use() int { return A() }`


@pytest.fixture
def fixture_dir(tmp_path, monkeypatch):
    root = write_tree(tmp_path / "tiny", {"go.mod": "module example.com/tiny\n\ngo 1.26\n", "lib.go": LIB,
                                          "lib_test.go": LIB_TEST,
                                          "entries.json": json.dumps({"tests": {"dir": ".", "test": ["./..."]}})})
    monkeypatch.setattr(config, "GO_TOOLCHAIN", GO)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    return root


def _runs(*recs) -> dict[str, evaluate.RepoRun]:
    return {"tiny": evaluate.RepoRun(name="tiny", dir=".", entities={"lib.go": {"types": [], "funcs": []}},
                                     resolutions=resolutions({"lib.go": list(recs)}, "tiny"))}


RIGHT = record(7, COL, "resolved", name="A", caller="Use", target=target("lib.go", "A", line=3))


def test_a_correct_run_passes_the_oracle(fixture_dir, tmp_path):
    got = evaluate.oracle_check(fixture_dir, _runs(RIGHT), str(tmp_path / "eval"), judge=True)
    assert got["error"] is None and got["failed_entries"] == [] and got["problems"] == 0, got
    assert got["edges"] == 1 and got["wrong"] == 0
    assert sum(got["verdicts"].get(v, 0) for v in ("CONFIRMED", "CONFIRMED-NOEXEC")) == 1   # `return A()`
    assert got["pass"] is True


def test_a_planted_wrong_edge_or_caller_fails_the_oracle(fixture_dir, tmp_path):
    wrong_edge = record(7, COL, "resolved", name="A", caller="Use", target=target("lib.go", "B", line=5))
    got = evaluate.oracle_check(fixture_dir, _runs(RIGHT, wrong_edge), str(tmp_path / "e1"), judge=True)
    assert got["wrong"] == 1 and got["verdicts"].get("WRONG-OTHER") == 1 and got["pass"] is False
    assert got["problems"] == 1          # B is not the site's name either (6b)
    wrong_caller = record(7, COL, "resolved", name="A", caller="B", target=target("lib.go", "A", line=3))
    got = evaluate.oracle_check(fixture_dir, _runs(wrong_caller), str(tmp_path / "e2"), judge=False)
    assert got["problem_examples"] == ["lib.go:7:%d call A: caller B, goracle's table says Use" % COL]
    assert got["edges"] == 0 and got["pass"] is False


IFACE_LIB = """package tiny

type Shape interface{ Area() int }

type Sq struct{}

func (Sq) Area() int { return 4 }

func Use(s Shape) int { return s.Area() }
"""
IFACE_TEST = """package tiny

import "testing"

func TestUse(t *testing.T) {
	if Use(Sq{}) != 4 {
		t.Fatal("bad")
	}
}
"""


@pytest.mark.parametrize("open_world, partial, verdict", [
    (False, None, "IFACE-OTHER"),          # p6b, closed world: [] is sent and the Sq.Area that runs is outside
    (True, None, "IFACE-CONSISTENT"),      # p8: open world, no key, name rule (a documented limitation)
    (False, "suppressed", "IFACE-CONSISTENT"),
])
def test_only_a_closed_world_edge_is_judged_against_its_candidates(tmp_path, monkeypatch, open_world, partial,
                                                                   verdict):
    # R50 as amended: an empty list is sent only for a closed-world edge without candidates_partial
    root = write_tree(tmp_path / "tiny", {"go.mod": "module example.com/tiny\n\ngo 1.26\n", "lib.go": IFACE_LIB,
                                          "lib_test.go": IFACE_TEST,
                                          "entries.json": json.dumps({"tests": {"dir": ".", "test": ["./..."]}})})
    monkeypatch.setattr(config, "GO_TOOLCHAIN", GO)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    lines = IFACE_LIB.splitlines()
    use = next(i for i, line in enumerate(lines, 1) if line.startswith("func Use"))
    rec = record(use, lines[use - 1].index("Area()") + 1, "resolved", name="Area", caller="Use",
                 target=target("lib.go", "Shape.Area", line=3), open_world=open_world)
    if partial:
        rec["candidates_partial"] = partial
    runs = {"tiny": evaluate.RepoRun(name="tiny", dir=".", entities={"lib.go": {
        "types": [{"qualname": "Shape", "methods": [{"qualname": "Shape.Area"}]}], "funcs": []}},
        resolutions=resolutions({"lib.go": [rec]}, "tiny"))}
    got = evaluate.oracle_check(root, runs, str(tmp_path / "eval"), judge=True)
    assert got["error"] is None and got["verdicts"] == {verdict: 1}, got
    assert got["pass"] is (verdict == "IFACE-CONSISTENT")
