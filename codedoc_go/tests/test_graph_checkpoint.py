"""graph_checkpoint.json (C7): the ID scheme, the completion flag, the phase,
the file hashes, the hash of resolutions.json and the Spanner target; Phases
8-9 resume only on the same inputs (spec §2.7)."""

import json
import os

import pytest

from graph_generator import config, pipeline
from graph_generator.pipeline import (ID_SCHEME, PipelineData, _load_graph_checkpoint,
                                      _save_graph_checkpoint, resolutions_hash, run_graph_track)
from tests.conftest import SAMPLE_GO, SAMPLE_TREE, write_tree
from tests.pipeline_fakes import install_graph_fakes


@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path / "iryo", SAMPLE_TREE)


def _checkpoint(out_dir):
    with open(os.path.join(out_dir, "graph_checkpoint.json"), encoding="utf-8") as f:
        return json.load(f)


def test_checkpoint_has_the_c7_shape(repo, out_dir, monkeypatch, fake_spanner):
    install_graph_fakes(monkeypatch)
    data = PipelineData(target_dir=repo, repo="iryo")
    run_graph_track(data)
    cp = _checkpoint(out_dir)
    assert list(cp) == ["id_scheme", "complete", "phase", "file_hashes", "resolutions_hash",
                        "spanner", "id_prefix", "graph", "project", "communities_hash"]
    assert (cp["id_prefix"], cp["graph"]) == (config.ID_PREFIX, config.GRAPH_NAME)
    assert cp["project"] is None   # no GOOGLE_CLOUD_PROJECT, and conftest disables ADC
    assert cp["id_scheme"] == ID_SCHEME == 1
    assert cp["complete"] is True and cp["phase"] == "phase9"
    assert sorted(cp["file_hashes"]) == SAMPLE_GO and cp["file_hashes"] == data.file_hashes
    assert cp["resolutions_hash"] == resolutions_hash(data.resolutions)
    assert cp["spanner"] == {"instance": "codedoc-instance", "database": "codedoc-go",
                             "graph": "code_graph_a", "id_prefix": config.ID_PREFIX}
    assert cp["communities_hash"] is None


