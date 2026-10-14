"""F1 end to end with fakes (leiden spec §1 done criteria 2 and 3, §7): the real
graph track on test_codes/iryo-kanri-system (a Go toolchain builds and runs
gohelper), Phase 4a's integrity and determinism, the wiki track with a fake
Gemini, and compare-topics with F1's reviewed features against a
directory-based LLM topic tree of the same run. Run with
`.venv/bin/python -m pytest -q -m go --no-cov tests/test_leiden_f1.py`."""
from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest

from graph_generator import __main__ as cli
from graph_generator import config, pipeline, topics_phase
from graph_generator.topics import community, graph_builder
from tests.conftest import FakeGenaiClient
from tests.go_worlds import GO
from tests.leiden_worlds import leiden_responder
from tests.wiki_worlds import wiki_responder

pytestmark = pytest.mark.go

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
F1 = os.path.join(ROOT, "test_codes", "iryo-kanri-system")
REPO = "iryo"


def _respond(model, contents, cfg):
    if "名前を付けてください" in contents or "構造的根拠" in contents:
        return leiden_responder(model, contents, cfg)
    return wiki_responder()(model, contents, cfg)


def _graph(out_base: str) -> dict:
    with cli._repo_output_dir(REPO):
        shutil.rmtree(pipeline._out_root(), ignore_errors=True)
    cli.main(["analyze", F1, "--graph", "--no-spanner", "--repo-name", REPO])
    with open(os.path.join(out_base, "repos", REPO, "topics", "communities.json"), encoding="utf-8") as f:
        return json.load(f)


def test_f1_end_to_end(tmp_path, monkeypatch, capsys):
    out_base = str(tmp_path / "out")
    monkeypatch.setattr(config, "OUTPUT_DIR", out_base)
    monkeypatch.setattr(config, "GO_TOOLCHAIN", GO)
    monkeypatch.setattr(pipeline, "_check_gemini", lambda soft: pipeline.Check("gemini", "ok", "fake"))

    # Determinism (done criterion 3): three graph runs, the same membership.
    docs = [_graph(out_base) for _ in range(3)]
    assert len({(d["membership_hash"], topics_phase.document_hash(d)) for d in docs}) == 1
    doc = docs[0]
    run = os.path.join(out_base, "repos", REPO)

    # Integrity (done criterion 2) on the rows the graph wrote.
    rows = {name[:-6]: [json.loads(line) for line in open(os.path.join(run, "graph_rows", name),
                                                            encoding="utf-8")]
            for name in os.listdir(os.path.join(run, "graph_rows"))}
    nodes, edges = topics_phase._split_rows(rows)
    projection = graph_builder.build_projection(nodes, edges, doc["profile"])
    assert projection.hash == doc["projection_hash"]
    assert community.check_integrity(doc, projection) == []
    assert doc["metrics"]["topics"]["1"] >= 3 and len(projection.atoms) > 100
    classes = {r["class_id"] for r in nodes["Classes"]}
    assert {r["class_id"] for r in rows["ClassBelongsToTopic"]} == classes  # every atom, once per level

    # The wiki track with a fake Gemini, then compare-topics.
    with cli._repo_output_dir(REPO):
        data = pipeline.load_graph_state(F1, REPO)
        pipeline.prepare_wiki(data, no_spanner=True)
        asyncio.run(pipeline.run_wiki_track(data, FakeGenaiClient(responder=_respond)))
    topics = community.all_topics(doc)
    assert all(os.path.isfile(os.path.join(run, "topics", f"{tid}.md")) for tid in topics)
    llm = str(tmp_path / "go-out" / "repos" / REPO)
    for name in ("entities.json", "units.json", "resolutions.json"):
        os.makedirs(llm, exist_ok=True)
        shutil.copy(os.path.join(run, name), os.path.join(llm, name))
    by_dir: dict[str, list[str]] = {}
    for f in nodes["Files"]:
        by_dir.setdefault(f["path"].split("/")[0], []).append(f["path"])
    tree = [{"id": f"llm_{d}", "name": d, "summary": "", "files": sorted(fs), "subtopics": []}
            for d, fs in sorted(by_dir.items())]
    os.makedirs(os.path.join(llm, "topics"))
    with open(os.path.join(llm, "topics", "topic_tree.json"), "w", encoding="utf-8") as f:
        json.dump(tree, f)
    capsys.readouterr()
    out = str(tmp_path / "cmp")
    cli.main(["compare-topics", "--llm", llm, "--leiden", run, "--out", out,
              "--features", os.path.join(F1, "ground_truth", "features.json")])
    result = json.load(open(os.path.join(out, "topic_comparison.json"), encoding="utf-8"))
    assert result["inputs"]["runs_verified"] is True
    assert result["stability"]["leiden"]["rerun_hash_equal"] is True
    with open(os.path.join(F1, "ground_truth", "features.json"), encoding="utf-8") as f:
        assert len(result["features"]["features"]) == len(json.load(f)["features"])
    assert result["coverage"]["leiden"]["files"] == result["coverage"]["leiden"]["files_total"]
    assert result["grounding"]["leiden"]["pages"] == len(topics)
    assert "AMI" in capsys.readouterr().out
