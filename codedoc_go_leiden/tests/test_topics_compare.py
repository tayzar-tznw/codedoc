"""compare: every metric group of leiden spec §6 on synthetic inputs, with
overlapping LLM topics, a subtopic, an invented path and a test file."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_generator.topics import compare, metrics  # noqa: E402
from graph_generator.topics.community import detect  # noqa: E402
from graph_generator.topics.graph_builder import build_projection  # noqa: E402
from graph_generator.topics.synthetic import RowBuilder  # noqa: E402

BILLING = ["billing/fee.go", "billing/points.go", "billing/ledger.go"]
RECEPTION = ["reception/desk.go", "reception/queue.go", "reception/slot.go"]

LLM_TREE = [
    {"id": "t1", "name": "診療報酬", "summary": "",
     "files": ["billing/fee.go", "billing/points.go", "billing/fee_test.go", "billing/ghost.go"],
     "subtopics": [{"name": "点数", "summary": "", "files": ["billing/points.go", "./billing/ledger.go"]}]},
    {"id": "t2", "name": "受付", "summary": "",
     "files": RECEPTION + ["billing/ledger.go"], "subtopics": []},
]


def rows():
    b = RowBuilder()
    t = {}
    for path in BILLING + RECEPTION:
        t[path] = b.type(path, path.split("/")[1][:-3].title())
    n = 0

    def link(x, y, k):
        nonlocal n
        for _ in range(k):
            n += 1
            b.call(b.method(t[x], f"m{n}"), b.method(t[y], f"m{n}"))

    for group in (BILLING, RECEPTION):
        link(group[0], group[1], 3)
        link(group[1], group[2], 3)
        link(group[0], group[2], 3)
    link("reception/desk.go", "billing/fee.go", 1)
    b.file("billing/fee_test.go", origin="test")
    link_test = b.type("billing/fee_test.go", "feeFixture")
    b.call(b.method(link_test, "Check"), b.method(t["billing/fee.go"], "m1"))
    return b.rows(), t


@pytest.fixture(scope="module")
def world():
    (nodes, edges), t = rows()
    proj = build_projection(nodes, edges)
    data = detect(proj, {"band": (2, 4)}).data
    tid = {name: next(k for k, v in data["levels"]["1"].items() if t[path] in v["members"])
           for name, path in (("billing", "billing/fee.go"), ("reception", "reception/desk.go"))}
    names = {tid["billing"]: "会計処理", tid["reception"]: "受付処理"}
    features = {"features": [{"name": "会計", "files": BILLING + ["billing/fee_test.go"]},
                             {"name": "受付", "files": RECEPTION}]}
    tree2 = [{"id": "x", "name": "全部", "files": BILLING[:2] + RECEPTION[:1], "subtopics": []},
             {"id": "y", "name": "残り", "files": BILLING[2:] + RECEPTION[1:], "subtopics": []}]
    result = compare.compare_topics(
        LLM_TREE, data, nodes, edges, features=features, llm_tree2=tree2, leiden_names=names,
        communities2=detect(proj, {"band": (2, 4)}),
        llm_stats={"discovery": {"calls": 3, "input_tokens": 100, "output_tokens": 50, "seconds": 2.5},
                   "pages": {"calls": 2, "input_tokens": 40, "output_tokens": 30, "seconds": 1.0}},
        leiden_stats={"naming": {"calls": 1, "input_tokens": 10, "output_tokens": 5, "seconds": 0.5}},
        llm_pages={"t1.md": "`Fee` と `Ghost`"}, leiden_pages={"a.md": "`Fee` と `Points`"})
    return {"nodes": nodes, "edges": edges, "data": data, "tid": tid, "names": names, "result": result}


def test_coverage_counts_invented_paths_and_overlaps(world):
    cov = world["result"]["coverage"]
    assert cov["llm"]["files"] == 6 and cov["llm"]["files_total"] == 6
    assert cov["llm"]["atoms"] == 12 and cov["llm"]["atoms_total"] == 12
    assert cov["llm"]["invented"] == ["billing/ghost.go"] and cov["llm"]["invented_paths"] == 1
    assert cov["llm"]["non_production_files_listed"] == 1
    assert (cov["llm"]["files_in_several_topics"], cov["llm"]["files_in_several_leaves"]) == (1, 1)
    assert (cov["leiden"]["files"], cov["leiden"]["atoms"]) == (6, 12)
    assert (cov["leiden"]["files_in_several_topics"], cov["leiden"]["files_in_several_leaves"]) == (0, 0)


def test_size_per_level(world):
    size = world["result"]["size"]
    assert size["llm"]["top"]["topics"] == 2 and size["llm"]["leaf"]["topics"] == 3
    assert size["llm"]["top"]["files"] == {"min": 3, "median": 3.5, "max": 4, "mean": 3.5}
    assert size["llm"]["leaf"]["files"]["min"] == 1
    assert size["leiden"]["top"]["topics"] == 2 and size["leiden"]["top"]["atoms"]["max"] == 6
    assert size["leiden"]["top"]["largest_atom_share"] == 0.5
    assert size["llm"]["top"]["largest_atom_share"] == pytest.approx(8 / 12, abs=1e-6)


def test_agreement_on_single_topic_files(world):
    top = world["result"]["agreement"]["top"]
    assert top["files"] == 5                     # ledger.go is in two LLM topics
    assert (top["ami"], top["ari"], top["nmi"]) == (1.0, 1.0, 1.0)
    assert top["jaccard_llm_to_leiden"] == pytest.approx(0.875)
    assert top["jaccard_leiden_to_llm"] == pytest.approx(0.875)
    match = {m["name"]: m for m in top["matches"]}
    assert (match["診療報酬"]["match_name"], match["診療報酬"]["overlap"], match["診療報酬"]["jaccard"]) == ("会計処理", 3, 1.0)
    assert (match["受付"]["match_name"], match["受付"]["jaccard"]) == ("受付処理", 0.75)
    leaf = world["result"]["agreement"]["leaf"]
    expected = metrics.ari(["t1/-", "t1/1", "t2", "t2", "t2"], ["b", "b", "r", "r", "r"])
    assert leaf["files"] == 5 and leaf["ari"] == pytest.approx(expected, abs=1e-6)


def test_directory_alignment(world):
    d = world["result"]["directory_alignment"]
    assert d["llm"] == {"files": 5, "ami_top_directory": 1.0, "ami_package": 1.0}
    assert d["leiden"] == {"files": 6, "ami_top_directory": 1.0, "ami_package": 1.0}


def test_structure_on_the_shared_atom_graph(world):
    s = world["result"]["structure"]
    assert s["atoms_in_multi_topic_files"] == 2   # Ledger and ledger.go's <package>
    assert (s["atoms_without_llm_topic"], s["atoms_unassigned_by_leiden"]) == (0, 0)
    assert s["shared_atoms"] == 10
    assert s["llm"]["intra_weight_share"] == pytest.approx(8 / 9, abs=1e-6)
    for key in ("modularity", "intra_weight_share", "mean_conductance", "topics"):
        assert s["llm"][key] == s["leiden"][key]
    assert s["leiden_full_graph_modularity"] == world["data"]["metrics"]["modularity"]


def test_stability_cost_and_grounding(world):
    r = world["result"]
    assert r["stability"]["leiden"] == {"seed_ari": world["data"]["seed_ari"], "rerun_hash_equal": True}
    assert r["stability"]["llm"]["files"] == 5    # ledger.go has two topics in the first run
    assert r["stability"]["llm"]["ari"] == pytest.approx(
        metrics.ari(["t1", "t1", "t2", "t2", "t2"], ["x", "x", "x", "y", "y"]), abs=1e-6)
    assert r["cost"]["llm"]["total"] == {"calls": 5, "input_tokens": 140, "output_tokens": 80, "seconds": 3.5}
    assert r["cost"]["leiden"]["naming"]["calls"] == 1
    assert r["grounding"]["llm"]["rate"] == 0.5 and r["grounding"]["llm"]["top_missing"] == ["Ghost"]
    assert r["grounding"]["leiden"]["rate"] == 1.0


def test_feature_recovery(world):
    feats = {f["name"]: f for f in world["result"]["features"]["features"]}
    acc = feats["会計"]
    assert acc["files"] == 3 and acc["not_production_or_missing"] == 1
    assert acc["llm_top"] == {"coverage": 1.0, "best": "診療報酬", "dispersion": 1, "irrelevant": 0.0}
    assert acc["llm_leaf"]["coverage"] == pytest.approx(2 / 3, abs=1e-6) and acc["llm_leaf"]["dispersion"] == 2
    assert acc["leiden_top"] == {"coverage": 1.0, "best": "会計処理", "dispersion": 1, "irrelevant": 0.0}
    rec = feats["受付"]
    assert rec["llm_top"]["irrelevant"] == 0.25 and rec["leiden_top"]["irrelevant"] == 0.0
    summary = world["result"]["features"]["summary"]
    assert summary["leiden_top"]["mean_coverage"] == 1.0 and summary["llm_top"]["uncoverable"] == 0


def test_render_markdown_shows_every_group(world):
    md = compare.render_markdown(world["result"])
    for needle in ("# Topic comparison", "## Coverage", "## Size", "## Agreement", "## Directory alignment",
                   "## Structural quality on the shared atom graph", "## Stability", "## Cost", "## Grounding",
                   "## Feature recovery", "| 診療報酬 | 3 | 会計処理 |", "`billing/ghost.go`",
                   "No single metric picks a winner"):
        assert needle in md


def test_empty_llm_tree_and_missing_extras():
    (nodes, edges), _t = rows()
    data = detect(build_projection(nodes, edges), {"band": (2, 4)}).data
    result = compare.compare_topics([], data, nodes, edges)
    assert result["coverage"]["llm"]["files"] == 0
    assert result["agreement"]["top"]["ami"] is None
    assert result["stability"]["llm"] is None and result["features"] is None
    assert result["grounding"] == {"llm": None, "leiden": None}
    assert "## Feature recovery" not in compare.render_markdown(result)


def test_hash_mismatches():
    a = {"x.go": {"sha256": "1"}, "y.go": {"sha256": "2"}}
    assert compare.hash_mismatches(a, dict(a)) == []
    assert compare.hash_mismatches(a, {"x.go": {"sha256": "1"}, "y.go": {"sha256": "3"}, "z.go": {}}) == ["y.go", "z.go"]


def test_a_feature_no_topic_touches_has_zero_coverage():
    (nodes, edges), _t = rows()
    data = detect(build_projection(nodes, edges), {"band": (2, 4)}).data
    tree = [{"id": "t", "name": "受付だけ", "files": RECEPTION, "subtopics": []}]
    result = compare.compare_topics(tree, data, nodes, edges,
                                    features={"features": [{"name": "会計", "files": BILLING}]})
    (row,) = result["features"]["features"]
    assert row["llm_top"] == {"coverage": 0.0, "best": None, "dispersion": None, "irrelevant": None}
    assert row["leiden_top"]["coverage"] == 1.0


def test_no_implements_ablation_is_reported(world):
    ab = world["result"]["ablation"]
    assert ab["profile"] == "no_implements" and ab["atoms_compared"] == 12
    assert (ab["ami_vs_reported"], ab["ari_vs_reported"]) == (1.0, 1.0)   # these rows have no implements edges
    assert "## Ablation: Leiden without implements edges" in compare.render_markdown(world["result"])


def test_ablation_can_be_skipped_or_is_moot():
    (nodes, edges), _t = rows()
    data = detect(build_projection(nodes, edges), {"band": (2, 4)}).data
    assert compare.compare_topics(LLM_TREE, data, nodes, edges, ablation=False)["ablation"] is None
    moot = detect(build_projection(nodes, edges, "calls_only"), {"band": (2, 4)}).data
    ab = compare.compare_topics(LLM_TREE, moot, nodes, edges)["ablation"]
    assert ab == {"profile": None, "reason": "the reported profile already has no implements edges"}
    assert "already has no implements edges" in compare.render_markdown(
        compare.compare_topics(LLM_TREE, moot, nodes, edges))


def test_unassigned_atoms_count_as_uncovered():
    (nodes, edges), _t = rows()
    lonely_nodes = {k: list(v) for k, v in nodes.items()}
    extra = RowBuilder()
    extra.type("dto/lonely.go", "Lonely")
    for table, extra_rows in extra.nodes.items():
        lonely_nodes[table] = lonely_nodes.get(table, []) + extra_rows
    data = detect(build_projection(lonely_nodes, edges), {"band": (2, 4)}).data
    assert data["unassigned_topic"]
    result = compare.compare_topics(LLM_TREE, data, lonely_nodes, edges, ablation=False)
    assert result["inputs"]["leiden_topics"] == 2
    cov = result["coverage"]["leiden"]
    assert (cov["files"], cov["files_total"], cov["atoms"], cov["atoms_total"]) == (6, 7, 12, 14)
    assert result["structure"]["atoms_unassigned_by_leiden"] == 2


def test_build_excluded_files_are_not_production_files():
    (nodes, edges), _t = rows()
    extra = RowBuilder()
    extra.file("billing/fee_windows.go", build_included=False)
    extra.type("billing/fee_windows.go", "WinFee")
    nodes = {k: list(v) + extra.nodes.get(k, []) for k, v in nodes.items()}
    data = detect(build_projection(nodes, edges), {"band": (2, 4)}).data
    result = compare.compare_topics(LLM_TREE, data, nodes, edges, ablation=False)
    assert result["inputs"]["production_files"] == 6 and result["inputs"]["eligible_atoms"] == 12


def test_runs_must_have_seen_the_same_input():
    (nodes, edges), _t = rows()
    data = detect(build_projection(nodes, edges), {"band": (2, 4)}).data
    run = {"entities": {"billing/fee.go": {"sha256": "1"}, "billing/points.go": "2"},
           "build": {"goos": "linux", "goarch": "amd64"}, "mode": "offline", "resolutions_hash": "r1"}
    ok = compare.compare_topics(LLM_TREE, data, nodes, edges, ablation=False, llm_run=run, leiden_run=dict(run))
    assert ok["inputs"]["runs_verified"] is True
    assert "| Same files, build configuration, mode and resolutions (checked) | yes |" in compare.render_markdown(ok)
    other = {**run, "entities": {"billing/fee.go": "9", "billing/points.go": "2"}, "mode": "download"}
    with pytest.raises(compare.CompareInputError, match="1 files differ: billing/fee.go; mode differs"):
        compare.compare_topics(LLM_TREE, data, nodes, edges, ablation=False, llm_run=run, leiden_run=other)
    with pytest.raises(ValueError, match="both"):
        compare.compare_topics(LLM_TREE, data, nodes, edges, ablation=False, llm_run=run)
    assert compare.run_mismatches(run, {**run, "build": {"goos": "darwin"}, "resolutions_hash": "r2"}) == [
        "build differs: {'goos': 'linux', 'goarch': 'amd64'} vs {'goos': 'darwin'}",
        "resolutions_hash differs: 'r1' vs 'r2'"]


def test_grounding_counts_each_miss_once_on_pages_with_their_check(world):
    from graph_generator.topics import reports
    names = reports.build_name_set(world["nodes"])
    page = "`Fee` と `Ghost`"
    checked = reports.append_grounding(page, reports.grounding_check(page, names))
    (nodes, edges), _t = rows()
    result = compare.compare_topics(LLM_TREE, world["data"], nodes, edges, ablation=False,
                                    llm_pages={"a.md": page}, leiden_pages={"a.md": checked})
    assert result["grounding"]["llm"] == result["grounding"]["leiden"]
    assert result["grounding"]["leiden"]["checked"] == 2 and result["grounding"]["leiden"]["rate"] == 0.5


def test_both_runs_topic_names_are_skipped_on_every_page(world):
    (nodes, edges), _t = rows()
    leiden_name = world["names"][world["tid"]["billing"]]           # 会計処理
    pages = {"a.md": f"`{leiden_name}` と `診療報酬` は `Fee` を使う。"}  # a Leiden name and an LLM name
    result = compare.compare_topics(LLM_TREE, world["data"], nodes, edges, ablation=False,
                                    leiden_names=world["names"], llm_pages=pages, leiden_pages=pages)
    for method in ("llm", "leiden"):
        assert (result["grounding"][method]["checked"], result["grounding"][method]["rate"]) == (1, 1.0)
    assert {"診療報酬", "点数", "受付", leiden_name} <= compare.topic_names(LLM_TREE, world["data"], world["names"])
