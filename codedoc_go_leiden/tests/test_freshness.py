"""ensure_fresh (spec §2.6 rules 1-3): entity records of changed and deleted
files are dropped; Phase 1.5 (WP-B2) re-extracts what it returns."""

import pytest

from graph_generator.pipeline import PipelineData, ensure_fresh


def _record(sha):
    return {"sha256": sha, "module": "example.com/iryo", "package": "example.com/iryo/billing",
            "types": [], "funcs": []}


@pytest.mark.parametrize("entities,hashes,kept,pending", [
    ({"a.go": _record("1")}, {"a.go": "1"}, ["a.go"], []),
    ({"a.go": _record("1")}, {"a.go": "2"}, [], ["a.go"]),
    ({"gone.go": _record("1")}, {}, [], []),
    ({}, {"new.go": "9"}, [], ["new.go"]),
    ({"a.go": _record("1"), "b.go": _record("x"), "c.go": _record("3")},
     {"a.go": "1", "b.go": "2", "d.go": "4"}, ["a.go"], ["b.go", "d.go"]),
])
def test_ensure_fresh(entities, hashes, kept, pending):
    data = PipelineData(target_dir="/r", extracted_entities=dict(entities), file_hashes=hashes)
    assert ensure_fresh(data) == pending
    assert sorted(data.extracted_entities) == kept
    assert ensure_fresh(data) == pending  # idempotent


def test_ensure_fresh_reports_what_it_dropped(capsys):
    data = PipelineData(target_dir="/r", file_hashes={"a.go": "2"},
                        extracted_entities={"a.go": _record("1"), "gone.go": _record("1")})
    ensure_fresh(data)
    assert "dropped entities of 1 changed and 1 deleted files" in capsys.readouterr().out
