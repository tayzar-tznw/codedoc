"""community: Leiden detection, resolution rule, post-processing, level 2,
identity, carry-over, determinism and the backend contract (leiden spec
§3.3–3.6, C12)."""

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import types
import warnings

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from graph_generator.topics import community  # noqa: E402
from graph_generator.topics.community import (Params, carry_over, check_integrity,  # noqa: E402
                                              choose_resolution, detect, membership_hash, merge_thin)
from graph_generator.topics.graph_builder import build_projection  # noqa: E402
from graph_generator.topics.metrics import adjacency  # noqa: E402
from graph_generator.topics.synthetic import RowBuilder, synthetic_repo  # noqa: E402

BAND_2 = {"band": (2, 4)}


class Repo:
    """RowBuilder with named types and `link(x, y, n)` = n distinct method pairs."""

    def __init__(self):
        self.b = RowBuilder()
        self.t: dict[str, str] = {}
        self._n = 0

    def type(self, code, path, **kw):
        self.t[code] = self.b.type(path, code, **kw)
        return self.t[code]

    def link(self, x, y, n=1):
        for _ in range(n):
            self._n += 1
            self.b.call(self.b.method(self.t[x], f"m{self._n}"), self.b.method(self.t[y], f"m{self._n}"))

    def projection(self, profile="strict"):
        return build_projection(*self.b.rows(), profile=profile)


def worked_example(extra=()):
    """Codex §8.5: weights 2 inside each feature (3 method pairs), 1 between
    the services; each type in its own file under its layer's directory."""
    r = Repo()
    for code, path in (("IC", "Controllers/invoice.go"), ("IS", "Services/invoice.go"),
                       ("IR", "Repositories/invoice.go"), ("OC", "Controllers/order.go"),
                       ("OS", "Services/order.go"), ("OR", "Repositories/order.go")):
        r.type(code, path)
    for x, y in (("IC", "IS"), ("IS", "IR"), ("IC", "IR"), ("OC", "OS"), ("OS", "OR"), ("OC", "OR")):
        r.link(x, y, 3)
    r.link("IS", "OS", 1)
    for code in extra:
        r.type(code, f"Services/{code.lower()}.go")
        r.link(code, "IS", 3)
        r.link(code, "IC", 3)
    return r


def _primary_sets(data, level="1"):
    return sorted(sorted(a for a, role in t["roles"].items() if role == "primary")
                  for t in data["levels"][level].values())


def test_worked_example_finds_the_two_features():
    r = worked_example()
    proj = r.projection()
    c = detect(proj, BAND_2)
    data = c.data
    t = r.t
    assert _primary_sets(data) == sorted([sorted([t["IC"], t["IS"], t["IR"]]), sorted([t["OC"], t["OS"], t["OR"]])])
    assert data["resolution_rule"] == "plateau"
    assert data["resolution"] == [s["resolution"] for s in data["sweep"] if s["count"] == 2][-1]  # largest γ (L-R43)
    assert data["metrics"]["modularity"] == pytest.approx(11 / 26, abs=1e-6)
    assert set(data["levels"]["2"]) == set(data["levels"]["1"]) == set(data["leaves"])  # carried (L-R9)
    assert all(t["carried"] for t in data["levels"]["2"].values())
    assert data["unassigned_topic"] is None and data["unassigned"] == []
    assert check_integrity(c, proj) == []


def test_document_records_backend_params_sweep_and_trials():
    proj = worked_example().projection()
    data = detect(proj, BAND_2).data
    assert data["version"] == 1
    assert data["backend"] == {"name": "graspologic-native", "version": community.backend_version(),
                               "function": "leiden"}
    assert data["params"]["seeds"] == [42, 43, 44, 45, 46]
    assert (data["params"]["randomness"], data["params"]["iterations"]) == (0.01, 10)
    assert [s["resolution"] for s in data["sweep"]] == [0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5]
    assert all(len(s["trials"]) == 5 for s in data["sweep"])
    assert [tr["seed"] for tr in data["trials"]] == [42, 43, 44, 45, 46]
    assert data["seed_ari"] == pytest.approx(1.0)
    assert data["atoms"] == len(proj.atoms) and data["edges"] == len(proj.edges)
    assert data["projection_hash"] == proj.hash
    assert data["profile"] == "strict"


def test_backend_is_the_pinned_version():
    assert community.backend_version() == community.TESTED_BACKEND_VERSION


def test_missing_backend_stops_with_an_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "graspologic_native", None)
    with pytest.raises(community.BackendMissingError, match="graspologic-native") as err:
        detect(worked_example().projection())
    assert "uv pip install" in str(err.value) and "never falls back" in str(err.value)
    empty = RowBuilder().rows()
    with pytest.raises(community.BackendMissingError):
        detect(build_projection(*empty))