def test_checkpoint_and_graph_meta_are_ascii_json(tmp_path, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    repo = write_tree(tmp_path / "会計", SAMPLE_TREE)
    run_graph_track(PipelineData(target_dir=repo, no_spanner=True))
    for name in ("graph_checkpoint.json", "graph_meta.json"):
        with open(os.path.join(out_dir, name), encoding="utf-8") as f:
            text = f.read()
        assert text.isascii() and json.loads(text)["id_scheme"] == 1
    with open(os.path.join(out_dir, "graph_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta == {"repo": "会計", "target_dir": os.path.realpath(repo), "id_scheme": 1,
                    "id_prefix": config.ID_PREFIX, "spanner": None, "project": None}


def test_same_inputs_skip_phases_8_and_9(repo, out_dir, monkeypatch, fake_spanner, capsys):
    calls = install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    assert fake_spanner.mutations["Files"]
    fake_spanner.mutations.clear()
    calls.clear()
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    assert fake_spanner.mutations == {}
    assert "build_node_rows" not in calls and "derive_edge_rows" not in calls
    assert "Skipping Phase 8" in capsys.readouterr().out
    assert _checkpoint(out_dir)["complete"] is True


@pytest.mark.parametrize("change", ["file", "resolutions", "spanner", "id_scheme"])
def test_changed_inputs_rewrite_from_phase_8(repo, out_dir, monkeypatch, fake_spanner, change):
    install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    fake_spanner.mutations.clear()
    if change == "file":
        with open(os.path.join(repo, "libs", "billing", "fee.go"), "a", encoding="utf-8") as f:
            f.write("\nconst Rate = 2\n")
    elif change == "resolutions":
        original = pipeline.phase1c_resolve

        def resolve(data):
            original(data)
            data.resolutions["files"]["libs/billing/fee.go"]["calls"].append({"status": "dynamic"})
        monkeypatch.setattr(pipeline, "phase1c_resolve", resolve)
    elif change == "spanner":
        monkeypatch.setattr(config, "SPANNER_DATABASE", "codedoc-go-2")
    else:
        cp = _checkpoint(out_dir)
        cp["id_scheme"] = ID_SCHEME + 1
        with open(os.path.join(out_dir, "graph_checkpoint.json"), "w", encoding="utf-8") as f:
            json.dump(cp, f)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    assert len(fake_spanner.mutations["Files"]) == 5  # nodes rewritten
    assert fake_spanner.mutations["DirContainsFile"]  # edges rewritten


def test_switching_to_no_spanner_rewrites_locally(repo, out_dir, monkeypatch, fake_spanner):
    install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    assert os.path.isfile(os.path.join(out_dir, "graph_rows", "Files.jsonl"))
    assert _checkpoint(out_dir)["spanner"] is None


def test_interrupted_phase_9_resumes_without_rewriting_nodes(repo, out_dir, monkeypatch,
                                                            fake_spanner):
    calls = install_graph_fakes(monkeypatch)
    original = pipeline.phase9_write_edges
    monkeypatch.setattr(pipeline, "phase9_write_edges",
                        lambda data: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    cp = _checkpoint(out_dir)
    assert (cp["phase"], cp["complete"]) == ("phase8", False)
    monkeypatch.setattr(pipeline, "phase9_write_edges", original)
    calls.clear()
    fake_spanner.mutations.clear()
    run_graph_track(PipelineData(target_dir=repo, repo="iryo"))
    assert "build_node_rows" not in calls and "derive_edge_rows" in calls
    assert "Files" not in fake_spanner.mutations and fake_spanner.mutations["DirContainsFile"]
    assert _checkpoint(out_dir)["complete"] is True


def test_a_restart_marks_the_graph_incomplete_before_writing(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    with open(os.path.join(repo, "libs", "kernel", "doc.go"), "a", encoding="utf-8") as f:
        f.write("\nvar X = 1\n")
    seen = {}
    monkeypatch.setattr(pipeline, "phase8_write_nodes",
                        lambda data: seen.setdefault("cp", _checkpoint(out_dir)))
    monkeypatch.setattr(pipeline, "phase9_write_edges", lambda data: None)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))
    assert (seen["cp"]["phase"], seen["cp"]["complete"]) == ("phase1.6", False)


def test_unreadable_checkpoint_means_no_resume(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    with open(os.path.join(out_dir, "graph_checkpoint.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    data = PipelineData(target_dir=repo, repo="iryo", no_spanner=True)
    assert _load_graph_checkpoint(data) is None
    _save_graph_checkpoint(data, "phase1.6", complete=False)
    assert _load_graph_checkpoint(data) is None  # nothing written yet: start at Phase 8
    _save_graph_checkpoint(data, "phase8", complete=False)
    assert _load_graph_checkpoint(data) == "phase8"


def test_resolutions_hash_ignores_stats():
    artifact = {"version": 1, "repo": "iryo", "files": {"a.go": {"sha256": "1", "calls": []}}}
    timed = {**artifact, "stats": {"by_status": {"resolved": 3}, "elapsed_seconds": 12.5}}
    assert resolutions_hash(timed) == resolutions_hash(artifact)
    assert resolutions_hash(artifact) != resolutions_hash({**artifact, "repo": "other"})
    assert resolutions_hash(None) == resolutions_hash({})


def test_resolutions_are_hashed_once_per_run(repo, out_dir, monkeypatch):
    install_graph_fakes(monkeypatch)
    hashed = []
    monkeypatch.setattr(pipeline, "resolutions_hash",
                        lambda artifact: hashed.append(1) or resolutions_hash(artifact))
    data = PipelineData(target_dir=repo, repo="iryo", no_spanner=True)
    run_graph_track(data)          # three checkpoints
    assert len(hashed) == 1
    assert _checkpoint(out_dir)["resolutions_hash"] == data.resolutions_digest \
        == resolutions_hash(data.resolutions)
    run_graph_track(PipelineData(target_dir=repo, repo="iryo", no_spanner=True))   # resumes
    assert len(hashed) == 2
