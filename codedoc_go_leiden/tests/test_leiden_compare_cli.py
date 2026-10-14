"""`compare-topics` (leiden spec §6, L-R27): codedoc_go's LLM topics against
the Leiden topics of the same repo and commit. Usage errors exit 2; runs that
did not see the same input, or that lack a finished step, exit 4."""
from __future__ import annotations

import json
import os
import shutil

import pytest

from graph_generator import __main__ as cli
from graph_generator import pipeline, topics_phase
from graph_generator.topics import community
from tests.conftest import FakeGenaiClient
from tests.leiden_worlds import (graph_data, install_leiden_graph, install_wiki_world, leiden_responder,
                                 read_jsonl, run_wiki, topics_file)


def _write(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f, ensure_ascii=False)


def _read(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _seal(directory) -> None:
    """graph_checkpoint.json records the run's communities.json, entities.json
    and resolutions.json, as a finished --graph run's does (the synthetic
    world writes no entities or resolutions, so the fixture makes them)."""
    path = os.path.join(directory, "graph_checkpoint.json")
    cp = _read(path)
    cp["communities_hash"] = topics_phase.document_hash(_read(os.path.join(directory, "topics", "communities.json")))
    cp["file_hashes"] = {p: e["sha256"] for p, e in _read(os.path.join(directory, "entities.json"))["entities"].items()}
    cp["resolutions_hash"] = pipeline.resolutions_hash(_read(os.path.join(directory, "resolutions.json")))
    _write(path, cp)


@pytest.fixture
def runs(monkeypatch, tmp_path, out_dir):
    """(llm dir, leiden dir): a finished leiden --graph and --wiki on
    synthetic rows, and a codedoc_go-shaped output of the same input whose
    topics are the feature directories."""
    target, _calls = install_leiden_graph(monkeypatch, tmp_path)
    install_wiki_world(monkeypatch)
    pipeline.run_graph_track(graph_data(target))
    run_wiki(target, FakeGenaiClient(leiden_responder))
    leiden = out_dir
    files = sorted(r["path"] for r in read_jsonl(leiden, "Files"))
    run = {"entities.json": {"version": 1, "repo": "iryo",
                             "entities": {p: {"sha256": f"sha-{p}"} for p in files}},
           "units.json": {"version": 1, "mode": "offline",
                          "builds": [{"id": "linux-amd64", "primary": True}]},
           "resolutions.json": {"version": 1, "files": {}, "stats": {"seconds": 1}}}
    llm = str(tmp_path / "go-out" / "repos" / "iryo")
    for name, doc in run.items():
        _write(os.path.join(leiden, name), doc)
        _write(os.path.join(llm, name), doc)
    _seal(leiden)
    by_dir: dict[str, list[str]] = {}
    for p in files:
        by_dir.setdefault(p.split("/")[1] if p.count("/") > 1 else "root", []).append(p)
    tree = [{"id": f"llm_{i}", "name": f"トピック{i}", "summary": "", "files": fs, "subtopics": []}
            for i, (_d, fs) in enumerate(sorted(by_dir.items()))]
    _write(os.path.join(llm, "topics", "topic_tree.json"), tree)
    _write(os.path.join(llm, "topics", "llm_0.md"), "# トピック0\n`NoSuchType` を使います。\n")
    _write(os.path.join(llm, "topics", "stats.json"),
           {"discovery": {"calls": 3, "input_tokens": 10, "output_tokens": 5, "seconds": 1.0}})
    return llm, leiden


def _exit_code(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code


def test_both_runs_are_required(capsys):
    assert _exit_code(["compare-topics", "--llm", "x"]) == 2
    assert "--leiden" in capsys.readouterr().err


def test_a_missing_directory_is_a_usage_error(runs, capsys):
    llm, _leiden = runs
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", llm + "-nope"]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_a_missing_llm_topic_tree_is_a_precondition_failure(runs, capsys):
    llm, leiden = runs
    os.remove(os.path.join(llm, "topics", "topic_tree.json"))
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden]) == 4
    err = capsys.readouterr().err
    assert "topic_tree.json" in err and "codedoc_go" in err


def test_a_missing_run_file_is_a_precondition_failure(runs, capsys):
    llm, leiden = runs
    os.remove(os.path.join(leiden, "resolutions.json"))
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden]) == 4
    assert "resolutions.json" in capsys.readouterr().err


def test_runs_on_other_files_are_refused(runs, capsys):
    llm, leiden = runs
    path = os.path.join(llm, "entities.json")
    doc = json.load(open(path, encoding="utf-8"))
    first = sorted(doc["entities"])[0]
    doc["entities"][first]["sha256"] = "changed"
    _write(path, doc)
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden]) == 4
    err = capsys.readouterr().err
    assert "1 files differ" in err and first in err


def test_runs_with_another_build_are_refused(runs, capsys):
    llm, leiden = runs
    _write(os.path.join(llm, "units.json"), {"version": 1, "mode": "download",
                                             "builds": [{"id": "linux-amd64", "primary": True}]})
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden]) == 4
    assert "mode differs" in capsys.readouterr().err


def test_communities_of_other_graph_rows_are_refused(runs, capsys):
    llm, leiden = runs
    path = os.path.join(leiden, "graph_rows", "MethodCalls.jsonl")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines[1:])
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden]) == 4
    assert "projection" in capsys.readouterr().err