def test_atoms_of_a_file_follow_its_type_as_attached_file():
    r = worked_example()
    data = detect(r.projection(), BAND_2).data
    invoice = next(t for t in data["levels"]["1"].values() if r.t["IC"] in t["members"])
    pkg = "class:Controllers/invoice.go:<package>"
    assert invoice["roles"][pkg] == "attached-file"
    assert invoice["size"] == 6 and invoice["file_count"] == 3


def test_isolated_type_alone_in_its_file_is_unassigned():
    r = worked_example()
    lonely = r.type("Lonely", "dto/lonely.go")
    data = detect(r.projection(), BAND_2).data
    assert data["unassigned_reasons"][lonely] == "isolated"
    assert data["unassigned_reasons"]["class:dto/lonely.go:<package>"] == "isolated"


def test_isolated_type_attaches_to_the_community_of_its_file():
    r = worked_example()
    dto = r.b.type("Services/invoice.go", "InvoiceDTO")
    data = detect(r.projection(), BAND_2).data
    invoice = next(t for t in data["levels"]["1"].values() if r.t["IS"] in t["members"])
    assert invoice["roles"][dto] == "attached-file"


def test_excluded_atoms_attach_by_weight_with_their_role():
    r = worked_example()
    r.b.file("Services/invoice_test.go", origin="test")
    fixture = r.type("invoiceFixture", "Services/invoice_test.go")
    r.b.file("gen/order.pb.go", generated=True)
    msg = r.type("OrderMsg", "gen/order.pb.go")
    r.link("invoiceFixture", "IS", 2)
    r.link("OS", "OrderMsg", 1)
    unused = r.b.type("Services/invoice_test.go", "unusedHelper")
    data = detect(r.projection(), BAND_2).data
    roles = {a: (tid, role) for tid, t in data["levels"]["1"].items() for a, role in t["roles"].items()}
    assert roles[fixture][1] == "attached-test" and roles[fixture][0] == roles[r.t["IS"]][0]
    assert roles[msg][1] == "attached-generated" and roles[msg][0] == roles[r.t["OS"]][0]
    assert data["unassigned_reasons"][unused] == "test"
    assert data["metrics"]["roles"]["attached-test"] == 1


def test_merge_thin_moves_small_groups_to_the_strongest_neighbour():
    adj = adjacency([("a", "b", 1), ("b", "c", 1), ("a", "c", 1), ("c", "d", 1),
                     ("e", "f", 1), ("f", "g", 1), ("h", "e", 1.0), ("h", "a", 0.5),
                     ("x", "y", 3.0)])
    groups, dropped = merge_thin([["a", "b", "c", "d"], ["e", "f", "g"], ["h"], ["x", "y"]], adj, 3)
    assert sorted(map(sorted, groups)) == [["a", "b", "c", "d"], ["e", "f", "g", "h"]]
    assert dropped == ["x", "y"]


def test_merge_thin_chains_and_tie_breaks():
    adj = adjacency([("p", "q", 1), ("q", "a", 1), ("a", "b", 1), ("b", "c", 1)])
    groups, dropped = merge_thin([["a", "b", "c"], ["p"], ["q"]], adj, 3)
    assert sorted(map(sorted, groups)) == [["a", "b", "c", "p", "q"]] and dropped == []
    adj = adjacency([("s", "a", 1), ("s", "e", 1), ("a", "b", 1), ("b", "c", 1), ("e", "f", 1), ("f", "g", 1)])
    groups, _ = merge_thin([["a", "b", "c"], ["e", "f", "g"], ["s"]], adj, 3)
    winner = min((["a", "b", "c"], ["e", "f", "g"]), key=membership_hash)  # equal weight (L-R9)
    assert sorted(winner + ["s"]) in sorted(map(sorted, groups))
    groups, dropped = merge_thin([["a", "b", "c"], ["z"]], {}, 3)
    assert sorted(map(sorted, groups)) == [["a", "b", "c"]] and dropped == ["z"]


def test_isolated_pair_is_a_thin_community_without_neighbour():
    r = worked_example()
    r.type("PairA", "pair/a.go")
    r.type("PairB", "pair/b.go")
    r.link("PairA", "PairB", 3)
    data = detect(r.projection(), BAND_2).data
    assert data["unassigned_reasons"][r.t["PairA"]] == "thin"
    assert data["unassigned_reasons"][r.t["PairB"]] == "thin"
    assert data["unassigned_reasons"]["class:pair/a.go:<package>"] == "isolated"


