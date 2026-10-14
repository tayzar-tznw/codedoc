"""Run modes (spec §2.1): the graph track (1 → 1.5 → 1.6 → 8 → 9), the wiki track
(1 re-scan → 2 → 3 → 4 → 5 → 6 → 10) and --all, which runs the graph track first;
--no-spanner writes the rows to graph_rows/*.jsonl (spec §2.3, C6)."""

import json
import os

import pytest

from graph_generator import __main__ as m
from graph_generator import pipeline
from graph_generator.pipeline import PipelineData
from tests.conftest import SAMPLE_GO, SAMPLE_TREE, write_tree
from tests.pipeline_fakes import install_graph_fakes, install_wiki_fakes, recorder

GRAPH_ORDER = ["phase1_scan", "discover", "phase1b_extract", "phase1c_resolve",
               "phase8_write_nodes", "build_node_rows", "phase9_write_edges", "derive_edge_rows"]
WIKI_ORDER = ["phase1_rescan", "phase2", "phase3", "phase4_topics", "phase5_topic_pages",
              "phase6_index", "phase10"]


@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path / "iryo", SAMPLE_TREE)


@pytest.fixture
def calls(monkeypatch):
    calls = install_graph_fakes(monkeypatch)
    install_wiki_fakes(monkeypatch, calls)
    for name in ("phase1_scan", "phase8_write_nodes", "phase9_write_edges", "phase1_rescan",
                 "phase4_topics", "phase5_topic_pages", "phase6_index"):
        monkeypatch.setattr(pipeline, name, recorder(calls, name, getattr(pipeline, name)))
    monkeypatch.setattr(pipeline, "preflight", lambda *a, **k: ([], None))
    return calls


def test_all_runs_the_graph_track_before_the_wiki_track(repo, out_dir, calls):
    m.main(["analyze", repo, "--all", "--no-spanner"])
    assert calls == GRAPH_ORDER + WIKI_ORDER


def test_graph_mode_makes_no_wiki_calls(repo, out_dir, calls):
    m.main(["analyze", repo, "--graph", "--no-spanner"])
    assert calls == GRAPH_ORDER


def test_wiki_mode_reuses_the_finished_graph(repo, out_dir, calls):
    m.main(["analyze", repo, "--graph", "--no-spanner"])
    calls.clear()
    m.main(["analyze", repo, "--wiki", "--no-spanner"])
    assert calls == WIKI_ORDER


