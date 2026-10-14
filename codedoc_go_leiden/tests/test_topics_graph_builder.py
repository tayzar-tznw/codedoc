"""graph_builder: atoms, eligibility, relations, weights, deduplication,
profiles and the ledger (leiden spec §3.1–3.2)."""

import json
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_generator.topics.graph_builder import (build_projection, parse_profile,  # noqa: E402
                                                  relation_weight, write_ledger)
from graph_generator.topics.synthetic import RowBuilder  # noqa: E402


def _weights(projection):
    return {(u, v): w for u, v, w in projection.edges}


def _ledger(projection, u, v):
    key = tuple(sorted((u, v)))
    return next(r for r in projection.ledger if (r["u"], r["v"]) == key)


def _two_types():
    b = RowBuilder()
    a = b.type("billing/fee.go", "FeeCalculator")
    p = b.type("billing/points.go", "Points")
    return b, a, p


def test_call_weight_counts_distinct_method_pairs_in_both_directions():
    b, a, p = _two_types()
    m1, m2 = b.method(a, "Calculate"), b.method(a, "Total")
    n1, n2 = b.method(p, "Round"), b.method(p, "Sum")
    b.call(m1, n1)
    b.call(m1, n1)  # a repeated row of the same pair collapses
    b.call(m2, n1)
    b.call(n2, m1)  # the other direction counts too
    proj = build_projection(*b.rows())
    assert _weights(proj) == {tuple(sorted((a, p))): pytest.approx(2.0)}  # log2(1 + 3)
    call = _ledger(proj, a, p)["relations"]["call"]
    assert (call["rows"], call["pairs"], call["n"]) == (4, 3, 3)
    assert call["contribution"] == pytest.approx(2.0)
    assert sorted(map(tuple, call["facts"])) == sorted([(m1, n1), (m2, n1), (n2, m1)])


def test_call_contribution_is_capped():
    b, a, p = _two_types()
    for i in range(20):
        b.call(b.method(a, f"M{i}"), b.method(p, f"N{i}"))
    proj = build_projection(*b.rows())
    assert proj.edges[0][2] == pytest.approx(4.0)
    assert relation_weight("call", 20) == 4.0
    assert relation_weight("call", 14) == pytest.approx(math.log2(15))


def test_embed_implements_and_calls_add_up():
    b, a, p = _two_types()
    iface = b.type("billing/api.go", "Calculator", kind="interface")
    b.embed(a, p)
    b.embed(a, p, pointer=True)  # one per atom pair
    b.call(b.method(a, "Calculate"), b.method(p, "Round"))
    b.implements(a, iface)
    proj = build_projection(*b.rows())
    w = _weights(proj)
    assert w[tuple(sorted((a, p)))] == pytest.approx(2.0 + 1.0)
    assert w[tuple(sorted((a, iface)))] == pytest.approx(0.5)
    embed = _ledger(proj, a, p)["relations"]["embed"]
    assert (embed["rows"], embed["n"], embed["contribution"]) == (2, 1, 2.0)


def test_interface_method_calls_are_iface_call_to_the_interface():
    b, a, _p = _two_types()
    iface = b.type("store/store.go", "Store", kind="interface")
    get = b.method(iface, "Get", member_kind="interface_method")
    b.call(b.method(a, "Calculate"), get)
    proj = build_projection(*b.rows())
    led = _ledger(proj, a, iface)
    assert list(led["relations"]) == ["iface_call"]
    assert led["weight"] == pytest.approx(1.0)


def test_only_exact_facts_enter():
    b, a, p = _two_types()
    m, n = b.method(a, "Calculate"), b.method(p, "Round")
    b.possibly(m, n)                              # inferred candidates never enter
    b.call(m, n, resolution="name-heuristic")     # not types / convention:*
    b.call(m, b.method(a, "Total"))               # same atom
    b.edges["MethodCalls"].append({"caller_method": m, "callee_method": "method:missing", "resolution": "types"})
    proj = build_projection(*b.rows())
    assert proj.edges == []
    assert proj.stats["calls_excluded_resolution"] == 1
    assert proj.stats["calls_same_atom"] == 1
    assert proj.stats["calls_unmapped"] == 1
    assert sorted(proj.atoms) == sorted([a, p, "class:billing/fee.go:<package>", "class:billing/points.go:<package>"])
    assert proj.stats["atoms_isolated"] == 4


def test_convention_resolutions_are_calls():
    b, a, p = _two_types()
    b.call(b.method(a, "Calculate"), b.method(p, "Round"), resolution="convention:func-alias")
    assert build_projection(*b.rows()).edges[0][2] == pytest.approx(1.0)


def test_functions_belong_to_their_files_package_atom():
    b, a, _p = _two_types()
    f = b.func("billing/util.go", "Round2")
    b.call(b.method(a, "Calculate"), f)
    proj = build_projection(*b.rows())
    assert set(_weights(proj)) == {tuple(sorted((a, "class:billing/util.go:<package>")))}
    assert proj.method_owner[f] == "class:billing/util.go:<package>"