def blocks_repo(super_features=2, block=7, dense_clique=False):
    """Super-features of two dense blocks (weight 2 per pair) joined by five
    pairs; super-features joined by one call. With `dense_clique` the second
    super-feature is one clique instead."""
    r = Repo()
    names = []
    for s in range(super_features):
        blocks = []
        for k in range(2):
            codes = [f"S{s}B{k}T{i}" for i in range(block)]
            for code in codes:
                r.type(code, f"s{s}/b{k}/{code.lower()}.go")
            blocks.append(codes)
        if dense_clique and s == 1:
            members = blocks[0] + blocks[1]
            for i, x in enumerate(members):
                for y in members[i + 1:]:
                    r.link(x, y, 3)
        else:
            for codes in blocks:
                for i, x in enumerate(codes):
                    for y in codes[i + 1:]:
                        r.link(x, y, 3)
            for i in range(5):
                r.link(blocks[0][i], blocks[1][i], 3)
        names.append(blocks)
    r.link(names[0][0][0], names[1][0][0], 1)
    return r, names


LOW = {"resolutions": (0.1,), "band": (1, 30)}


def test_level2_split_is_accepted_for_two_blocks():
    r, names = blocks_repo()
    proj = r.projection()
    c = detect(proj, LOW)
    data = c.data
    assert len(data["levels"]["1"]) == 2
    expected = sorted(sorted(r.t[x] for x in codes) for blocks in names for codes in blocks)
    assert _primary_sets(data, "2") == expected
    for tid, t in data["levels"]["1"].items():
        assert t["split"]["accepted"] is True and t["split"]["raw_sizes"] == [7, 7]
        assert len(t["children"]) == 2
        union = sorted(a for ch in t["children"] for a in data["levels"]["2"][ch]["members"])
        assert union == t["members"]
        for ch in t["children"]:
            child = data["levels"]["2"][ch]
            assert child["parent"] == tid and child["level"] == 2
            assert all(child["roles"][a] == t["roles"][a] for a in child["members"])
    assert sorted(data["leaves"]) == sorted(data["levels"]["2"])
    assert check_integrity(c, proj) == []


def test_level2_split_is_rejected_for_a_clique_and_skipped_for_small_topics():
    r, _names = blocks_repo(dense_clique=True)
    data = detect(r.projection(), LOW).data
    splits = sorted((t["split"]["accepted"], t["split"]["attempted"]) for t in data["levels"]["1"].values())
    assert splits == [(False, True), (True, True)]
    rejected = next(t for t in data["levels"]["1"].values() if not t["split"]["accepted"])
    assert rejected["children"] == [] and "child" in rejected["split"]["reason"]
    small = detect(worked_example().projection(), BAND_2).data
    assert all(t["split"]["attempted"] is False for t in small["levels"]["1"].values())


def test_topic_ids_follow_level_and_membership():
    proj = worked_example().projection()
    data = detect(proj, {**BAND_2, "repo": "iryo", "id_prefix": "cd"}).data
    for level, topics in data["levels"].items():
        for tid, t in topics.items():
            primary = sorted(a for a, r in t["roles"].items() if r == "primary")
            assert len(primary) < len(t["members"])  # attached <package> atoms are not hashed (L-R9)
            mhash = hashlib.sha256("|".join(primary).encode("utf-8")).hexdigest()[:16]
            raw = "\x00".join(("topic", "iryo", f"L{t['level']}", mhash))
            assert t["membership_hash"] == mhash == membership_hash(primary)
            assert tid == "cd_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert data["repo"] == "iryo"


def test_repo_defaults_to_the_rows_repo_column():
    data = detect(worked_example().projection(), BAND_2).data
    assert data["repo"] == "iryo"


def test_three_runs_give_the_same_membership_hash_and_document():
    nodes, edges, _f = synthetic_repo(400, seed=3)
    runs = [detect(build_projection(nodes, edges), {"repo": "synth"}).data for _ in range(3)]
    assert len({r["membership_hash"] for r in runs}) == 1
    assert len({json.dumps(r, sort_keys=True) for r in runs}) == 1


