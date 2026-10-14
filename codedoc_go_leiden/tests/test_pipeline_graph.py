"""Graph helpers kept from codedoc_python (identity, Spanner writes, embeddings)
and the hand-off stubs later work packages fill (C6, spec §6.4)."""

import asyncio

import pytest
from google.api_core.exceptions import Aborted

from graph_generator import config, pipeline
from graph_generator.pipeline import (PipelineData, _batch_insert, _claim_id, _edge_id,
                                      _embed_and_write, _make_id, _row, _write_rows)
from tests.conftest import FakeGenaiClient, FakeSpannerDb


def test_make_id_is_deterministic_prefixed_and_scoped():
    a = _make_id("method", "iryo", "libs/billing/fee.go", "FeeCalculator.Calculate")
    assert a == _make_id("method", "iryo", "libs/billing/fee.go", "FeeCalculator.Calculate")
    assert a.startswith(config.ID_PREFIX + "_") and len(a) == len(config.ID_PREFIX) + 17
    assert a != _make_id("method", "other", "libs/billing/fee.go", "FeeCalculator.Calculate")
    assert a != _make_id("class", "iryo", "libs/billing/fee.go", "FeeCalculator.Calculate")
    # NUL joining: moving a separator between parts changes the ID
    assert _make_id("file", "r", "a|b", "") != _make_id("file", "r|a", "b", "")
    assert _edge_id("MethodCalls", "x", "y") != _edge_id("PossiblyCalls", "x", "y")
    assert _make_id("file", "r", "caf\udce9.go", "")  # a surrogate-escaped name never raises
    # Go qualnames with '#' and '<...>' are plain inputs (spec §4.2)
    assert len({_make_id("method", "r", "a.go", q) for q in ("init#1", "init#2", "<init>")}) == 3


def test_make_id_matches_the_codedoc_python_formula():
    import hashlib
    raw = "\x00".join(("method", "iryo", "libs/billing/fee.go", "FeeCalculator.Calculate"))
    expected = f"{config.ID_PREFIX}_" + hashlib.sha256(raw.encode()).hexdigest()[:16]
    assert _make_id("method", "iryo", "libs/billing/fee.go", "FeeCalculator.Calculate") == expected


def test_claim_id_allows_repeats_and_refuses_collisions():
    owners = {}
    assert _claim_id(owners, "a_1", ("method", "a.go", "F")) is True
    assert _claim_id(owners, "a_1", ("method", "a.go", "F")) is False
    with pytest.raises(RuntimeError, match="node ID collision"):
        _claim_id(owners, "a_1", ("method", "b.go", "F"))


def test_row_follows_the_column_order():
    assert _row(["b", "a"], {"a": 1, "b": 2, "extra": 3}) == [2, 1]
    with pytest.raises(KeyError):
        _row(["a", "missing"], {"a": 1})


def test_batch_insert_retries_transient_errors(instant_sleeps):
    class _FlakyDb(FakeSpannerDb):
        def __init__(self):
            super().__init__()
            self.failures = 1

        def batch(self):
            if self.failures:
                self.failures -= 1
                raise Aborted("try again")
            return super().batch()

    db = _FlakyDb()
    _batch_insert(db, "Files", ["file_id"], [["a"], ["b"]])
    assert db.mutations["Files"] == [["a"], ["b"]] and len(instant_sleeps) == 1


def test_batch_insert_gives_up_after_retries(instant_sleeps, out_dir):
    class _DeadDb(FakeSpannerDb):
        def batch(self):
            raise Aborted("down")

    with pytest.raises(Aborted):
        _batch_insert(_DeadDb(), "Files", ["file_id"], [["a"]], max_retries=3)
    assert len(instant_sleeps) == 2


def test_commits_stay_under_spanners_mutation_limit():
    """Spanner allows 80,000 mutations per commit and counts an insert once per
    written column (cloud.google.com/spanner/quotas); 5,000 Methods rows of 18
    columns would be 90,000."""
    sizes = []

    class _Batch:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def insert_or_update(self, table, columns, values):
            sizes.append(len(values) * len(columns))

    class _Db(FakeSpannerDb):
        def batch(self):
            return _Batch()

    columns = [f"c{i}" for i in range(18)]  # Methods writes 18 columns
    _batch_insert(_Db(), "Methods", columns, [[0] * 18] * 12_000)
    assert sum(sizes) == 12_000 * 18
    assert max(sizes) <= pipeline._MUTATIONS_PER_COMMIT < 80_000


