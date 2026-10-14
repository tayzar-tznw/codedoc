"""Partition metrics in plain Python (leiden spec §3.3, §3.4, §6; decision L7).

* `ari`, `ami`, `nmi`: agreement between two labellings. They follow
  scikit-learn 1.9.1's `adjusted_rand_score`, `adjusted_mutual_info_score`
  and `normalized_mutual_info_score` (arithmetic average), including their
  special cases and eps guards. The tests compare them with frozen
  scikit-learn values, so scikit-learn is not a dependency.
* `modularity`: normalized, undirected, weighted modularity with a
  resolution, Q = Σ_C [internal(C)/W − γ·(degree_sum(C)/2W)²] (Codex §8.5).
* `conductance`, `cohesion`: per-community structure on an adjacency map.

Labellings are two equal-length sequences, or two mappings (element →
label), which are compared on their common keys.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence

EPS = 2.220446049250313e-16  # numpy.finfo("float64").eps, as scikit-learn uses it

Labels = Sequence[Hashable] | Mapping[Hashable, Hashable]
Edge = tuple[str, str, float]
Adjacency = dict[str, dict[str, float]]


def _paired(labels_a: Labels, labels_b: Labels) -> tuple[list, list]:
    if isinstance(labels_a, Mapping) and isinstance(labels_b, Mapping):
        keys = [k for k in labels_a if k in labels_b]
        return [labels_a[k] for k in keys], [labels_b[k] for k in keys]
    if isinstance(labels_a, Mapping) or isinstance(labels_b, Mapping):
        raise TypeError("pass two sequences or two mappings")
    xs, ys = list(labels_a), list(labels_b)
    if len(xs) != len(ys):
        raise ValueError(f"labellings differ in length: {len(xs)} != {len(ys)}")
    return xs, ys


def ari(labels_a: Labels, labels_b: Labels) -> float:
    """Adjusted Rand index from the pair confusion matrix (1.0 for identical
    partitions, about 0 for independent ones, 1.0 for empty input)."""
    xs, ys = _paired(labels_a, labels_b)
    n = len(xs)
    cont = Counter(zip(xs, ys))
    rows, cols = Counter(xs), Counter(ys)
    sum_sq = sum(v * v for v in cont.values())
    tp = sum_sq - n
    fp = sum(v * cols[y] for (_x, y), v in cont.items()) - sum_sq
    fn = sum(v * rows[x] for (x, _y), v in cont.items()) - sum_sq
    tn = n * n - fp - fn - sum_sq
    if fn == 0 and fp == 0:
        return 1.0
    return 2.0 * (tp * tn - fn * fp) / ((tp + fn) * (fn + tn) + (tp + fp) * (fp + tn))


def _entropy(counts: Iterable[int], n: int) -> float:
    counts = list(counts)
    if n == 0:
        return 1.0
    if len(counts) == 1:
        return 0.0
    log_n = math.log(n)
    return -sum((c / n) * (math.log(c) - log_n) for c in counts)


def _mutual_info(cont: Counter, rows: Counter, cols: Counter, n: int) -> float:
    if len(rows) == 1 or len(cols) == 1:
        return 0.0
    log_n = math.log(n)
    total = 0.0
    for (x, y), nij in cont.items():
        p = nij / n
        term = p * (math.log(nij) - log_n) + p * (-math.log(rows[x] * cols[y]) + log_n + log_n)
        total += 0.0 if abs(term) < EPS else term
    return max(total, 0.0)


def _expected_mutual_info(a: list[int], b: list[int], n: int) -> float:
    """E[MI] under the hypergeometric model (scikit-learn's Cython loop)."""
    if len(a) == 1 or len(b) == 1:
        return 0.0
    lgf = [math.lgamma(k + 1) for k in range(n + 1)]  # lgf[k] = log(k!)
    log_n = math.log(n)
    emi = 0.0
    for ai in a:
        log_a = math.log(ai)
        for bj in b:
            log_b = math.log(bj)
            fixed = lgf[ai] + lgf[bj] + lgf[n - ai] + lgf[n - bj] - lgf[n]
            for nij in range(max(1, ai - n + bj), min(ai, bj) + 1):
                gln = fixed - lgf[nij] - lgf[ai - nij] - lgf[bj - nij] - lgf[n - ai - bj + nij]
                emi += (nij / n) * (log_n + math.log(nij) - log_a - log_b) * math.exp(gln)
    return emi


def nmi(labels_a: Labels, labels_b: Labels) -> float:
    """Normalized mutual information, MI / mean(H(a), H(b))."""
    xs, ys = _paired(labels_a, labels_b)
    rows, cols = Counter(xs), Counter(ys)
    if len(rows) == len(cols) == 1 or len(rows) == len(cols) == 0:
        return 1.0
    n = len(xs)
    cont = Counter(zip(xs, ys))
    if len(cont) == len(rows) == len(cols):
        return 1.0  # the same partition under other labels
    mi = _mutual_info(cont, rows, cols, n)
    if mi == 0:
        return 0.0
    return mi / ((_entropy(rows.values(), n) + _entropy(cols.values(), n)) / 2)


def ami(labels_a: Labels, labels_b: Labels) -> float:
    """Adjusted mutual information, (MI − E[MI]) / (mean(H) − E[MI]).

    Identical partitions return 1.0 directly: when both are all singletons,
    MI = E[MI] = mean(H) and the quotient is 0/0, which scikit-learn turns
    into 1.0 only because its MI and entropy round alike."""
    xs, ys = _paired(labels_a, labels_b)
    rows, cols = Counter(xs), Counter(ys)
    if len(rows) == len(cols) == 1 or len(rows) == len(cols) == 0:
        return 1.0
    if len(rows) == 1 or len(cols) == 1:
        return 0.0
    n = len(xs)
    cont = Counter(zip(xs, ys))
    if len(cont) == len(rows) == len(cols):
        return 1.0
    mi = _mutual_info(cont, rows, cols, n)
    emi = _expected_mutual_info(list(rows.values()), list(cols.values()), n)
    normalizer = (_entropy(rows.values(), n) + _entropy(cols.values(), n)) / 2
    denominator = normalizer - emi
    denominator = min(denominator, -EPS) if denominator < 0 else max(denominator, EPS)
    numerator = mi - emi
    numerator = min(numerator, -EPS) if numerator < 0 else max(numerator, EPS)
    return numerator / denominator


_MISSING = object()


class ModularityScorer:
    """Normalized modularity of many partitions of one undirected weighted
    graph; weighted degrees and W are computed once.

    Each edge counts once in W and in its community's internal weight, and
    once per endpoint in the degree sums. Repeated edges add up; self-loops
    are ignored (the projection has none). A node missing from a membership
    is a singleton community. 0.0 when the total weight is zero."""

    def __init__(self, edges: Iterable[Edge]):
        self.edges: list[Edge] = []
        self.degree: dict[str, float] = defaultdict(float)
        self.total = 0.0
        for u, v, w in edges:
            if u == v:
                continue
            w = float(w)
            if w < 0:
                raise ValueError(f"negative weight on ({u}, {v})")
            self.edges.append((u, v, w))
            self.degree[u] += w
            self.degree[v] += w
            self.total += w

    def __call__(self, membership: Mapping[str, Hashable], resolution: float = 1.0) -> float:
        if self.total == 0:
            return 0.0
        get = membership.get
        inside = sum(w for u, v, w in self.edges if (cu := get(u, _MISSING)) is not _MISSING and cu == get(v, _MISSING))
        two_w = 2.0 * self.total
        sums: dict[Hashable, float] = defaultdict(float)
        expected = 0.0
        for node, k in self.degree.items():
            c = get(node, _MISSING)
            if c is _MISSING:
                expected += (k / two_w) ** 2
            else:
                sums[c] += k
        expected += sum((d / two_w) ** 2 for d in sums.values())
        return inside / self.total - resolution * expected


def modularity(edges: Iterable[Edge], membership: Mapping[str, Hashable],
               resolution: float = 1.0) -> float:
    """Normalized modularity Q = Σ_C [internal(C)/W − γ·(degree_sum(C)/2W)²]
    (rules in `ModularityScorer`)."""
    return ModularityScorer(edges)(membership, resolution)


def adjacency(edges: Iterable[Edge]) -> Adjacency:
    """Symmetric adjacency map; repeated edges add up, self-loops and zero
    weights are dropped."""
    adj: Adjacency = defaultdict(dict)
    for u, v, w in edges:
        w = float(w)
        if u == v or w == 0:
            continue
        adj[u][v] = adj[u].get(v, 0.0) + w
        adj[v][u] = adj[v].get(u, 0.0) + w
    return dict(adj)


def volume(adj: Adjacency) -> float:
    """Sum of weighted degrees (2W)."""
    return sum(w for nbrs in adj.values() for w in nbrs.values())


def conductance(adj: Adjacency, members: Iterable[str], total_volume: float | None = None) -> float:
    """Weighted cut / min(vol(S), vol(V∖S)), as networkx defines it; 0.0
    when the smaller volume is zero."""
    s = set(members)
    vol_total = volume(adj) if total_volume is None else total_volume
    vol_s = cut = 0.0
    for u in s:
        for v, w in adj.get(u, {}).items():
            vol_s += w
            if v not in s:
                cut += w
    denom = min(vol_s, vol_total - vol_s)
    return cut / denom if denom > 0 else 0.0


def cohesion(adj: Adjacency, members: Iterable[str]) -> float:
    """Internal edges / possible pairs (unweighted, self-loops excluded);
    1.0 for fewer than two members (graphify's convention)."""
    s = set(members)
    n = len(s)
    if n <= 1:
        return 1.0
    internal = sum(1 for u in s for v in adj.get(u, {}) if v in s and u < v)
    return internal / (n * (n - 1) / 2)