def test_membership_hash_is_the_same_across_processes():
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        from graph_generator.topics import synthetic, graph_builder, community
        nodes, edges, _ = synthetic.synthetic_repo(300, seed=5)
        print(community.detect(graph_builder.build_projection(nodes, edges)).data["membership_hash"])
    """)
    hashes = set()
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
        out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)
        hashes.add(out.stdout.strip())
    nodes, edges, _ = synthetic_repo(300, seed=5)
    hashes.add(detect(build_projection(nodes, edges)).data["membership_hash"])
    assert len(hashes) == 1


def test_carry_over_inherits_at_jaccard_06_and_keeps_unchanged_ids():
    before = detect(worked_example().projection(), BAND_2).data
    r = worked_example(extra=("IX",))
    after = detect(r.projection(), {**BAND_2, "jaccard": 0.6}, previous=before).data
    old = {tuple(sorted(t["members"])): tid for tid, t in before["levels"]["1"].items()}
    for tid, t in after["levels"]["1"].items():
        if r.t["OS"] in t["members"]:
            assert tid in before["levels"]["1"]
            assert (t["inherited_from"], t["jaccard"]) == (tid, 1.0)
        else:
            prev_id = next(v for k, v in old.items() if r.t["IS"] in k)
            assert tid != prev_id
            assert t["inherited_from"] == prev_id
            assert t["jaccard"] == pytest.approx(6 / 8)  # 6 old members + IX and its file's <package>
    assert after["carry_over"]["levels"]["1"] == {"inherited": 2, "new": 0, "retired": []}
    assert after["carry_over"]["previous_membership_hash"] == before["membership_hash"]


def test_carry_over_below_threshold_is_new():
    matches, summary = carry_over({"1": {"new1": {"a", "b", "c"}, "new2": {"x", "y"}}},
                                  {"levels": {"1": {"old1": {"members": ["a", "b", "z", "w"]},
                                                    "old2": {"members": ["x", "y"]}}}}, 0.6)
    assert matches == {"new2": ("old2", 1.0)}
    assert summary["levels"]["1"] == {"inherited": 1, "new": 1, "retired": ["old1"]}
    assert carry_over({"1": {}}, None, 0.6) == ({}, {})


def test_choose_resolution_plateau_and_closest():
    assert choose_resolution([3, 6, 6, 6, 9, 9, 30], (5, 25)) == (3, "plateau")  # the plateau's largest γ (L-R43)
    assert choose_resolution([6, 6, 9, 9, 12], (5, 25)) == (1, "plateau")      # equal runs: the earlier one
    assert choose_resolution([2, 7, 9, 9, 9, 7], (5, 25)) == (4, "plateau")
    assert choose_resolution([1, 2, 3, 4], (5, 25)) == (3, "closest")
    assert choose_resolution([28, 50, 91], (5, 25)) == (0, "closest")
    assert choose_resolution([3, 27], (5, 25)) == (0, "closest")              # equal distance: smaller γ


def test_hub_is_the_highest_pagerank_member():
    r = Repo()
    for code in ("Hub", "L1", "L2", "L3", "L4"):
        r.type(code, f"star/{code.lower()}.go")
    for leaf in ("L1", "L2", "L3", "L4"):
        r.link("Hub", leaf, 2)
    r.link("L1", "L2", 1)
    data = detect(r.projection(), {"band": (1, 5)}).data
    (topic,) = data["levels"]["1"].values()
    assert topic["hub"] == r.t["Hub"] and topic["ranked"][0] == r.t["Hub"]
    assert topic["label"] == "star.Hub" and topic["label_source"] == "hub"


def test_topic_dependencies_and_links():
    r = worked_example()
    proj = r.projection()
    data = detect(proj, BAND_2).data
    tid = {code: next(t for t, v in data["levels"]["1"].items() if r.t[code] in v["members"]) for code in ("IS", "OS")}
    assert data["topic_edges"] == [{"source": tid["IS"], "target": tid["OS"], "weight": 1.0, "call_count": 1}]
    assert community.topic_links(data, proj, 1) == {tuple(sorted(tid.values())): 1.0}
    assert community.neighbours(data, proj, 1)[tid["IS"]] == [(tid["OS"], 1.0)]


def test_no_edges_puts_every_atom_in_unassigned():
    r = Repo()
    r.type("A", "a/a.go")
    r.type("B", "b/b.go")
    proj = r.projection()
    c = detect(proj)
    assert c.data["resolution_rule"] == "no-edges" and c.data["sweep"] == []
    uid = c.data["unassigned_topic"]
    assert list(c.data["levels"]["1"]) == [uid] and list(c.data["levels"]["2"]) == [uid]
    assert c.data["levels"]["1"][uid]["members"] == sorted(proj.info) == sorted(c.data["unassigned"])
    assert c.data["leaves"] == [] and c.data["metrics"]["topics"] == {"1": 0, "2": 0}
    assert check_integrity(c, proj) == []


class PanicException(BaseException):
    """Stands in for pyo3's PanicException, which is not an Exception."""


class FakeBackend:
    """Wraps the real backend; `fail[seed]` raises, `script[seed]` overrides
    membership, `skew` shifts the reported modularity."""

    def __init__(self, fail=None, script=None, skew=0.0):
        import graspologic_native
        self.real, self.fail, self.script, self.skew = graspologic_native, fail or {}, script or {}, skew
        self.calls = 0

    def leiden(self, edges, **kw):
        self.calls += 1
        if kw["seed"] in self.fail:
            raise self.fail[kw["seed"]]
        if kw["seed"] in self.script:
            return 0.0, dict(self.script[kw["seed"]])
        return self.real.leiden(edges, **kw)

    def modularity(self, edges, membership, resolution=1.0):
        return self.real.modularity(edges, membership, resolution=resolution) + self.skew