def test_closure_without_class_id_takes_its_enclosing_owner():
    b, a, p = _two_types()
    outer = b.method(a, "Calculate")
    inner = b.closure(b.closure(outer, 1, class_id=None), 1, class_id=None)  # Calculate.func1.func1
    b.call(inner, b.method(p, "Round"))
    proj = build_projection(*b.rows())
    assert proj.method_owner[inner] == a
    assert set(_weights(proj)) == {tuple(sorted((a, p)))}
    assert proj.stats["closures_owned_by_parent"] == 2


def test_tests_and_generated_code_are_excluded_but_kept_for_attachment():
    b, a, p = _two_types()
    b.file("billing/fee_test.go", origin="test")
    t = b.type("billing/fee_test.go", "feeFixture")
    b.file("gen/api.pb.go", generated=True)
    g = b.type("gen/api.pb.go", "Msg")
    calc = b.method(a, "Calculate")
    b.call(b.method(t, "Check"), calc)
    b.call(calc, b.method(g, "Marshal"))
    b.call(calc, b.method(p, "Round"))
    proj = build_projection(*b.rows())
    assert proj.excluded == {t: "test", g: "generated", "class:billing/fee_test.go:<package>": "test",
                             "class:gen/api.pb.go:<package>": "generated"}
    assert t not in proj.atoms and g not in proj.atoms
    assert set(_weights(proj)) == {tuple(sorted((a, p)))}
    assert sorted((u, v) for u, v, _w in proj.attach_edges) == sorted([tuple(sorted((a, t))), tuple(sorted((a, g)))])
    assert proj.attach_adjacency[t] == {a: 1.0}
    assert sum(1 for r in proj.ledger if not r["projected"]) == 2


def test_unknown_origin_is_neither_clustered_nor_attached():
    b, a, p = _two_types()
    odd = b.type("odd/x.go", "Odd")
    b._classes[odd]["origin"] = "vendored"  # not package / command / test / xtest
    b.call(b.method(odd, "M"), b.method(a, "Calculate"))
    proj = build_projection(*b.rows())
    assert proj.excluded[odd] == "origin"
    assert proj.attach_edges == [] and proj.edges == []


def test_profile_with_tests_clusters_test_atoms():
    b, a, _p = _two_types()
    b.file("billing/fee_test.go", origin="xtest")
    t = b.type("billing/fee_test.go", "feeFixture")
    b.call(b.method(t, "Check"), b.method(a, "Calculate"))
    proj = build_projection(*b.rows(), profile="with_tests")
    assert t in proj.atoms
    assert set(_weights(proj)) == {tuple(sorted((a, t)))}


def test_profile_calls_only_drops_embed_and_implements():
    b, a, p = _two_types()
    iface = b.type("billing/api.go", "Calculator", kind="interface")
    b.embed(a, p)
    b.implements(a, iface)
    b.call(b.method(a, "Calculate"), b.method(p, "Round"))
    proj = build_projection(*b.rows(), profile="calls_only")
    assert _weights(proj) == {tuple(sorted((a, p))): pytest.approx(1.0)}
    assert proj.stats["embed_ablated"] == 1 and proj.stats["implements_ablated"] == 1


def test_profile_degree_norm_divides_by_strict_degrees():
    b = RowBuilder()
    a, x, y = (b.type(f"p/{n}.go", n) for n in ("A", "X", "Y"))
    am = b.method(a, "M")
    for i in range(3):
        b.call(am, b.method(x, f"N{i}"))      # n = 3 → 2.0
    b.call(am, b.method(y, "N"))              # n = 1 → 1.0
    proj = build_projection(*b.rows(), profile="degree_norm")
    w = _weights(proj)
    assert w[tuple(sorted((a, x)))] == pytest.approx(2.0 / math.sqrt(3.0 * 2.0))
    assert w[tuple(sorted((a, y)))] == pytest.approx(1.0 / math.sqrt(3.0 * 1.0))
    assert _ledger(proj, a, x)["raw_weight"] == pytest.approx(2.0)


def test_parse_profile_canonicalizes_and_rejects_unknown_options():
    assert parse_profile("strict") == ("strict", frozenset())
    assert parse_profile("") == ("strict", frozenset())
    assert parse_profile("with_tests+degree_norm")[0] == "degree_norm+with_tests"
    assert parse_profile("strict+calls_only")[0] == "calls_only"
    with pytest.raises(ValueError, match="unknown topic profile"):
        parse_profile("enriched")