def test_compare_topics_writes_the_report(runs, tmp_path, capsys):
    llm, leiden = runs
    features = tmp_path / "features.json"
    files = sorted(r["path"] for r in read_jsonl(leiden, "Files"))
    features.write_text(json.dumps({"features": [{"name": "feat000",
                                                  "files": [p for p in files if "/feat000/" in p]}]}))
    out = tmp_path / "cmp"
    cli.main(["compare-topics", "--llm", llm, "--leiden", leiden, "--features", str(features),
              "--llm2", llm, "--out", str(out)])
    result = json.loads((out / "topic_comparison.json").read_text(encoding="utf-8"))
    assert result["inputs"]["runs_verified"] is True
    assert result["stability"]["leiden"]["rerun_hash_equal"] is True
    assert result["stability"]["llm"]["ari"] == 1.0  # --llm2 is the same run
    assert result["features"]["features"][0]["name"] == "feat000"
    topics = community.all_topics(topics_file(leiden, "communities.json"))
    grounding = result["grounding"]["leiden"]
    assert grounding["pages"] == len(topics)
    assert grounding["checked"] - grounding["found"] == len(topics)  # the grounding section is not re-checked
    assert result["grounding"]["llm"]["top_missing"] == ["NoSuchType"]
    assert result["cost"]["llm"]["discovery"]["calls"] == 3
    markdown = (out / "topic_comparison.md").read_text(encoding="utf-8")
    assert markdown.startswith("#")
    stdout = capsys.readouterr().out
    assert str(out / "topic_comparison.json") in stdout and "AMI" in stdout


def test_the_report_goes_to_the_leiden_run_dir_by_default(runs):
    """Not topics/: Phase 6 deletes every .md there that is not a topic page."""
    llm, leiden = runs
    cli.main(["compare-topics", "--llm", llm, "--leiden", leiden])
    with open(os.path.join(leiden, "topic_comparison.json"), encoding="utf-8") as f:
        assert json.load(f)["ablation"]["profile"] == "no_implements"
    assert os.path.isfile(os.path.join(leiden, "topic_comparison.md"))
    assert not os.path.exists(os.path.join(leiden, "topics", "topic_comparison.md"))


def test_a_missing_backend_exits_3(runs, monkeypatch, capsys):
    llm, leiden = runs

    def missing():
        raise community.BackendMissingError(community.INSTALL_HINT)

    monkeypatch.setattr(community, "_backend", missing)
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden]) == 3
    assert "graspologic-native is not installed" in capsys.readouterr().err


def test_a_missing_networkx_exits_3(runs, monkeypatch, capsys):
    import sys
    llm, leiden = runs
    for name in [m for m in sys.modules if m.startswith("graph_generator.topics")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "networkx", None)
    import graph_generator
    monkeypatch.delattr(graph_generator, "topics", raising=False)
    assert _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden]) == 3
    assert "networkx" in capsys.readouterr().err


def test_a_second_leiden_run_replaces_the_in_process_rerun(runs, tmp_path):
    llm, leiden = runs
    other = tmp_path / "leiden2"
    shutil.copytree(leiden, other)
    path = other / "topics" / "communities.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["membership_hash"] = "0" * 16
    path.write_text(json.dumps(doc))
    _seal(other)
    out = tmp_path / "cmp2"
    cli.main(["compare-topics", "--llm", llm, "--leiden", leiden, "--leiden2", str(other),
              "--out", str(out)])
    result = json.loads((out / "topic_comparison.json").read_text(encoding="utf-8"))
    assert result["stability"]["leiden"]["rerun_hash_equal"] is False


def _edit(path, change) -> None:
    doc = _read(path)
    change(doc)
    _write(str(path), doc)


@pytest.mark.parametrize("flag", ["--llm2", "--leiden2"])
def test_a_second_run_on_other_input_is_refused(runs, tmp_path, capsys, flag):
    """L-R27 holds for the second runs too: another commit's run is no rerun."""
    llm, leiden = runs
    other = tmp_path / "second"
    shutil.copytree(llm if flag == "--llm2" else leiden, other)
    _edit(other / "units.json", lambda d: d.update(builds=[{"id": "windows-amd64", "primary": True}]))
    code = _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden, flag, str(other)])
    err = capsys.readouterr().err
    assert code == 4 and f"{flag} {other} is not a second run on the same input" in err and "build differs" in err


def test_a_second_leiden_run_with_other_topic_settings_is_refused(runs, tmp_path, capsys):
    llm, leiden = runs
    other = tmp_path / "leiden2"
    shutil.copytree(leiden, other)
    _edit(other / "topics" / "communities.json", lambda d: d["params"].update(seeds=[7]))
    _seal(other)
    code = _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden, "--leiden2", str(other)])
    assert code == 4 and "topic settings (TOPIC_*, SUBTOPIC_MIN_ATOMS) differ" in capsys.readouterr().err


@pytest.mark.parametrize("stale", ["incomplete", "topics/communities.json", "entities.json", "resolutions.json"])
def test_a_leiden_run_whose_files_are_not_from_its_checkpoint_is_refused(runs, capsys, stale):
    """The --leiden files come from one finished --graph run: its checkpoint
    is complete and holds their hashes."""
    llm, leiden = runs
    if stale == "incomplete":
        _edit(os.path.join(leiden, "graph_checkpoint.json"), lambda d: d.update(complete=False))
    elif stale == "topics/communities.json":
        _edit(os.path.join(leiden, "topics", "communities.json"), lambda d: d.update(membership_hash="0" * 16))
    elif stale == "entities.json":
        _edit(os.path.join(leiden, "entities.json"),
              lambda d: d["entities"][sorted(d["entities"])[0]].update(sha256="changed"))
    else:
        _edit(os.path.join(leiden, "resolutions.json"), lambda d: d.update(files={"x.go": {}}))
    code = _exit_code(["compare-topics", "--llm", llm, "--leiden", leiden])
    err = capsys.readouterr().err
    assert code == 4
    assert ("no finished --graph run" in err if stale == "incomplete"
            else f"{stale} not from the --graph run graph_checkpoint.json records" in err)