@pytest.mark.parametrize("error", [RuntimeError("boom"), PanicException("panicked at src/lib.rs")])
def test_any_backend_failure_stops_4a_with_a_clear_error(monkeypatch, error):
    monkeypatch.setattr(community, "_backend", lambda: FakeBackend(fail={43: error}))
    with pytest.raises(community.CommunityError, match="graspologic-native failed at resolution .*, seed 43") as err:
        detect(worked_example().projection(), {**BAND_2, "workers": 1})
    assert isinstance(err.value, Exception) and err.value.__cause__ is error


def test_backend_failure_in_a_thread_stops_4a_too(monkeypatch):
    monkeypatch.setattr(community, "_backend", lambda: FakeBackend(fail={45: PanicException("x")}))
    with pytest.raises(community.CommunityError, match="seed 45"):
        detect(worked_example().projection(), {**BAND_2, "workers": 4})


def test_incomplete_membership_stops_4a(monkeypatch):
    proj = worked_example().projection()
    partial = {a: 0 for a in sorted({x for u, v, _ in proj.edges for x in (u, v)})[:-1]}
    monkeypatch.setattr(community, "_backend", lambda: FakeBackend(script={42: partial}))
    with pytest.raises(community.CommunityError, match="labels for 6 nodes"):
        detect(proj, BAND_2)


def test_a_modularity_check_mismatch_warns_and_never_stops(monkeypatch):
    plain = detect(worked_example().projection(), BAND_2).data
    monkeypatch.setattr(community, "_backend", lambda: FakeBackend(skew=1e-5))
    with pytest.warns(RuntimeWarning, match="modularity check: graspologic-native"):
        data = detect(worked_example().projection(), BAND_2).data
    assert len(data["warnings"]) == 35 and plain["warnings"] == []
    for tr in data["trials"]:
        assert tr["modularity"] == pytest.approx(tr["modularity_check"] + 1e-5, abs=1e-6)  # backend value selects
        assert tr["modularity_delta"] == pytest.approx(1e-5, rel=1e-3)
    assert data["levels"].keys() == plain["levels"].keys() and set(data["levels"]["1"]) == set(plain["levels"]["1"])


def test_small_differences_do_not_warn(monkeypatch):
    monkeypatch.setattr(community, "_backend", lambda: FakeBackend(skew=1e-8))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert detect(worked_example().projection(), BAND_2).data["warnings"] == []


def test_backend_and_own_modularity_agree_to_1e9_on_small_graphs():
    r, _names = blocks_repo()
    for data in (detect(worked_example().projection(), BAND_2).data, detect(r.projection(), LOW).data,
                 detect(build_projection(*synthetic_repo(300, seed=4)[:2])).data):
        trials = [tr for s in data["sweep"] for tr in s["trials"]]
        trials += [tr for t in data["levels"]["1"].values() for tr in t.get("split", {}).get("trials", [])]
        assert trials and all(tr["modularity_delta"] <= 1e-9 for tr in trials)