def _random_rows(seed: int):
    rng = random.Random(seed)
    b = RowBuilder()
    types = [b.type(f"p{i % 4}/f{i}.go", f"T{i}") for i in range(12)]
    methods = [b.method(t, f"M{j}") for t in types for j in range(3)]
    for _ in range(60):
        b.call(*rng.sample(methods, 2))
    for _ in range(4):
        b.embed(*rng.sample(types, 2))
    return b.rows()


def test_projection_is_canonical_and_its_hash_is_stable():
    nodes, edges = _random_rows(1)
    first = build_projection(nodes, edges)
    shuffled_nodes = {k: list(reversed(v)) for k, v in nodes.items()}
    shuffled_edges = {k: random.Random(7).sample(v, len(v)) for k, v in edges.items()}
    second = build_projection(shuffled_nodes, shuffled_edges)
    assert first.edges == second.edges and first.hash == second.hash
    assert first.atoms == sorted(first.atoms)
    assert first.edges == sorted(first.edges)
    assert all(u < v for u, v, _w in first.edges)
    assert build_projection(nodes, edges, "calls_only").hash != first.hash
    edges2 = {**edges, "MethodCalls": edges["MethodCalls"][1:]}
    assert build_projection(nodes, edges2).hash != first.hash


def test_ledger_rows_and_file(tmp_path):
    nodes, edges = _random_rows(2)
    proj = build_projection(nodes, edges)
    projected = [r for r in proj.ledger if r["projected"]]
    assert len(projected) == len(proj.edges)
    for row, (u, v, w) in zip(projected, proj.edges):
        assert (row["u"], row["v"]) == (u, v)
        assert row["weight"] == pytest.approx(w)
        assert row["weight"] == pytest.approx(sum(r["contribution"] for r in row["relations"].values()))
    path = tmp_path / "topics" / "projection.jsonl"
    write_ledger(proj, str(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(proj.ledger)
    assert json.loads(lines[0])["u"] == proj.ledger[0]["u"]


def test_structural_rows_with_unknown_or_equal_ends_are_counted():
    b, a, p = _two_types()
    b.embed(a, "class:nowhere:X")
    b.embed(a, a)
    b.implements(p, "class:nowhere:I")
    b.nodes["Classes"].append({"name": "NoId"})
    proj = build_projection(*b.rows())
    assert proj.edges == []
    assert (proj.stats["embed_unmapped"], proj.stats["embed_same_atom"], proj.stats["implements_unmapped"]) == (1, 1, 1)
    assert proj.stats["classes_without_id"] == 1


def test_functions_list_each_atoms_declarations_without_closures():
    b, a, _p = _two_types()
    calc = b.method(a, "Calculate")
    b.closure(calc, 1)
    f = b.func("billing/fee.go", "NewFee")
    proj = build_projection(*b.rows())
    assert proj.functions[a] == ["FeeCalculator.Calculate"]
    assert proj.functions["class:billing/fee.go:<package>"] == ["NewFee"]
    assert proj.method_names[f] == "NewFee"


def test_closure_callers_collapse_into_their_declaration():
    b, a, p = _two_types()
    calc = b.method(a, "Calculate")
    rnd = b.method(p, "Round")
    b.call(calc, rnd)
    for k in (1, 2, 3):
        b.call(b.closure(calc, k), rnd)          # Calculate.func1..func3 call Round too
    proj = build_projection(*b.rows())
    call = _ledger(proj, a, p)["relations"]["call"]
    assert (call["rows"], call["pairs"], call["n"]) == (4, 1, 1)
    assert proj.edges[0][2] == pytest.approx(1.0)
    assert call["facts"] == [[calc, rnd]]


def test_build_excluded_files_are_not_eligible():
    b, a, _p = _two_types()
    b.file("billing/fee_windows.go", build_included=False)
    w = b.type("billing/fee_windows.go", "WinFee")
    b.call(b.method(w, "M"), b.method(a, "Calculate"))
    proj = build_projection(*b.rows())
    assert proj.excluded[w] == "build" and w not in proj.atoms
    assert proj.attach_adjacency[w] == {a: 1.0}


def test_profile_no_implements_drops_only_implements():
    b, a, p = _two_types()
    iface = b.type("billing/api.go", "Calculator", kind="interface")
    b.embed(a, p)
    b.implements(a, iface)
    proj = build_projection(*b.rows(), profile="no_implements")
    assert _weights(proj) == {tuple(sorted((a, p))): pytest.approx(2.0)}
    assert proj.stats["implements_ablated"] == 1 and "embed_ablated" not in proj.stats


def test_non_finite_weights_are_dropped():
    import graph_generator.topics.graph_builder as gb
    b, a, p = _two_types()
    b.call(b.method(a, "Calculate"), b.method(p, "Round"))
    original = gb.relation_weight
    try:
        gb.relation_weight = lambda rel, n: float("nan")
        proj = build_projection(*b.rows())
    finally:
        gb.relation_weight = original
    assert proj.edges == [] and proj.stats["dropped_weights"] == 1