def test_no_spanner_writes_jsonl_rows_and_no_spanner_calls(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    monkeypatch.setattr(pipeline, "_get_spanner_db",
                        lambda: pytest.fail("--no-spanner must not open Spanner"))
    data = PipelineData(target_dir=repo, repo="iryo", no_spanner=True)
    pipeline.run_graph_track(data)
    rows_dir = os.path.join(out_dir, "graph_rows")
    assert sorted(n for n in os.listdir(rows_dir) if n.endswith(".jsonl")) == [
        "DirContainsFile.jsonl", "Directories.jsonl", "Files.jsonl"]
    with open(os.path.join(rows_dir, "Files.jsonl"), encoding="utf-8") as f:
        files = [json.loads(line) for line in f]
    assert [r["path"] for r in files] == SAMPLE_GO
    assert files[0] == {"file_id": pipeline._make_id("file", "iryo", SAMPLE_GO[0], ""),
                        "path": SAMPLE_GO[0], "repo": "iryo"}
    assert data.graph_counts == {"Files": 5, "Directories": 6, "DirContainsFile": 5}
    for name in ("graph_checkpoint.json", "graph_meta.json"):
        with open(os.path.join(out_dir, name), encoding="utf-8") as f:
            assert json.load(f)["spanner"] is None


def test_no_spanner_rewrite_drops_stale_tables(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    rows_dir = os.path.join(out_dir, "graph_rows")
    os.makedirs(rows_dir)
    with open(os.path.join(rows_dir, "PossiblyCalls.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"edge_id": "stale"}\n')
    pipeline.run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    assert "PossiblyCalls.jsonl" not in os.listdir(rows_dir)


def test_graph_track_writes_spanner_and_jsonl_rows(repo, out_dir, monkeypatch, fake_spanner):
    """R20: graph_rows/*.jsonl is written with Spanner too; --wiki reads it."""
    install_graph_fakes(monkeypatch)
    data = PipelineData(target_dir=repo, repo="iryo")
    pipeline.run_graph_track(data)
    assert fake_spanner.columns["Files"] == ["file_id", "path", "repo"]
    assert [r[1] for r in fake_spanner.mutations["Files"]] == SAMPLE_GO
    assert len(fake_spanner.mutations["DirContainsFile"]) == 5
    with open(os.path.join(out_dir, "graph_rows", "Files.jsonl"), encoding="utf-8") as f:
        assert [json.loads(line)["path"] for line in f] == SAMPLE_GO


def test_graph_track_prints_status_summary_and_saves_state(repo, out_dir, monkeypatch, capsys):
    install_graph_fakes(monkeypatch)
    pipeline.run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    out = capsys.readouterr().out
    assert "Sites by status: resolved 2, external 1" in out
    assert "Rows written: Files 5, Directories 6, DirContainsFile 5" in out
    state = pipeline.load_pipeline_data(repo, "iryo")
    assert state is not None and sorted(state.file_hashes) == SAMPLE_GO
    assert state.resolutions["stats"]["by_status"] == {"resolved": 2, "external": 1}


def test_wiki_track_writes_pages_by_topic_id(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    install_wiki_fakes(monkeypatch)
    data = PipelineData(target_dir=repo, repo="iryo", no_spanner=True)
    pipeline.run_graph_track(data)
    pipeline.prepare_wiki(data, no_spanner=True)
    import asyncio
    asyncio.run(pipeline.run_wiki_track(data))
    [topic] = data.topics
    assert os.path.isfile(os.path.join(out_dir, "topics", f"{topic['id']}.md"))
    assert os.path.isfile(os.path.join(out_dir, "index.md"))


def test_wiki_track_prints_the_call_estimate_before_phase_2(repo, out_dir, monkeypatch, capsys):
    calls = install_graph_fakes(monkeypatch)
    install_wiki_fakes(monkeypatch, calls)
    data = PipelineData(target_dir=repo, repo="iryo", no_spanner=True)
    pipeline.run_graph_track(data)
    pipeline.prepare_wiki(data, no_spanner=True)
    import asyncio
    asyncio.run(pipeline.run_wiki_track(data))
    # the fake graph rows have no declarations: one batched call per file
    assert "[Wiki] Estimated Gemini calls: Phase 2 = 5 (5 files to summarize" in capsys.readouterr().out
    estimate = {"phase2": 42, "files": 40, "cached": 2, "generated": 1, "phase3": 9,
                "phase4": 4, "phase5": 15}
    monkeypatch.setattr(pipeline, "estimate_wiki_calls", recorder(calls, "estimate", lambda d: estimate))
    calls.clear()
    asyncio.run(pipeline.run_wiki_track(data))
    assert calls[:2] == ["estimate", "phase2"]
    assert ("Phase 2 = 42 (40 files to summarize; 2 cached, 1 generated without a call); "
            "Phases 3-5 at most 9 + 4 + 15") in capsys.readouterr().out


def test_summary_cache_dir(out_dir, monkeypatch, tmp_path):
    from graph_generator import config
    assert pipeline.summary_cache_dir("iryo") == os.path.join(out_dir, "summaries")
    monkeypatch.setattr(config, "WIKI_SUMMARY_CACHE", str(tmp_path / "shared"))
    assert pipeline.summary_cache_dir("iryo") == str(tmp_path / "shared" / "iryo")


def test_phase9_reports_what_was_not_edged(repo, out_dir, monkeypatch, capsys):
    """Spec §5.4/§6.3: sites that write no row are counted (EdgeRows.stats)."""
    install_graph_fakes(monkeypatch)

    class EdgeRows(dict):
        stats = {"possibly_capped": 2, "calls_external": 7, "files_stale": 0}
    monkeypatch.setattr(pipeline, "derive_edge_rows", lambda data, idx: EdgeRows({"DirContainsFile": []}))
    pipeline.run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    assert "[Phase 9] Not edged: calls_external 7, possibly_capped 2" in capsys.readouterr().out