def test_backend_is_never_called_without_edges(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(community, "_backend", lambda: fake)
    r = Repo()
    r.type("A", "a/a.go")
    detect(r.projection())
    assert fake.calls == 0


def test_invalid_weights_never_reach_the_backend():
    proj = worked_example().projection()
    u, v, _w = proj.edges[0]
    for bad in (float("nan"), 0.0, -1.0, float("inf")):
        broken = dataclasses.replace(proj, edges=[(u, v, bad)] + proj.edges[1:])
        with pytest.raises(community.CommunityError, match="invalid edge"):
            detect(broken, BAND_2)


def test_equal_modularity_ties_go_to_the_smallest_membership_hash(monkeypatch):
    r = Repo()
    for code in ("A", "B", "C", "D"):
        r.type(code, f"ring/{code.lower()}.go")
    for x, y in (("A", "B"), ("B", "C"), ("C", "D"), ("D", "A")):
        r.link(x, y, 1)
    t = r.t
    p1 = {t["A"]: 0, t["B"]: 0, t["C"]: 1, t["D"]: 1}
    p2 = {t["A"]: 0, t["D"]: 0, t["B"]: 1, t["C"]: 1}
    script = {42: p1, 43: p2, 44: p1, 45: p2, 46: p1}
    monkeypatch.setattr(community, "_backend", lambda: FakeBackend(script=script))
    data = detect(r.projection(), {"resolutions": (1.0,), "band": (1, 4), "min_size": 2}).data
    hashes = {tr["membership_hash"] for tr in data["trials"]}
    assert len(hashes) == 2
    assert len({tr["modularity"] for tr in data["trials"]}) == 1
    assert data["sweep"][0]["membership_hash"] == min(hashes)


def test_check_integrity_reports_broken_documents():
    proj = worked_example().projection()
    data = json.loads(json.dumps(detect(proj, BAND_2).data))
    tid, topic = next(iter(data["levels"]["1"].items()))
    victim = next(a for a, r in topic["roles"].items() if r == "primary")
    topic["members"].remove(victim)
    del topic["roles"][victim]
    problems = check_integrity(data, proj)
    assert any(f"level 1: atom {victim} appears 0 times" == p for p in problems)
    assert any("membership_hash" in p for p in problems)
    assert any("carried topic differs" in p for p in problems)
    data["leaves"].append("nope")
    assert "leaves differ from the level-2 frontier" in check_integrity(data, proj)
    data2 = json.loads(json.dumps(detect(proj, BAND_2).data))
    data2["levels"]["1"][tid]["roles"][victim] = "unassigned"
    assert any("does not fit the topic" in p for p in check_integrity(data2, proj))


def test_params_validation():
    assert Params.of({"seeds": [1, 2], "resolutions": [0.5, 1.0], "band": [3, 9]}).band == (3, 9)
    for bad in ({"seeds": []}, {"seeds": (1, 1)}, {"resolutions": (1.0, 0.5)}, {"resolutions": (0.0,)},
                {"band": (9, 3)}, {"min_size": 0}):
        with pytest.raises(ValueError):
            Params.of(bad)
    with pytest.raises(TypeError):
        Params.of({"unknown": 1})


def test_write_is_atomic_json(tmp_path):
    c = detect(worked_example().projection(), BAND_2)
    path = tmp_path / "topics" / "communities.json"
    c.write(str(path))
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(c.data))
    assert not (tmp_path / "topics" / "communities.json.tmp").exists()
    assert set(c.timings) == {"sweep", "post", "total"}


def test_summary_line_and_views():
    data = detect(worked_example().projection(), BAND_2).data
    assert "2 topics, 0 subtopics" in community.summary_line(data)
    assert set(community.leaf_map(data)) == set(community.level_map(data, 1))
    assert set(community.all_topics(data)) == set(data["levels"]["1"])


def test_fake_backend_object_shape_is_the_real_one():
    gn = community._backend()
    assert isinstance(gn, types.ModuleType) and callable(gn.leiden)


def test_atoms_from_several_repos_need_an_explicit_repo():
    r = worked_example()
    r.b.nodes["Classes"][0]["repo"] = "other"
    with pytest.raises(ValueError, match="several repos"):
        detect(r.projection(), BAND_2)
    assert detect(r.projection(), {**BAND_2, "repo": "iryo"}).data["repo"] == "iryo"


def test_thread_count_never_changes_the_document():
    nodes, edges, _f = synthetic_repo(300, seed=9)
    proj = build_projection(nodes, edges)
    one = detect(proj, {"workers": 1}).data
    many = detect(proj, {"workers": 6}).data
    assert json.dumps(one, sort_keys=True) == json.dumps(many, sort_keys=True)
    assert "workers" not in one["params"]


def test_thin_leftovers_are_attached_by_file_like_isolates():
    r = worked_example()
    pair_a = r.b.type("Services/invoice.go", "PairA")   # same file as IS
    r.t["PairA"] = pair_a
    r.type("PairB", "pair/b.go")
    r.link("PairA", "PairB", 3)
    data = detect(r.projection(), BAND_2).data
    invoice = next(t for t in data["levels"]["1"].values() if r.t["IS"] in t["members"])
    assert invoice["roles"][pair_a] == "attached-file"
    assert data["unassigned_reasons"][r.t["PairB"]] == "thin"


def test_build_excluded_atoms_are_attached_as_excluded():
    r = worked_example()
    r.b.file("Services/invoice_windows.go", build_included=False)
    win = r.type("WinInvoice", "Services/invoice_windows.go")
    r.link("WinInvoice", "IS", 2)
    proj = r.projection()
    assert proj.excluded[win] == "build"
    data = detect(proj, BAND_2).data
    invoice = next(t for t in data["levels"]["1"].values() if r.t["IS"] in t["members"])
    assert invoice["roles"][win] == "attached-excluded"


def test_attached_members_do_not_change_topic_ids():
    before = detect(worked_example().projection(), BAND_2).data
    r = worked_example()
    r.b.file("Services/invoice_test.go", origin="test")
    r.type("invoiceFixture", "Services/invoice_test.go")
    r.link("invoiceFixture", "IS", 2)
    after = detect(r.projection(), BAND_2).data
    real = set(after["levels"]["1"]) - {after["unassigned_topic"]}  # the test file's <package> is unassigned
    assert real == set(before["levels"]["1"])                        # primary-only hash (L-R9)
    assert after["membership_hash"] != before["membership_hash"]      # the document hash sees roles


