"""--wiki preconditions (spec §7.1): a finished --graph on the same file hashes,
the same ID scheme and the same Spanner target, else a refusal naming the fix
(exit code 4 through the CLI)."""

import json
import os

import pytest

from graph_generator import __main__ as m
from graph_generator import config, pipeline
from graph_generator.pipeline import PipelineData, WikiPreconditionError
from tests.conftest import SAMPLE_GO, SAMPLE_TREE, write_tree
from tests.pipeline_fakes import install_graph_fakes, install_wiki_fakes


@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path / "iryo", SAMPLE_TREE)


def _graph(repo, **options):
    data = PipelineData(target_dir=repo, repo="iryo", **options)
    pipeline.run_graph_track(data)
    return data


def _wiki_data(repo, no_spanner):
    data = pipeline.load_graph_state(repo, "iryo")
    pipeline.prepare_wiki(data, no_spanner=no_spanner)
    return data


def test_wiki_refuses_without_graph(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    with pytest.raises(WikiPreconditionError, match="run --graph first"):
        pipeline.load_graph_state(repo, "iryo")
    _graph(repo, no_spanner=True)
    assert pipeline.load_graph_state(repo, "iryo").repo == "iryo"
    # A re-run with new resolutions that stops after Phase 8 leaves a graph
    # that is half rewritten: not finished, although the files are unchanged
    # and the saved state of the first run still matches their hashes.
    resolve = pipeline.phase1c_resolve

    def new_resolutions(data):
        resolve(data)
        data.resolutions["stats"] = {"by_status": {"resolved": 3}}
        data.resolutions["files"]["libs/billing/fee.go"]["calls"].append({"status": "resolved"})
    monkeypatch.setattr(pipeline, "phase1c_resolve", new_resolutions)
    monkeypatch.setattr(pipeline, "phase9_write_edges",
                        lambda data: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        _graph(repo, no_spanner=True)
    with open(os.path.join(out_dir, "graph_checkpoint.json"), encoding="utf-8") as f:
        cp = json.load(f)
    assert (cp["phase"], cp["complete"]) == ("phase8", False)
    assert pipeline.load_pipeline_data(repo, "iryo").file_hashes == cp["file_hashes"]
    with pytest.raises(WikiPreconditionError, match="run --graph first"):
        pipeline.load_graph_state(repo, "iryo")


def test_wiki_refuses_on_other_id_scheme(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    _graph(repo, no_spanner=True)
    path = os.path.join(out_dir, "graph_checkpoint.json")
    with open(path, encoding="utf-8") as f:
        cp = json.load(f)
    cp["id_scheme"] = pipeline.ID_SCHEME + 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cp, f)
    with pytest.raises(WikiPreconditionError, match="ID scheme.*run --graph first"):
        pipeline.load_graph_state(repo, "iryo")


def test_wiki_refuses_on_changed_hashes(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    _graph(repo, no_spanner=True)
    with open(os.path.join(repo, "libs", "billing", "fee.go"), "a", encoding="utf-8") as f:
        f.write("\nconst Rate = 2\n")
    write_tree(repo, {"libs/billing/rates.go": "package billing\n"})
    os.remove(os.path.join(repo, "cmd", "reception", "main.go"))
    with pytest.raises(WikiPreconditionError) as ei:
        _wiki_data(repo, no_spanner=True)
    message = str(ei.value)
    assert "3 files differ from the graph" in message
    for line in ("changed  libs/billing/fee.go", "added    libs/billing/rates.go",
                 "deleted  cmd/reception/main.go"):
        assert line in message
    assert "--graph` (or `--all`)" in message
    assert "service.go" not in message  # unchanged files are not listed


def test_wiki_refuses_on_spanner_target_mismatch(repo, out_dir, monkeypatch, fake_spanner):
    install_graph_fakes(monkeypatch)
    _graph(repo)  # written to the configured Spanner target
    assert _wiki_data(repo, no_spanner=False).repo == "iryo"  # same target: accepted
    monkeypatch.setattr(config, "SPANNER_DATABASE", "codedoc-go-other")
    with pytest.raises(WikiPreconditionError) as ei:
        _wiki_data(repo, no_spanner=False)
    message = str(ei.value)
    assert "codedoc-instance/codedoc-go " in message  # the graph's target
    assert "codedoc-instance/codedoc-go-other " in message  # the configured target
    assert "SPANNER_DATABASE" in message
    # --no-spanner writes nothing to Spanner, so no target has to match.
    assert _wiki_data(repo, no_spanner=True).no_spanner is True


def test_wiki_no_spanner_rule(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    _graph(repo, no_spanner=True)
    with pytest.raises(WikiPreconditionError) as ei:
        _wiki_data(repo, no_spanner=False)
    message = str(ei.value)
    assert "--no-spanner" in message and "written locally only" in message
    data = _wiki_data(repo, no_spanner=True)
    assert sorted(data.file_hashes) == SAMPLE_GO


def test_problems_are_reported_together(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    _graph(repo, no_spanner=True)
    os.remove(os.path.join(repo, "libs", "kernel", "doc.go"))
    with pytest.raises(WikiPreconditionError) as ei:
        _wiki_data(repo, no_spanner=False)
    assert "deleted  libs/kernel/doc.go" in str(ei.value)
    assert "written locally only" in str(ei.value)


def test_graph_state_must_match_the_checkpoint(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    _graph(repo, no_spanner=True)
    os.remove(os.path.join(out_dir, "pipeline_data.pkl"))
    with pytest.raises(WikiPreconditionError, match="run --graph first"):
        pipeline.load_graph_state(repo, "iryo")


@pytest.mark.parametrize("flags,code", [(["--no-spanner"], 0), ([], 4)])
def test_cli_wiki_exit_codes(repo, out_dir, monkeypatch, capsys, flags, code):
    install_graph_fakes(monkeypatch)
    install_wiki_fakes(monkeypatch)
    monkeypatch.setattr(pipeline, "preflight", lambda *a, **k: ([], None))
    with pytest.raises(SystemExit) as ei:
        m.main(["analyze", repo, "--wiki", *flags])
    assert ei.value.code == 4  # no graph yet
    assert "run --graph first" in capsys.readouterr().err
    m.main(["analyze", repo, "--graph", "--no-spanner"])
    if code:
        with pytest.raises(SystemExit) as ei:
            m.main(["analyze", repo, "--wiki", *flags])
        assert ei.value.code == code
        assert "--no-spanner" in capsys.readouterr().err
    else:
        m.main(["analyze", repo, "--wiki", *flags])
        page_dir = os.path.join(out_dir, "repos", "iryo", "topics")
        assert any(name.endswith(".md") for name in os.listdir(page_dir))


def test_saved_graph_state_guard(tmp_path, out_dir, capsys):
    """The pickle --wiki loads belongs to this target and repo; fields added
    since it was written get their defaults (spec §2.6 rule 2)."""
    import pickle
    target = str(tmp_path / "iryo")
    os.makedirs(target)
    path = os.path.join(out_dir, "pipeline_data.pkl")
    pipeline.save_pipeline_data(PipelineData(target_dir=target, repo="iryo",
                                             file_hashes={"a.go": "1"}))
    loaded = pipeline.load_pipeline_data(target, "iryo")
    assert loaded is not None and loaded.file_hashes == {"a.go": "1"}
    assert pipeline.load_pipeline_data(str(tmp_path / "api"), "iryo") is None  # other target
    assert pipeline.load_pipeline_data(target, "api") is None  # other repo

    old = PipelineData(target_dir=target, repo="iryo")
    del old.node_summaries, old.graph_counts  # written by an older version
    with open(path, "wb") as f:
        pickle.dump(old, f)
    backfilled = pipeline.load_pipeline_data(target, "iryo")
    assert backfilled.node_summaries == {} and backfilled.graph_counts == {}

    with open(path, "wb") as f:
        pickle.dump({"not": "pipeline data"}, f)
    assert pipeline.load_pipeline_data(target, "iryo") is None
    with open(path, "wb") as f:
        f.write(b"\x80\x04garbage")
    assert pipeline.load_pipeline_data(target, "iryo") is None
    assert "Ignoring" in capsys.readouterr().out


def test_long_change_lists_are_capped(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    _graph(repo, no_spanner=True)
    write_tree(repo, {f"gen/g{i:03d}.go": "package gen\n" for i in range(60)})
    with pytest.raises(WikiPreconditionError) as ei:
        _wiki_data(repo, no_spanner=True)
    message = str(ei.value)
    assert "60 files differ from the graph" in message
    assert "added    gen/g049.go" in message and "gen/g050.go" not in message
    assert "... and 10 more" in message


def test_wiki_needs_the_graph_rows(repo, out_dir, monkeypatch):
    """R20: the wiki track reads its graph context from graph_rows/."""
    import shutil
    install_graph_fakes(monkeypatch)
    _graph(repo, no_spanner=True)
    shutil.rmtree(os.path.join(out_dir, "graph_rows"))
    with pytest.raises(WikiPreconditionError, match="graph_rows is missing.*run --graph first"):
        pipeline.load_graph_state(repo, "iryo")