def test_spanner_writes_use_spec_columns_in_parallel(fake_spanner, monkeypatch):
    columns = {"MethodCalls": ["edge_id", "caller_method", "callee_method"],
               "PossiblyCalls": ["edge_id", "caller_method", "callee_method", "open_world"]}
    monkeypatch.setattr(pipeline, "_columns", lambda table: columns[table])
    data = PipelineData(target_dir="/r", repo="iryo")
    counts = _write_rows(data, {
        "MethodCalls": [{"edge_id": "e1", "callee_method": "m2", "caller_method": "m1"}],
        "PossiblyCalls": [{"edge_id": "e2", "caller_method": "m1", "callee_method": "m3",
                           "open_world": True}],
        "ClassEmbeds": []}, parallel=True)
    assert counts == {"MethodCalls": 1, "PossiblyCalls": 1, "ClassEmbeds": 0}
    assert fake_spanner.mutations["MethodCalls"] == [["e1", "m1", "m2"]]
    assert fake_spanner.columns["PossiblyCalls"] == columns["PossiblyCalls"]
    assert "ClassEmbeds" not in fake_spanner.mutations
    assert data.graph_counts == counts


def test_columns_come_from_the_schema_spec():
    from graph_generator.setup_spanner_graph import write_columns
    assert pipeline._columns("Files") == write_columns("Files")


@pytest.mark.parametrize("phase,module,attr", [
    ("phase1b_extract", "go_extract", "extract_phase"),
    ("phase1c_resolve", "resolution", "resolve_phase"),
])
def test_helper_phases_delegate_to_wp_b2(phase, module, attr, monkeypatch, tmp_path):
    import sys
    import types
    seen = {}
    fake = types.ModuleType(f"graph_generator.{module}")
    setattr(fake, attr, lambda data, **kw: seen.update(data=data, **kw))
    monkeypatch.setitem(sys.modules, f"graph_generator.{module}", fake)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out" / "repos" / "iryo"))
    data = PipelineData(target_dir="/r", repo="iryo")
    getattr(pipeline, phase)(data)
    assert seen == {"data": data, "out_root": str(tmp_path / "out" / "repos" / "iryo"),
                    "output_dir": str(tmp_path / "out"), "printer": pipeline._print}
    monkeypatch.setitem(sys.modules, f"graph_generator.{module}", None)
    with pytest.raises(NotImplementedError, match=r"not implemented yet \(WP-B2\)"):
        getattr(pipeline, phase)(data)


def test_embed_and_write_updates_embeddings(fake_spanner, fake_vertexai):
    model = pipeline._embedding_model()
    items = [(f"a_{i}", f"text {i}") for i in range(config.EMBED_BATCH_SIZE + 3)]
    assert _embed_and_write(model, fake_spanner, items, "Files", "file_id", "files") == len(items)
    written = {row[0]: row[1] for row in fake_spanner.updates["Files"]}
    assert set(written) == {i for i, _ in items} and written["a_0"] == [0.1, 0.2, 0.3]
    assert fake_vertexai["n"] == 2  # two batches


def test_embed_and_write_reports_failures(fake_spanner, out_dir, instant_sleeps, capsys):
    class _Model:
        def get_embeddings(self, texts):
            raise RuntimeError("permanent embedding failure")

    assert _embed_and_write(_Model(), fake_spanner, [("a_1", "x")], "Files", "file_id", "files") == 0
    assert "Files" not in fake_spanner.updates
    assert "1 files failed embedding" in capsys.readouterr().out


def test_embed_and_write_retries_rate_limits(fake_spanner, instant_sleeps):
    class _Model:
        calls = 0

        def get_embeddings(self, texts):
            _Model.calls += 1
            if _Model.calls == 1:
                raise RuntimeError("429 Resource exhausted")
            return [type("E", (), {"values": [1.0]})() for _ in texts]

    assert _embed_and_write(_Model(), fake_spanner, [("a_1", "x")], "Topics", "topic_id", "topics") == 1
    assert instant_sleeps and fake_spanner.updates["Topics"] == [["a_1", [1.0]]]