def test_unassigned_topic_is_one_fixed_level1_topic_carried_to_level2():
    r = worked_example()
    lonely = r.type("Lonely", "dto/lonely.go")
    proj = r.projection()
    data = detect(proj, {**BAND_2, "repo": "iryo", "id_prefix": "cd"}).data
    uid = data["unassigned_topic"]
    assert uid == community.unassigned_topic_id("iryo", "cd")
    u = data["levels"]["1"][uid]
    assert u["unassigned"] is True and u["label"] == "未分類" and u["label_source"] == "fixed"
    assert u["members"] == sorted([lonely, "class:dto/lonely.go:<package>"]) == data["unassigned"]
    assert set(u["roles"].values()) == {"unassigned"} and u["hub"] is None
    assert data["levels"]["2"][uid]["carried"] is True
    assert uid not in data["leaves"] and uid not in community.all_topics(data)
    assert uid in community.all_topics(data, include_unassigned=True)
    assert lonely not in community.leaf_map(data) and community.leaf_map(data, True)[lonely] == uid
    assert check_integrity(data, proj) == []


def test_split_topics_are_not_carried():
    r, _names = blocks_repo()
    data = detect(r.projection(), LOW).data
    assert not any(t.get("carried") for t in data["levels"]["2"].values())
    assert data["metrics"]["topics"] == {"1": 2, "2": 4} and data["metrics"]["leaves"] == 4


def test_groups_mark_partial_topics_and_record_coverage():
    r = worked_example()
    groups = {"ws0": {"status": "ok", "files": ["Controllers/invoice.go"]},
              "ws1": {"status": "toolchain", "files": ["Services/order.go", "gone.go"]}}
    data = detect(r.projection(), {**BAND_2, "workers": 1}, groups=groups).data
    partial = {tid: t["partial"] for tid, t in data["levels"]["1"].items()}
    order = next(tid for tid, t in data["levels"]["1"].items() if r.t["OS"] in t["members"])
    assert partial[order] is True and sum(partial.values()) == 1
    assert data["levels"]["2"][order]["partial"] is True
    assert data["coverage"] == {"groups": {"ws0": {"status": "ok", "files": 1}, "ws1": {"status": "toolchain", "files": 2}},
                                "failed_files": 2, "partial_topics": [order]}
    assert detect(r.projection(), BAND_2).data["coverage"] is None


def test_file_topics_roles_follow_declaration_counts():
    r = worked_example()
    b = r.b
    # Services/invoice.go also declares a type of the order feature and methods of OS.
    shared = b.type("Services/invoice.go", "OrderHelper")
    for i in range(3):
        b.call(b.method(shared, f"h{i}"), b.method(r.t["OS"], f"x{i}"))
    for i in range(10):                                         # methods of OS declared in invoice.go
        b.method(r.t["OS"], f"Extra{i}", path="Services/invoice.go")
    nodes, edges = b.rows()
    data = detect(build_projection(nodes, edges), BAND_2).data
    tid = {code: next(k for k, v in data["levels"]["1"].items() if r.t[code] in v["members"]) for code in ("IS", "OS")}
    rows = [x for x in community.file_topics(data, nodes) if x["file_id"] == "file:Services/invoice.go"]
    # invoice: IS + its 7 methods = 8; order: OrderHelper + 3 methods + 10 OS methods = 14.
    assert {x["topic_id"]: x["role"] for x in rows} == {tid["OS"]: "primary", tid["IS"]: "secondary"}
    alone = [x for x in community.file_topics(data, nodes) if x["file_id"] == "file:Controllers/order.go"]
    assert alone == [{"file_id": "file:Controllers/order.go", "topic_id": tid["OS"], "role": "primary"}]


def test_topic_tree_has_the_sibling_shape_plus_class_ids():
    r, names = blocks_repo()
    r.type("Lonely", "dto/lonely.go")
    proj = r.projection()
    data = detect(proj, LOW).data
    parent = next(iter(t for t in data["levels"]["1"].values() if not t.get("unassigned")))
    tree = community.topic_tree(data, proj, names={data["leaves"][0]: "子トピック"}, summaries={})
    assert len(tree) == 2 and all(set(e) == {"id", "name", "summary", "files", "class_ids", "subtopics"} for e in tree)
    assert all(len(e["subtopics"]) == 2 for e in tree)
    first = tree[0]
    assert first["files"] == sorted({proj.info[a]["path"] for a in first["class_ids"]})
    assert sorted(a for s in first["subtopics"] for a in s["class_ids"]) == first["class_ids"]
    assert any(s["name"] == "子トピック" for e in tree for s in e["subtopics"])
    assert parent["label"] in {e["name"] for e in tree}
    assert all("dto/lonely.go" not in e["files"] for e in tree)   # 「未分類」 is left out


