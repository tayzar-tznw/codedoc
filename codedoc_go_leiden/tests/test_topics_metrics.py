"""metrics: ARI/AMI/NMI against frozen scikit-learn values, modularity against
Codex §8.5's worked example, conductance and cohesion."""

import os
import sys

import networkx as nx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_generator.topics import metrics  # noqa: E402

# Computed once on 2026-10-13 with scikit-learn 1.9.1
# (uv run --no-project --with scikit-learn==1.9.1 python …):
# (name, labels_a, labels_b, adjusted_rand_score, adjusted_mutual_info_score,
#  normalized_mutual_info_score), arithmetic average.
FROZEN = [
    ('random_4_vs_5',
     [3, 1, 2, 3, 0, 3, 0, 1, 0, 0, 1, 0, 1, 1, 1, 3, 3, 1, 0, 1, 1, 1, 2, 3, 3, 2, 3, 3, 3, 3, 3, 3, 1, 2, 0, 1, 3, 1, 0, 0],
     [3, 0, 3, 4, 3, 1, 4, 0, 3, 0, 3, 1, 2, 1, 1, 0, 3, 0, 2, 0, 1, 2, 1, 4, 3, 1, 4, 2, 3, 1, 0, 4, 4, 4, 0, 3, 1, 4, 3, 0],
     -0.04172193305829728, -0.06565110234657301, 0.06863345042793899),
    ('correlated_noise',
     [2, 2, 2, 1, 0, 1, 2, 2, 1, 1, 2, 2, 2, 1, 1, 1, 1, 0, 2, 2, 1, 2, 0, 0, 1, 1, 2, 1, 2, 2, 2, 1, 2, 0, 2, 1, 2, 2, 2, 2,
      1, 0, 0, 2, 2, 1, 0, 0, 0, 2, 0, 0, 1, 2, 0, 1, 1, 1, 1, 2],
     [2, 2, 2, 1, 0, 1, 2, 2, 1, 1, 2, 2, 0, 1, 1, 1, 1, 0, 2, 2, 1, 2, 0, 2, 1, 1, 2, 1, 2, 2, 2, 1, 2, 0, 2, 1, 2, 2, 2, 2,
      1, 0, 0, 0, 2, 1, 0, 0, 0, 2, 0, 0, 1, 0, 0, 1, 1, 1, 1, 2],
     0.8238197415738485, 0.802935319939446, 0.8094455150621992),
    ('six_vs_two',
     [0, 0, 0, 0, 4, 5, 4, 4, 0, 0, 0, 0, 3, 4, 5, 0, 2, 3, 5, 2, 2, 5, 1, 5, 5],
     [1, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1, 1, 1, 0, 0, 1, 1, 1, 1, 0],
     0.07142255537774783, -0.0006400599522147736, 0.11438680237483054),
    ('strings_vs_ints',
     ['t3', 't2', 't0', 't2', 't2', 't1', 't3', 't1', 't3', 't3', 't3', 't1', 't2', 't2', 't3', 't0', 't1', 't3', 't0',
      't3', 't3', 't0', 't0', 't2', 't0', 't0', 't3', 't0', 't2', 't2', 't3', 't0', 't2', 't0', 't3', 't2', 't1', 't3',
      't1', 't3', 't0', 't1', 't2', 't0', 't2', 't0', 't2', 't1', 't2', 't3'],
     [3, 6, 4, 0, 6, 2, 4, 4, 1, 0, 3, 0, 6, 6, 3, 6, 3, 1, 3, 2, 4, 5, 4, 6, 0, 0, 1, 1, 1, 4, 2, 2, 0, 6, 0, 1, 2, 6, 3, 3,
      2, 3, 2, 3, 5, 0, 5, 4, 2, 3],
     0.00301089234583936, 0.007016810638631219, 0.140532316498553),
    ('many_small',
     [9, 3, 9, 6, 9, 5, 5, 4, 7, 8, 6, 9, 7, 5, 4, 8, 0, 0, 2, 2, 7, 7, 9, 2, 7, 0, 2, 0, 6, 2],
     [9, 9, 0, 6, 3, 6, 8, 0, 7, 2, 4, 2, 0, 9, 9, 5, 4, 7, 3, 1, 9, 9, 7, 8, 2, 6, 4, 5, 0, 7],
     -0.07841336883665412, -0.11950714999459613, 0.4411542389699937),
    ('relabelled',
     [1, 0, 4, 3, 1, 1, 3, 3, 3, 4, 3, 2, 0, 0, 0, 2, 3, 3, 2, 1, 4, 2, 1, 4, 1, 2, 1, 1, 1, 2, 0, 3, 3, 2, 4, 4, 3, 4, 0, 0,
      3, 4, 2, 4, 4],
     [0, 3, 2, 1, 0, 0, 1, 1, 1, 2, 1, 4, 3, 3, 3, 4, 1, 1, 4, 0, 2, 4, 0, 2, 0, 4, 0, 0, 0, 4, 3, 1, 1, 4, 2, 2, 1, 2, 3, 3,
      1, 2, 4, 2, 2],
     1.0, 1.0, 1.0),
    ('blocks_of_ten',
     [i // 10 for i in range(100)],
     [1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 2, 3, 2, 2, 2, 2, 2, 2, 3, 2, 3, 3, 3, 3, 3, 4, 3, 3, 3, 3,
      4, 4, 5, 4, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 5, 6, 5, 5, 5, 6, 6, 6, 7, 6, 6, 6, 6, 6, 6, 8, 7, 7, 7, 7, 7, 7, 8, 7, 7,
      8, 8, 8, 8, 9, 8, 8, 8, 8, 8, 9, 0, 9, 9, 9, 9, 9, 9, 0, 9],
     0.6907923860115095, 0.7700909808032294, 0.8197212309980231),
    ('singletons_vs_pairs',
     [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
     [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5],
     0.0, 3.203426503814903e-15, 0.8379234756068775),
    ('one_cluster_both', [0, 0, 0, 0, 0], [1, 1, 1, 1, 1], 1.0, 1.0, 1.0),
    ('one_cluster_one_side', [0, 0, 0, 0, 0, 0], [0, 1, 0, 1, 2, 2], 0.0, 0.0, 0.0),
    ('all_singletons_both', [0, 1, 2, 3, 4, 5], [0, 1, 2, 3, 4, 5], 1.0, 1.0, 1.0),
    ('two_elements_split', [0, 1], [0, 0], 0.0, 0.0, 0.0),
    ('all_singletons_7', [0, 1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1, 0], 1.0, 1.0, 1.0),
    ('all_singletons_10', list(range(10)), list(range(10)), 1.0, 1.0, 1.0),
]


@pytest.mark.parametrize("name,a,b,ari,ami,nmi", FROZEN, ids=[c[0] for c in FROZEN])
def test_agreement_metrics_equal_frozen_scikit_learn_values(name, a, b, ari, ami, nmi):
    assert metrics.ari(a, b) == pytest.approx(ari, abs=1e-15)
    assert metrics.ami(a, b) == pytest.approx(ami, abs=1e-12)
    assert metrics.nmi(a, b) == pytest.approx(nmi, abs=1e-12)


def test_at_least_five_random_partitions_are_frozen():
    random_cases = [c for c in FROZEN if len(set(c[1])) > 1 and len(set(c[2])) > 1 and c[3] not in (0.0, 1.0)]
    assert len(random_cases) >= 5


def test_agreement_metrics_are_symmetric_and_label_free():
    a, b = FROZEN[1][1], FROZEN[1][2]
    renamed = [f"x{v}" for v in b]
    for f in (metrics.ari, metrics.ami, metrics.nmi):
        assert f(a, b) == pytest.approx(f(b, a), abs=1e-12)
        assert f(a, renamed) == pytest.approx(f(a, b), abs=1e-12)


def test_mappings_compare_on_common_keys():
    a = {"f1": "x", "f2": "x", "f3": "y", "f4": "y", "only_a": "z"}
    b = {"f1": 1, "f2": 1, "f3": 2, "f4": 2, "only_b": 9}
    assert metrics.ari(a, b) == 1.0
    assert metrics.ami(a, b) == 1.0
    assert metrics.nmi(a, b) == 1.0


def test_bad_inputs_raise():
    with pytest.raises(ValueError):
        metrics.ari([1, 2], [1])
    with pytest.raises(TypeError):
        metrics.ami({"a": 1}, [1])


def test_empty_labellings():
    assert metrics.ari([], []) == 1.0
    assert metrics.ami([], []) == 1.0
    assert metrics.nmi([], []) == 1.0


# Codex §8.5: six types, two features, three directories.
WORKED = [("IC", "IS", 2.0), ("IS", "IR", 2.0), ("IC", "IR", 2.0), ("IS", "OS", 1.0),
          ("OC", "OS", 2.0), ("OS", "OR", 2.0), ("OC", "OR", 2.0)]
FEATURES = {"IC": "invoice", "IS": "invoice", "IR": "invoice", "OC": "order", "OS": "order", "OR": "order"}
DIRECTORIES = {"IC": "Controllers", "OC": "Controllers", "IS": "Services", "OS": "Services",
               "IR": "Repositories", "OR": "Repositories"}


def test_modularity_of_codex_worked_example():
    assert metrics.modularity(WORKED, FEATURES) == pytest.approx(11 / 26, abs=1e-12)
    assert metrics.modularity(WORKED, DIRECTORIES) == pytest.approx(-44 / 169, abs=1e-12)
    assert metrics.modularity(WORKED, dict.fromkeys(FEATURES, "all")) == pytest.approx(0.0, abs=1e-12)


def test_modularity_moves_of_codex_worked_example():
    singletons = {v: v for v in FEATURES}
    q0 = metrics.modularity(WORKED, singletons)
    q1 = metrics.modularity(WORKED, {**singletons, "IS": "IC"})
    q2 = metrics.modularity(WORKED, {**singletons, "IS": "IC", "IR": "IC"})
    assert q1 - q0 == pytest.approx(16 / 169, abs=1e-12)
    assert q2 - q1 == pytest.approx(34 / 169, abs=1e-12)
    assert metrics.modularity(WORKED, dict.fromkeys(FEATURES, 0)) \
        - metrics.modularity(WORKED, FEATURES) == pytest.approx(-11 / 26, abs=1e-12)


def test_modularity_matches_the_backend_formula():
    gn = pytest.importorskip("graspologic_native")
    labels = {"invoice": 0, "order": 1}
    for resolution in (0.5, 1.0, 1.5):
        ours = metrics.modularity(WORKED, FEATURES, resolution)
        theirs = gn.modularity(WORKED, {k: labels[v] for k, v in FEATURES.items()}, resolution)
        assert ours == pytest.approx(theirs, abs=1e-12)


def test_modularity_edge_rules():
    assert metrics.modularity([], {}) == 0.0
    assert metrics.modularity(WORKED + [("IC", "IC", 9.0)], FEATURES) == pytest.approx(11 / 26, abs=1e-12)
    with_missing = {k: v for k, v in FEATURES.items() if k != "OR"}
    assert metrics.modularity(WORKED, with_missing) == pytest.approx(
        metrics.modularity(WORKED, {**FEATURES, "OR": "alone"}), abs=1e-12)
    with pytest.raises(ValueError):
        metrics.modularity([("a", "b", -1.0)], {"a": 0, "b": 0})


def test_conductance_matches_networkx():
    graph = nx.Graph()
    graph.add_weighted_edges_from(WORKED)
    adj = metrics.adjacency(WORKED)
    for members in (["IC", "IS", "IR"], ["IC"], ["IS", "OS"], ["IC", "OC", "IR"]):
        assert metrics.conductance(adj, members) == pytest.approx(
            nx.conductance(graph, members, weight="weight"), abs=1e-12)
    assert metrics.conductance(adj, ["IC", "IS", "IR"]) == pytest.approx(1 / 13, abs=1e-12)
    assert metrics.conductance(adj, []) == 0.0
    assert metrics.conductance({}, ["x"]) == 0.0


def test_cohesion_counts_internal_pairs():
    adj = metrics.adjacency([("a", "b", 1.0), ("b", "c", 5.0), ("a", "c", 1.0), ("c", "d", 1.0), ("d", "d", 1.0)])
    assert metrics.cohesion(adj, ["a", "b", "c"]) == 1.0
    assert metrics.cohesion(adj, ["a", "b", "c", "d"]) == pytest.approx(4 / 6)
    assert metrics.cohesion(adj, ["a"]) == 1.0


def test_adjacency_sums_repeats_and_drops_loops_and_zeros():
    adj = metrics.adjacency([("a", "b", 1.0), ("b", "a", 2.0), ("a", "a", 3.0), ("c", "d", 0.0)])
    assert adj == {"a": {"b": 3.0}, "b": {"a": 3.0}}
    assert metrics.volume(adj) == 6.0