def test_a_real_backend_panic_becomes_a_community_error(monkeypatch):
    proj = worked_example().projection()
    u, v, _w = proj.edges[0]
    broken = dataclasses.replace(proj, edges=[(u, v, float("nan"))] + proj.edges[1:])
    monkeypatch.setattr(community, "_check_edges", lambda edges: None)   # let the NaN reach Rust
    with pytest.raises(community.CommunityError, match="PanicException") as err:
        detect(broken, {**BAND_2, "workers": 1})
    assert not isinstance(err.value.__cause__, Exception)                # pyo3 panics are BaseExceptions


def test_topic_rows_cover_every_derived_table():
    r, _names = blocks_repo()
    lonely = r.type("Lonely", "dto/lonely.go")
    nodes, edges = r.b.rows()
    data = detect(build_projection(nodes, edges), {**LOW, "id_prefix": "cd"}).data
    first_leaf = data["leaves"][0]
    rows = community.topic_rows(data, nodes, names={"names": {first_leaf: {"name": "受付", "label_source": "llm"}}},
                                summaries={first_leaf: "要約"})
    assert set(rows) == set(community.TOPIC_TABLES)
    topics = {row["topic_id"]: row for row in rows["Topics"]}
    assert len(topics) == len(rows["Topics"]) == 2 + 4 + 1            # level 1, level 2, 「未分類」
    assert topics[first_leaf]["name"] == "受付" and topics[first_leaf]["summary"] == "要約"
    assert topics[data["unassigned_topic"]]["label_source"] == "fixed" and topics[data["unassigned_topic"]]["name"] == "未分類"
    assert {row["level"] for row in rows["Topics"]} == {1, 2} and all(row["basis"] == "structure" for row in rows["Topics"])
    assert set(rows["Topics"][0]) == {"topic_id", "name", "summary", "repo", "embedding", "level", "parent_topic_id",
                                      "size", "file_count", "cohesion", "conductance", "hub_class_id",
                                      "label_source", "basis", "membership_hash"}
    belongs = [(x["class_id"], x["topic_id"], x["role"]) for x in rows["ClassBelongsToTopic"]]
    assert (lonely, data["unassigned_topic"], "unassigned") in belongs
    n_members = sum(len(t["members"]) for lv in ("1", "2") for t in data["levels"][lv].values() if not t.get("carried"))
    assert len(belongs) == n_members
    assert len(rows["TopicContainsTopic"]) == 4 and len(rows["TopicDependsOn"]) == len(data["topic_edges"])
    x = rows["TopicContainsTopic"][0]
    raw = "\x00".join(("edge", "TopicContainsTopic", x["parent_topic"], x["child_topic"]))
    assert x["edge_id"] == "cd_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert {x["role"] for x in rows["FileBelongsToTopic"]} <= {"primary", "secondary"}
    assert data["id_prefix"] == "cd"


# Recorded sweeps of the first real runs (communities.json, 2026-10-14): community counts per γ of the
# grid (0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5) and the index the plateau rule picks (L-R43).
RECORDED_SWEEPS = {
    "F1": ([2, 3, 4, 6, 6, 8, 11], 4),            # plateau 0.5–0.75, same partition: γ recorded 0.75
    "etcd": ([11, 14, 17, 17, 20, 22, 29], 3),     # plateau 0.35–0.5 → 0.5
    "prometheus": ([7, 13, 13, 18, 17, 18, 25], 2),  # plateau 0.2–0.35 → 0.35 (0.2 kept a 65.9 % topic)
}


@pytest.mark.parametrize("repo", sorted(RECORDED_SWEEPS))
def test_recorded_sweeps_pick_the_largest_gamma_of_the_plateau(repo):
    counts, expected = RECORDED_SWEEPS[repo]
    assert choose_resolution(counts, (5, 25)) == (expected, "plateau")


def test_plateau_rule_edges():
    assert choose_resolution([3, 6, 6, 9], (5, 25)) == (2, "plateau")          # F1-shaped two-point tie
    assert choose_resolution([3, 6, 9], (5, 25)) == (1, "plateau")             # single points: the earlier
    assert choose_resolution([6, 6, 9, 9], (5, 25)) == (1, "plateau")          # equal plateaus: the smaller-γ one
    assert choose_resolution([1, 2, 3], (5, 25)) == (2, "closest")             # closest fallback unchanged
    assert choose_resolution([28, 50, 91], (5, 25)) == (0, "closest")