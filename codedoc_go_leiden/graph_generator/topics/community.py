"""Phase 4a: Leiden communities on the atom projection (leiden spec §3.3–3.6
with the Appendix B amendments L-R8, L-R9, L-R25, L-R38, L-R39, L-R40).

`detect(projection, params, previous, groups)` returns `Communities`; its
`data` is the C12 document for `topics/communities.json`.

1. **Backend.** `graspologic_native.leiden` (MIT), pinned by the variant's
   requirements and recorded. A missing backend raises BackendMissingError
   with the install command; there is no fallback (Codex §8.2).
   `starting_communities` is never passed (1.3.1 panics on it), and neither
   are isolates, zero or non-finite weights. A graph without edges, or a
   level-2 subgraph without internal edges, never reaches the backend. Any
   backend exception, including `PanicException` (a BaseException), becomes
   CommunityError, which stops 4a and not the graph track (L-R8).
2. **Input** is canonical: sorted atoms, `(min, max)` endpoints, sorted
   edges (the projection's order). Same input and seed give the same result.
3. **Trials.** One `leiden` call per seed (`trials=1`); calls run in a
   small thread pool, since the backend releases the GIL, and results are
   taken in seed order, so threads never change a result. A trial's quality
   is `graspologic_native.modularity` at the trial's own resolution (L-R25).
   `metrics.ModularityScorer` (verified on Codex §8.5's worked example)
   recomputes it as a QA check: both values are recorded, and a difference
   beyond `isclose(rel_tol=1e-9, abs_tol=1e-6)` only warns (RuntimeWarning
   and the document's `warnings`), never stops. The best trial has the highest quality;
   ties within `tolerance` go to the lexicographically smallest membership
   hash. Every trial and the mean pairwise seed ARI are recorded.
4. **Resolution (level 1).** Sweep the grid, count communities with at least
   `min_size` atoms, take the longest plateau of that count inside the band
   (ties: the plateau with the smaller resolution), and within it the
   largest resolution (L-R43: at the smaller one, modularity's resolution
   limit keeps a giant community while small components fill the count);
   with no count in the band, the resolution whose count is closest to it
   (ties: the smaller resolution).
5. **Membership (§3.4, L-R9).** Primary members are the clustered atoms:
   eligible atoms with edges, after thin communities (< `min_size`) merged
   into the neighbour with the most shared weight. Every other atom —
   isolates, thin leftovers without a neighbour, tests, generated and
   build-excluded code — is attached at every level inside its parent: to
   the community with most primary atoms of its file (`attached-file`), else
   to the one it shares the most weight with (`attached-test`,
   `attached-generated`, `attached-excluded`), else to 「未分類」. 「未分類」
   is one level-1 topic (`unassigned: true`, role `unassigned`, a fixed ID,
   never named by the LLM), also listed in the C12 `unassigned` key.
6. **Level 2.** A level-1 topic with more than `subtopic_min_atoms` primary
   atoms is re-clustered on its induced subgraph (γ = `level2_resolution`,
   same seeds). The split is kept only when at least two children have
   `min_size` atoms; thin children merge into a sibling, and attached
   members follow the same rules without leaving the parent, so children are
   disjoint and their union is the parent. Unsplit topics and 「未分類」 are
   carried into level 2 under the same ID (`carried: true`), so each level
   covers every atom exactly once; `leaves` lists the leaf topics.
7. **Identity (§3.5).** `topic_id = _make_id("topic", repo, f"L{level}",
   membership_hash)` with the hash over the primary members. Names carry over
   from a previous communities.json at Jaccard ≥ `jaccard` over primary
   members (`inherited_from`); pages are regenerated whenever the ID changes
   (L-R38). The hub is the member with the highest PageRank inside the
   community.

Ties between candidate communities: the higher score, then the smaller
membership hash; between atoms: the smaller class_id.
"""

from __future__ import annotations

import hashlib
import heapq
import importlib.metadata
import json
import math
import os
import time
import warnings
from collections import Counter, defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .graph_builder import (CALL_RELATIONS, PAIR_RELATIONS, ROLE_OF_EXCLUSION, Projection,
                            atom_label, relation_weight)
from .metrics import Adjacency, Edge, ModularityScorer, ari, cohesion, conductance, volume

VERSION = 1
BACKEND = "graspologic-native"
TESTED_BACKEND_VERSION = "1.3.1"
INSTALL_HINT = ("graspologic-native is not installed. Phase 4a (Leiden topics) needs it and never "
                "falls back to another algorithm. Install the pinned version: "
                "uv pip install --python .venv/bin/python -r requirements.txt "
                f"(or 'graspologic-native=={TESTED_BACKEND_VERSION}')")
UNASSIGNED_NAME = "未分類"
UNASSIGNED_KEY = "unassigned"  # the fourth _make_id input of the 「未分類」 topic
ROLES = ("primary", "attached-file", "attached-test", "attached-generated", "attached-excluded", "unassigned")
QUALITY_REL_TOL, QUALITY_ABS_TOL = 1e-9, 1e-6  # backend vs own modularity before a warning


class BackendMissingError(RuntimeError):
    """graspologic-native cannot be imported."""


class CommunityError(RuntimeError):
    """The backend failed or a result broke an invariant; Phase 4a stops."""


@dataclass(frozen=True)
class Params:
    """4a settings; the defaults are the leiden spec's (§3.3, §7)."""

    repo: str = ""                  # empty: the repo column of the Classes rows
    id_prefix: str = "a"            # config.ID_PREFIX in the pipeline
    seeds: tuple[int, ...] = (42, 43, 44, 45, 46)
    randomness: float = 0.01
    iterations: int = 10
    resolutions: tuple[float, ...] = (0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5)
    band: tuple[int, int] = (5, 25)
    subtopic_min_atoms: int = 12
    level2_resolution: float = 1.0
    min_size: int = 3
    jaccard: float = 0.6
    tolerance: float = 1e-9
    workers: int = 0                # threads for backend calls; 0 = min(8, CPU count); no effect on results

    @classmethod
    def of(cls, params: Params | Mapping[str, Any] | None) -> Params:
        if params is None:
            p = cls()
        elif isinstance(params, cls):
            p = params
        else:
            kw = dict(params)
            for key in ("seeds", "resolutions", "band"):
                if key in kw:
                    kw[key] = tuple(kw[key])
            p = cls(**kw)
        if not p.seeds or len(set(p.seeds)) != len(p.seeds):
            raise ValueError(f"seeds must be distinct and non-empty: {p.seeds}")
        if not p.resolutions or any(r <= 0 for r in p.resolutions) \
                or list(p.resolutions) != sorted(set(p.resolutions)):
            raise ValueError(f"resolutions must be positive and strictly increasing: {p.resolutions}")
        if len(p.band) != 2 or not 1 <= p.band[0] <= p.band[1]:
            raise ValueError(f"band must be (min, max) with 1 <= min <= max: {p.band}")
        if p.min_size < 1 or p.subtopic_min_atoms < 1 or p.iterations < 1 or p.workers < 0:
            raise ValueError("min_size, subtopic_min_atoms and iterations must be >= 1, workers >= 0")
        return p


@dataclass
class Communities:
    """`data` is the C12 document; `timings` (seconds) stay out of it so the
    document is byte-identical across runs on the same input."""

    data: dict
    timings: dict[str, float] = field(default_factory=dict)

    def write(self, path: str) -> None:
        """Write `data` atomically (sorted keys, UTF-8)."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════════
# Identity
# ═══════════════════════════════════════════════════════════════════


def _make_id(kind: str, repo: str, relpath: str, qualname: str, id_prefix: str) -> str:
    """Copied from codedoc_python/graph_generator/pipeline.py `_make_id`
    (lines 1717–1730) with an identical formula, so topic IDs are made like
    every node ID. ID_PREFIX is a parameter because this package does not
    import the pipeline's config."""
    raw = "\x00".join((kind, repo, relpath, qualname))
    h = hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return f"{id_prefix}_{h}"


def membership_hash(members) -> str:
    """sha256("|".join(sorted(class_ids)))[:16] (leiden spec §3.5); topics
    pass their primary members (L-R9)."""
    return hashlib.sha256("|".join(sorted(members)).encode("utf-8")).hexdigest()[:16]


def partition_hash(groups) -> str:
    """16 hex chars over a whole partition, independent of group order."""
    text = "\n".join(sorted("|".join(sorted(g)) for g in groups))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _edge_id(table: str, source: str, target: str, id_prefix: str) -> str:
    """Edge ID as codedoc_python's `pipeline._edge_id`: `_make_id("edge",
    table, source, target)`."""
    return _make_id("edge", table, source, target, id_prefix)


def topic_id(repo: str, level: int, mhash: str, id_prefix: str) -> str:
    return _make_id("topic", repo, f"L{level}", mhash, id_prefix)


def unassigned_topic_id(repo: str, id_prefix: str) -> str:
    return _make_id("topic", repo, "L1", UNASSIGNED_KEY, id_prefix)


def primary_members(topic: Mapping) -> list[str]:
    return sorted(a for a, role in topic["roles"].items() if role == "primary")


# ═══════════════════════════════════════════════════════════════════
# Backend and trials
# ═══════════════════════════════════════════════════════════════════


def backend_version() -> str | None:
    try:
        return importlib.metadata.version(BACKEND)
    except importlib.metadata.PackageNotFoundError:
        return None


def _backend():
    try:
        import graspologic_native
    except ImportError as exc:
        raise BackendMissingError(INSTALL_HINT) from exc
    return graspologic_native


def _check_edges(edges: list[Edge]) -> None:
    """The backend gets only canonical edges with finite positive weights (L-R8)."""
    for u, v, w in edges:
        if not u < v or not (w > 0 and math.isfinite(w)):
            raise CommunityError(f"invalid edge for the backend: ({u!r}, {v!r}, {w!r})")


@dataclass
class _Trial:
    seed: int
    groups: list[list[str]]
    quality: float          # graspologic_native.modularity at the trial's resolution (selection key)
    backend_quality: float  # the quality leiden itself returned
    hash: str
    check: float            # metrics.ModularityScorer at the same resolution

    def record(self) -> dict:
        return {"seed": self.seed, "modularity": round(self.quality, 6),
                "modularity_check": round(self.check, 6), "modularity_delta": abs(self.quality - self.check),
                "backend_quality": round(self.backend_quality, 6),
                "membership_hash": self.hash, "communities": len(self.groups)}


def _groups(membership: Mapping[str, int]) -> list[list[str]]:
    inverse: dict[int, list[str]] = defaultdict(list)
    for node, label in membership.items():
        inverse[label].append(node)
    return sorted((sorted(g) for g in inverse.values()), key=lambda g: (-len(g), g))


def _leiden_calls(gn, edges: list[Edge], specs: list[tuple[float, int]], p: Params) -> list:
    """(leiden quality, membership, backend modularity at the resolution) for
    each (resolution, seed), in spec order. Calls run in threads (the backend
    releases the GIL); each call is independent, so threads never change a
    result. A backend exception of any kind becomes CommunityError."""
    def call(spec: tuple[float, int]):
        resolution, seed = spec
        try:
            quality, membership = gn.leiden(edges, resolution=resolution, randomness=p.randomness,
                                            iterations=p.iterations, use_modularity=True, seed=seed, trials=1)
            return quality, membership, gn.modularity(edges, membership, resolution=resolution)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # PanicException derives from BaseException (L-R8)
            raise CommunityError(f"graspologic-native failed at resolution {resolution}, seed {seed}: "
                                 f"{type(exc).__name__}: {exc}") from exc
    workers = min(len(specs), p.workers or min(8, os.cpu_count() or 1))
    if workers <= 1:
        return [call(spec) for spec in specs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(call, specs))


def _run_trials(gn, edges: list[Edge], resolution: float, p: Params, results: list | None = None,
                scorer: ModularityScorer | None = None,
                notes: list[str] | None = None) -> tuple[_Trial, list[_Trial], float | None]:
    """One leiden call per seed (or the given `results`, in seed order) →
    (best trial, all trials, mean pairwise seed ARI). QA warnings go to
    `notes` and to `warnings.warn`."""
    nodes = sorted({x for u, v, _ in edges for x in (u, v)})
    scorer = scorer or ModularityScorer(edges)
    if results is None:
        results = _leiden_calls(gn, edges, [(resolution, seed) for seed in p.seeds], p)
    trials: list[_Trial] = []
    for seed, (backend_q, membership, backend_mod) in zip(p.seeds, results):
        if sorted(membership) != nodes:
            raise CommunityError(f"graspologic-native returned {len(membership)} labels for {len(nodes)} "
                                 f"nodes at resolution {resolution}, seed {seed}")
        quality = float(backend_mod)
        ours = scorer(membership, resolution)
        if not math.isclose(quality, ours, rel_tol=QUALITY_REL_TOL, abs_tol=QUALITY_ABS_TOL):
            note = (f"modularity check: graspologic-native {quality!r} vs own {ours!r} at resolution "
                    f"{resolution}, seed {seed} (selection uses graspologic-native)")
            warnings.warn(note, RuntimeWarning, stacklevel=2)
            if notes is not None:
                notes.append(note)
        groups = _groups(membership)
        trials.append(_Trial(seed, groups, quality, float(backend_q), partition_hash(groups), ours))
    best_q = max(t.quality for t in trials)
    best = min((t for t in trials if t.quality >= best_q - p.tolerance), key=lambda t: t.hash)
    return best, trials, _seed_ari(trials, nodes)


def _seed_ari(trials: list[_Trial], nodes: list[str]) -> float | None:
    if len(trials) < 2:
        return None
    labels = []
    for t in trials:
        where = {a: i for i, g in enumerate(t.groups) for a in g}
        labels.append([where[a] for a in nodes])
    pairs = [(i, j) for i in range(len(labels)) for j in range(i + 1, len(labels))]
    return sum(ari(labels[i], labels[j]) for i, j in pairs) / len(pairs)


def choose_resolution(counts: list[int], band: tuple[int, int]) -> tuple[int, str]:
    """Index into the sweep and the rule used. "plateau": the longest run of
    equal counts inside the band (equal runs: the earlier one), at its
    largest resolution (L-R43). "closest": no count in the band; the count
    closest to it (ties: the earlier one)."""
    lo, hi = band
    runs: list[tuple[int, int]] = []  # (start, length)
    i = 0
    while i < len(counts):
        j = i
        while j + 1 < len(counts) and counts[j + 1] == counts[i]:
            j += 1
        runs.append((i, j - i + 1))
        i = j + 1
    inside = [r for r in runs if lo <= counts[r[0]] <= hi]
    if inside:
        start, length = max(inside, key=lambda r: (r[1], -r[0]))
        return start + length - 1, "plateau"

    def distance(c: int) -> int:
        return lo - c if c < lo else c - hi

    return min(range(len(counts)), key=lambda k: (distance(counts[k]), k)), "closest"


# ═══════════════════════════════════════════════════════════════════
# Membership (§3.4, L-R9)
# ═══════════════════════════════════════════════════════════════════


def _pick(scores: Mapping[int, float], groups: Mapping[int, set[str]]) -> int:
    """The highest score, then the smaller membership hash (L-R9)."""
    best = max(scores.values())
    tied = [k for k, s in scores.items() if s == best]
    return tied[0] if len(tied) == 1 else min(tied, key=lambda k: membership_hash(groups[k]))


def merge_thin(groups: list[list[str]] | list[set[str]], adj: Adjacency,
               min_size: int) -> tuple[list[set[str]], list[str]]:
    """Merge every group below `min_size` into the neighbouring group with the
    most shared weight, smallest groups first; return the groups and the
    atoms of thin groups without a neighbour (they are treated like
    isolates)."""
    comm: dict[int, set[str]] = {i: set(g) for i, g in enumerate(groups)}
    where = {a: i for i, g in comm.items() for a in g}
    dropped: list[str] = []
    heap = [(len(g), membership_hash(g), i) for i, g in comm.items() if len(g) < min_size]
    heapq.heapify(heap)
    while heap:
        size, _key, i = heapq.heappop(heap)
        g = comm.get(i)
        if g is None or len(g) != size:
            continue
        shared: dict[int, float] = defaultdict(float)
        for a in sorted(g):
            for b, w in adj.get(a, {}).items():
                j = where.get(b)
                if j is not None and j != i:
                    shared[j] += w
        if not shared:
            dropped.extend(sorted(g))
            for a in g:
                del where[a]
            del comm[i]
            continue
        target = _pick(shared, comm)
        comm[target] |= g
        for a in g:
            where[a] = target
        del comm[i]
        if len(comm[target]) < min_size:
            heapq.heappush(heap, (len(comm[target]), membership_hash(comm[target]), target))
    return [comm[k] for k in sorted(comm)], dropped


def _attach(primary: dict[int, set[str]], projection: Projection, candidates: Mapping[str, tuple],
            inside_parent: bool) -> tuple[dict[int, dict[str, str]], dict[str, str]]:
    """Add non-primary atoms to the primary groups (L-R9). `candidates` maps
    an atom to (weight role, reason): every atom first tries file
    co-location (`attached-file`), then — when it has a weight role (tests,
    generated, build-excluded) — the most shared weight with primary members.
    Placement looks only at primary members, so the order does not matter.
    At level 2 (`inside_parent`) an atom never leaves the parent: the last
    resort is the group with the smallest membership hash. Returns the
    member maps (atom → role) and the atoms left unplaced (→ reason)."""
    file_of = {cid: a.get("file_id") or "" for cid, a in projection.info.items()}
    where = {a: k for k, g in primary.items() for a in g}
    per_file: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for a, k in where.items():
        if file_of.get(a):
            per_file[file_of[a]][k] += 1
    attach_adj = projection.attach_adjacency
    members = {k: dict.fromkeys(sorted(g), "primary") for k, g in primary.items()}
    unplaced: dict[str, str] = {}
    for atom, (weight_role, reason) in sorted(candidates.items()):
        by_file = per_file.get(file_of.get(atom) or "", {})
        if by_file:
            members[_pick(by_file, primary)][atom] = "attached-file"
            continue
        if weight_role:
            shared: dict[int, float] = defaultdict(float)
            for b, w in sorted(attach_adj.get(atom, {}).items()):
                if b in where:
                    shared[where[b]] += w
            if shared:
                members[_pick(shared, primary)][atom] = weight_role
                continue
        if inside_parent and primary:
            members[_pick(dict.fromkeys(primary, 0.0), primary)][atom] = weight_role or "attached-file"
        else:
            unplaced[atom] = reason
    return members, unplaced


# ═══════════════════════════════════════════════════════════════════
# Topic records
# ═══════════════════════════════════════════════════════════════════


def _rank(members: list[str], projection: Projection) -> list[str]:
    """Members by PageRank inside the community (weighted, undirected), then
    in-community weighted degree, then ID; non-projection members (attached
    tests, generated and build-excluded code) follow by ID."""
    adj = projection.adjacency
    sub = [a for a in members if a in projection.eligible]
    sset = set(sub)
    graph = nx.Graph()
    graph.add_nodes_from(sub)
    graph.add_weighted_edges_from((u, v, w) for u in sub for v, w in sorted(adj.get(u, {}).items())
                                  if v in sset and u < v)
    pr = nx.pagerank(graph, weight="weight", max_iter=1000) if sub else {}
    deg = {a: sum(w for v, w in adj.get(a, {}).items() if v in sset) for a in sub}
    ranked = sorted(sub, key=lambda a: (-round(pr.get(a, 0.0), 10), -round(deg[a], 10), a))
    return ranked + sorted(a for a in members if a not in sset)


def _files(members, projection: Projection) -> list[str]:
    return sorted({projection.info[a]["file_id"] for a in members if projection.info[a]["file_id"]})


def _record(level: int, members: Mapping[str, str], parent: str | None, repo: str, p: Params,
            projection: Projection, total_volume: float) -> tuple[str, dict]:
    mem = sorted(members)
    mhash = membership_hash(a for a in mem if members[a] == "primary")
    tid = topic_id(repo, level, mhash, p.id_prefix)
    adj = projection.adjacency
    eligible_members = [a for a in mem if a in projection.eligible]
    ranked = _rank(mem, projection)
    hub = ranked[0] if ranked else None
    files = _files(mem, projection)
    return tid, {
        "level": level, "parent": parent, "children": [],
        "members": mem, "roles": {a: members[a] for a in mem},
        "hub": hub, "label": atom_label(projection.info[hub]) if hub else "", "label_source": "hub",
        "ranked": ranked, "size": len(mem), "file_count": len(files), "files": files,
        "cohesion": round(cohesion(adj, eligible_members), 6),
        "conductance": round(conductance(adj, eligible_members, total_volume), 6),
        "membership_hash": mhash, "inherited_from": None, "jaccard": None, "partial": False,
    }


def _unassigned_record(members: list[str], repo: str, p: Params, projection: Projection) -> tuple[str, dict]:
    files = _files(members, projection)
    return unassigned_topic_id(repo, p.id_prefix), {
        "level": 1, "parent": None, "children": [], "unassigned": True,
        "members": sorted(members), "roles": dict.fromkeys(sorted(members), "unassigned"),
        "hub": None, "label": UNASSIGNED_NAME, "label_source": "fixed", "ranked": [],
        "size": len(members), "file_count": len(files), "files": files,
        "cohesion": None, "conductance": None, "membership_hash": membership_hash([]),
        "inherited_from": None, "jaccard": None, "partial": False,
        "split": {"attempted": False, "accepted": False, "reason": "unassigned"},
    }


def _split(members: dict[str, str], gn, p: Params, projection: Projection,
           notes: list[str] | None = None) -> tuple[list[dict[str, str]] | None, dict]:
    """Level-2 children of one level-1 topic, or None with the reason."""
    primary = sorted(a for a, r in members.items() if r == "primary")
    if len(primary) <= p.subtopic_min_atoms:
        return None, {"attempted": False, "accepted": False,
                      "reason": f"{len(primary)} primary atoms <= {p.subtopic_min_atoms}"}
    pset = set(primary)
    adj = projection.adjacency
    sub_adj = {u: {v: w for v, w in adj.get(u, {}).items() if v in pset} for u in primary}
    sub_edges = sorted((u, v, w) for u in primary for v, w in sub_adj[u].items() if u < v)
    if not sub_edges:
        return None, {"attempted": True, "accepted": False, "reason": "no internal edges"}
    best, trials, seed_ari = _run_trials(gn, sub_edges, p.level2_resolution, p, notes=notes)
    raw_sizes = [len(g) for g in best.groups]
    record = {"attempted": True, "resolution": p.level2_resolution,
              "modularity": round(best.quality, 6), "membership_hash": best.hash,
              "raw_sizes": raw_sizes, "seed_ari": None if seed_ari is None else round(seed_ari, 6),
              "trials": [t.record() for t in trials]}
    big = sum(1 for s in raw_sizes if s >= p.min_size)
    if big < 2:
        record.update(accepted=False, reason=f"{big} child(ren) with >= {p.min_size} atoms")
        return None, record
    groups, thin = merge_thin(best.groups, sub_adj, p.min_size)
    placed = set().union(*groups)
    candidates = {a: (None, "thin") for a in thin}
    candidates.update({a: (None if r in ("primary", "attached-file") else r, r)
                       for a, r in members.items() if a not in placed and a not in candidates})
    children, unplaced = _attach(dict(enumerate(groups)), projection, candidates, inside_parent=True)
    if unplaced or sorted(a for c in children.values() for a in c) != sorted(members):
        raise CommunityError("level-2 split lost or duplicated members")
    record.update(accepted=True, reason="ok", children=len(children))
    return [children[k] for k in sorted(children)], record


# ═══════════════════════════════════════════════════════════════════
# Views over a C12 document
# ═══════════════════════════════════════════════════════════════════


def as_json(communities: Communities | Mapping) -> Mapping:
    return communities.data if isinstance(communities, Communities) else communities


def all_topics(communities: Communities | Mapping, include_unassigned: bool = False) -> dict[str, dict]:
    """topic ID → record over both levels (carried copies are not repeated)."""
    data = as_json(communities)
    out = {}
    for level in sorted(data.get("levels", {})):
        for tid, t in data["levels"][level].items():
            if t.get("carried") or (t.get("unassigned") and not include_unassigned):
                continue
            out[tid] = t
    return out


def level_map(communities: Communities | Mapping, level: int | str,
              include_unassigned: bool = False) -> dict[str, str]:
    """class_id → topic of one level (every role; carried topics included)."""
    topics = as_json(communities).get("levels", {}).get(str(level), {})
    return {a: tid for tid, t in topics.items() if include_unassigned or not t.get("unassigned")
            for a in t["members"]}


def leaf_map(communities: Communities | Mapping, include_unassigned: bool = False) -> dict[str, str]:
    """class_id → its leaf topic (level 2 where its topic was split)."""
    return level_map(communities, 2, include_unassigned)


def _assignment(communities, level: int | str) -> dict[str, str]:
    return leaf_map(communities) if level == "leaf" else level_map(communities, level)


def topic_links(communities: Communities | Mapping, projection: Projection,
                level: int | str = "leaf") -> dict[tuple[str, str], float]:
    """Shared projected weight between topics of one level (keys sorted)."""
    assign = _assignment(communities, level)
    acc: dict[tuple[str, str], float] = defaultdict(float)
    for u, v, w in projection.edges:
        tu, tv = assign.get(u), assign.get(v)
        if tu and tv and tu != tv:
            acc[(tu, tv) if tu < tv else (tv, tu)] += w
    return dict(sorted(acc.items()))


def neighbours(communities: Communities | Mapping, projection: Projection,
               level: int | str = "leaf") -> dict[str, list[tuple[str, float]]]:
    """topic → [(neighbour, shared weight)], strongest first."""
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (a, b), w in topic_links(communities, projection, level).items():
        out[a].append((b, w))
        out[b].append((a, w))
    return {t: sorted(lst, key=lambda x: (-x[1], x[0])) for t, lst in out.items()}


def topic_dependencies(communities: Communities | Mapping, projection: Projection,
                       level: int | str = "leaf") -> list[dict]:
    """Directed dependencies between topics of one level from the directed
    facts of eligible atoms: `call_count` = distinct method pairs of the call
    relations, `weight` = Σ α·min(log2(1 + n), cap) per directed atom pair
    (unnormalized). Leaf level = `TopicDependsOn`."""
    assign = _assignment(communities, level)
    acc: dict[tuple[str, str], list] = {}
    for (u, v), rels in projection.directed.items():
        if u not in projection.eligible or v not in projection.eligible:
            continue
        tu, tv = assign.get(u), assign.get(v)
        if not tu or not tv or tu == tv:
            continue
        entry = acc.setdefault((tu, tv), [0.0, 0])
        for rel, facts in rels.items():
            entry[0] += relation_weight(rel, 1 if rel in PAIR_RELATIONS else len(facts))
            if rel in CALL_RELATIONS:
                entry[1] += len(facts)
    return [{"source": s, "target": t, "weight": round(w, 6), "call_count": c}
            for (s, t), (w, c) in sorted(acc.items())]


def file_topics(communities: Communities | Mapping, nodes: Mapping[str, list[dict]]) -> list[dict]:
    """`FileBelongsToTopic` rows `{file_id, topic_id, role}` (leiden spec §4,
    L-R40): a file belongs to every leaf topic holding one of its atoms
    (「未分類」 included); `primary` for the topic holding most of its
    declarations (types plus functions and methods, closures excluded),
    `secondary` otherwise; ties go to the smaller topic_id."""
    leaf = leaf_map(communities, include_unassigned=True)
    belongs: dict[str, set[str]] = defaultdict(set)
    declared: dict[str, Counter] = defaultdict(Counter)
    for row in nodes.get("Classes", []):
        tid, fid = leaf.get(row.get("class_id")), row.get("file_id")
        if tid and fid:
            belongs[fid].add(tid)
            if row.get("kind") != "package":
                declared[fid][tid] += 1
    for row in nodes.get("Methods", []):
        tid, fid = leaf.get(row.get("class_id")), row.get("file_id")
        if tid and fid and row.get("member_kind") != "closure":
            declared[fid][tid] += 1
    rows = []
    for fid, tids in sorted(belongs.items()):
        primary = min(tids, key=lambda t: (-declared[fid][t], t))
        rows += [{"file_id": fid, "topic_id": t, "role": "primary" if t == primary else "secondary"}
                 for t in sorted(tids)]
    return rows


def topic_tree(communities: Communities | Mapping, projection: Projection,
               names: Mapping[str, str] | None = None, summaries: Mapping[str, str] | None = None) -> list[dict]:
    """`topics/topic_tree.json` (§3.6, L-R27): the siblings' shape
    `[{"id", "name", "summary", "files", "subtopics": [...]}]` plus
    `class_ids`; `files` are relpaths. Level-1 topics by size, then ID;
    「未分類」 is left out. Names default to the hub labels."""
    data = as_json(communities)
    names, summaries = names or {}, summaries or {}
    levels = data.get("levels", {})

    def node(tid: str, t: Mapping) -> dict:
        files = sorted({projection.info[a]["path"] for a in t["members"]
                        if a in projection.info and projection.info[a].get("path")})
        return {"id": tid, "name": names.get(tid) or t.get("label") or tid, "summary": summaries.get(tid, ""),
                "files": files, "class_ids": list(t["members"])}

    tree = []
    for tid, t in sorted(((k, v) for k, v in levels.get("1", {}).items() if not v.get("unassigned")),
                         key=lambda kv: (-kv[1]["size"], kv[0])):
        entry = node(tid, t)
        entry["subtopics"] = [node(c, levels["2"][c]) for c in sorted(
            t.get("children") or [], key=lambda c: (-levels["2"][c]["size"], c))]
        tree.append(entry)
    return tree


TOPIC_TABLES = ("Topics", "ClassBelongsToTopic", "FileBelongsToTopic", "TopicDependsOn", "TopicContainsTopic")


def topic_rows(communities: Communities | Mapping, nodes: Mapping[str, list[dict]],
               names: Mapping | None = None, summaries: Mapping[str, str] | None = None) -> dict[str, list[dict]]:
    """Every row of the derived topic tables (leiden spec §4, L-R40), for
    the delete-then-insert write of one repo (parent amendment R10).

    `names` is names.json (or {topic_id: name}); without a name a topic gets
    its hub label (`label_source` hub). `summaries` maps topic IDs to page
    summaries. Each topic is one `Topics` row, carried copies included once;
    `ClassBelongsToTopic` holds every member of every topic with its role;
    `embedding` is left to Phase 10."""
    data = as_json(communities)
    repo, prefix = data.get("repo", ""), data.get("id_prefix", "a")
    named = dict((names or {}).get("names", names or {}))
    summaries = summaries or {}
    out: dict[str, list[dict]] = {table: [] for table in TOPIC_TABLES}
    for tid, t in sorted(all_topics(data, include_unassigned=True).items()):
        entry = named.get(tid)
        name = (entry.get("name") if isinstance(entry, Mapping) else entry) or t.get("label") or tid
        source = entry.get("label_source", "llm") if isinstance(entry, Mapping) else ("llm" if entry else None)
        out["Topics"].append({
            "topic_id": tid, "name": name, "summary": summaries.get(tid, ""), "repo": repo, "embedding": None,
            "level": t["level"], "parent_topic_id": t.get("parent"), "size": t["size"],
            "file_count": t["file_count"], "cohesion": t.get("cohesion"), "conductance": t.get("conductance"),
            "hub_class_id": t.get("hub"), "label_source": source or t.get("label_source", "hub"),
            "basis": "structure", "membership_hash": t["membership_hash"]})
        for cid, role in sorted(t["roles"].items()):
            out["ClassBelongsToTopic"].append({"edge_id": _edge_id("ClassBelongsToTopic", cid, tid, prefix),
                                               "class_id": cid, "topic_id": tid, "role": role})
        for child in t.get("children") or []:
            out["TopicContainsTopic"].append({"edge_id": _edge_id("TopicContainsTopic", tid, child, prefix),
                                              "parent_topic": tid, "child_topic": child})
    for row in file_topics(data, nodes):
        out["FileBelongsToTopic"].append({"edge_id": _edge_id("FileBelongsToTopic", row["file_id"], row["topic_id"], prefix),
                                          **row})
    for e in data.get("topic_edges", []):
        out["TopicDependsOn"].append({"edge_id": _edge_id("TopicDependsOn", e["source"], e["target"], prefix),
                                      "source_topic": e["source"], "target_topic": e["target"],
                                      "weight": e["weight"], "call_count": e["call_count"]})
    return out


# ═══════════════════════════════════════════════════════════════════
# Carry-over (§3.5, L-R38)
# ═══════════════════════════════════════════════════════════════════


def carry_over(new_levels: Mapping[str, Mapping[str, set[str]]], previous: Mapping | None,
               threshold: float) -> tuple[dict[str, tuple[str, float]], dict]:
    """Greedy one-to-one Jaccard matching per level over primary members →
    {new topic: (previous topic, jaccard)} at or above the threshold, and a
    summary. Carried copies and 「未分類」 are not matched."""
    if not previous:
        return {}, {}
    matches: dict[str, tuple[str, float]] = {}
    summary: dict[str, Any] = {"previous_membership_hash": previous.get("membership_hash"), "levels": {}}
    for level, topics in sorted(new_levels.items()):
        old = {tid: set(primary_members(t)) if "roles" in t else set(t.get("members", []))
               for tid, t in (previous.get("levels", {}).get(level) or {}).items()
               if not t.get("carried") and not t.get("unassigned")}
        owners: dict[str, list[str]] = defaultdict(list)
        for tid, mem in old.items():
            for a in mem:
                owners[a].append(tid)
        candidates = []
        for nid, mem in topics.items():
            for oid, k in Counter(t for a in mem for t in owners.get(a, ())).items():
                j = k / (len(mem) + len(old[oid]) - k)
                if j >= threshold:
                    candidates.append((-j, oid, nid, j))
        used_old: set[str] = set()
        level_matches = 0
        for _neg, oid, nid, j in sorted(candidates):
            if oid in used_old or nid in matches:
                continue
            used_old.add(oid)
            matches[nid] = (oid, j)
            level_matches += 1
        summary["levels"][level] = {"inherited": level_matches, "new": len(topics) - level_matches,
                                    "retired": sorted(set(old) - used_old)}
    return matches, summary


# ═══════════════════════════════════════════════════════════════════
# detect
# ═══════════════════════════════════════════════════════════════════


def _repo_of(projection: Projection, repo: str) -> str:
    """`params.repo`, else the single repo of the Classes rows."""
    if repo:
        return repo
    repos = {a.get("repo") for a in projection.info.values() if a.get("repo")}
    if len(repos) > 1:
        raise ValueError(f"atoms from several repos {sorted(repos)}; pass params.repo")
    return repos.pop() if repos else ""


def _coverage(groups: Mapping[str, Mapping] | None, levels: Mapping[str, dict], projection: Projection) -> dict | None:
    """L-R39: per-group resolution status; topics with a member in a group
    whose status is not "ok" are marked `partial`."""
    if groups is None:
        return None
    failed = {f for g in groups.values() if g.get("status", "ok") != "ok" for f in g.get("files", [])}
    partial = []
    for ts in levels.values():
        for tid, t in ts.items():
            if any(projection.info[a].get("path") in failed for a in t["members"]):
                t["partial"] = True
                if not t.get("carried"):
                    partial.append(tid)
    return {"groups": {gid: {"status": g.get("status", "ok"), "files": len(g.get("files", []))}
                       for gid, g in sorted(groups.items())},
            "failed_files": len(failed), "partial_topics": sorted(partial)}


def detect(projection: Projection, params: Params | Mapping[str, Any] | None = None,
           previous: Mapping | None = None, groups: Mapping[str, Mapping] | None = None) -> Communities:
    """Communities of one projection (module docstring). `previous` is the
    previous run's communities.json, for the Jaccard carry-over; `groups`
    maps resolution group IDs to {"status", "files": [relpath, ...]} (L-R39)."""
    p = Params.of(params)
    gn = _backend()
    t0 = time.perf_counter()
    repo = _repo_of(projection, p.repo)
    adj = projection.adjacency
    total_volume = volume(adj)
    edges = projection.edges
    _check_edges(edges)

    # Level 1: sweep, plateau, best trial. Without edges the backend is not called.
    sweep: list[dict] = []
    chosen: dict | None = None
    best_groups: list[list[str]] = []
    rule = "no-edges"
    notes: list[str] = []
    if edges:
        results = []
        calls = _leiden_calls(gn, edges, [(g, seed) for g in p.resolutions for seed in p.seeds], p)
        n = len(p.seeds)
        scorer = ModularityScorer(edges)
        for i, gamma in enumerate(p.resolutions):
            best, trials, seed_ari = _run_trials(gn, edges, gamma, p, calls[i * n:(i + 1) * n], scorer, notes)
            count = sum(1 for g in best.groups if len(g) >= p.min_size)
            results.append(best)
            sweep.append({"resolution": gamma, "count": count, "communities": len(best.groups),
                          "modularity": round(best.quality, 6), "membership_hash": best.hash,
                          "seed_ari": None if seed_ari is None else round(seed_ari, 6),
                          "trials": [t.record() for t in trials]})
        idx, rule = choose_resolution([s["count"] for s in sweep], p.band)
        chosen = sweep[idx]
        best_groups = results[idx].groups
    t_sweep = time.perf_counter()

    # Membership (L-R9): primary = clustered atoms; everything else is attached.
    merged, thin = merge_thin(best_groups, adj, p.min_size)
    candidates: dict[str, tuple] = {a: (None, "isolated") for a in projection.atoms if a not in adj}
    candidates.update({a: (None, "thin") for a in thin})
    candidates.update({a: (ROLE_OF_EXCLUSION[r], r) for a, r in projection.excluded.items()
                       if r in ROLE_OF_EXCLUSION})
    level1, unplaced = _attach(dict(enumerate(merged)), projection, candidates, inside_parent=False)
    unassigned = dict(unplaced)
    unassigned.update({a: "origin" for a, r in projection.excluded.items() if r not in ROLE_OF_EXCLUSION})

    # Records, level 2, 「未分類」, carried leaves, carry-over.
    levels: dict[str, dict[str, dict]] = {"1": {}, "2": {}}
    leaves: list[str] = []
    for k in sorted(level1):
        tid, rec = _record(1, level1[k], None, repo, p, projection, total_volume)
        children, split = _split(level1[k], gn, p, projection, notes)
        rec["split"] = split
        levels["1"][tid] = rec
        if children:
            for child in children:
                cid, crec = _record(2, child, tid, repo, p, projection, total_volume)
                levels["2"][cid] = crec
                rec["children"].append(cid)
                leaves.append(cid)
            rec["children"].sort()
        else:
            leaves.append(tid)
    unassigned_id = None
    if unassigned:
        unassigned_id, urec = _unassigned_record(list(unassigned), repo, p, projection)
        levels["1"][unassigned_id] = urec
    for tid, rec in list(levels["1"].items()):
        if not rec["children"]:
            levels["2"][tid] = {**rec, "carried": True}
    levels = {lv: dict(sorted(ts.items())) for lv, ts in levels.items()}
    matches, carry = carry_over(
        {lv: {tid: set(primary_members(t)) for tid, t in ts.items() if not t.get("carried") and not t.get("unassigned")}
         for lv, ts in levels.items()}, previous, p.jaccard)
    for ts in levels.values():
        for tid, t in ts.items():
            if tid in matches:
                t["inherited_from"], t["jaccard"] = matches[tid][0], round(matches[tid][1], 6)
    coverage = _coverage(groups, levels, projection)
    t_post = time.perf_counter()

    data: dict[str, Any] = {
        "version": VERSION,
        "backend": {"name": BACKEND, "version": backend_version(), "function": "leiden"},
        "profile": projection.profile,
        "repo": repo,
        "params": {"seeds": list(p.seeds), "randomness": p.randomness, "iterations": p.iterations,
                   "band": list(p.band), "subtopic_min_atoms": p.subtopic_min_atoms,
                   "resolutions": list(p.resolutions), "level2_resolution": p.level2_resolution,
                   "min_size": p.min_size, "jaccard": p.jaccard, "tolerance": p.tolerance,
                   "use_modularity": True, "trials_per_seed": 1},
        "sweep": sweep,
        "resolution": chosen["resolution"] if chosen else None,
        "resolution_rule": rule,
        "trials": chosen["trials"] if chosen else [],
        "seed_ari": chosen["seed_ari"] if chosen else None,
        "levels": levels,
        "leaves": sorted(leaves),
        "unassigned": sorted(unassigned),
        "unassigned_reasons": dict(sorted(unassigned.items())),
        "unassigned_topic": unassigned_id,
        "atoms": len(projection.atoms),
        "edges": len(edges),
        "projection_hash": projection.hash,
        "carry_over": carry,
        "coverage": coverage,
        "warnings": notes,
        "id_prefix": p.id_prefix,
    }
    data["topic_edges"] = topic_dependencies(data, projection)
    data["membership_hash"] = _document_hash(levels)
    data["metrics"] = _metrics(data, projection, chosen)
    return Communities(data=data, timings={"sweep": t_sweep - t0, "post": t_post - t_sweep,
                                           "total": time.perf_counter() - t0})


def _document_hash(levels: Mapping[str, Mapping[str, dict]]) -> str:
    """Hash of every level's full membership with roles (the determinism check)."""
    parts = []
    for lv, ts in sorted(levels.items()):
        for tid, t in sorted(ts.items()):
            parts.append(f"L{lv}|{tid}|" + "|".join(f"{a}={r}" for a, r in sorted(t["roles"].items())))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _metrics(data: dict, projection: Projection, chosen: dict | None) -> dict:
    l1 = {tid: t for tid, t in data["levels"]["1"].items() if not t.get("unassigned")}
    membership = {a: tid for tid, t in l1.items() for a, r in t["roles"].items() if r == "primary"}
    edges = projection.edges
    total = sum(w for _u, _v, w in edges)
    inside = sum(w for u, v, w in edges if membership.get(u) is not None and membership.get(u) == membership.get(v))
    roles = Counter(r for t in data["levels"]["1"].values() for r in t["roles"].values())
    in_topic = level_map(data, 1)
    assigned = sum(1 for a in projection.atoms if a in in_topic)
    conductances = [t["conductance"] for t in l1.values()]
    scorer = ModularityScorer(edges)
    return {
        "modularity": round(scorer(membership, 1.0), 6),
        "modularity_at_resolution": round(scorer(membership, chosen["resolution"]), 6) if chosen else None,
        "intra_weight_share": round(inside / total, 6) if total else None,
        "mean_conductance": round(sum(conductances) / len(conductances), 6) if conductances else None,
        "coverage": round(assigned / len(projection.atoms), 6) if projection.atoms else None,
        "topics": {"1": len(l1),
                   "2": sum(1 for t in data["levels"]["2"].values() if not t.get("carried"))},
        "leaves": len(data["leaves"]),
        "roles": dict(sorted(roles.items())),
        "unassigned": len(data["unassigned"]),
        "unassigned_by_reason": dict(sorted(Counter(data["unassigned_reasons"].values()).items())),
        "isolated_atoms": projection.stats.get("atoms_isolated", 0),
    }


# ═══════════════════════════════════════════════════════════════════
# Integrity (leiden spec §1 done criterion 2, L-R9)
# ═══════════════════════════════════════════════════════════════════


def check_integrity(communities: Communities | Mapping, projection: Projection) -> list[str]:
    """Problems with a C12 document against its projection; [] when it holds:
    each level covers every atom exactly once (「未分類」 included); every
    member is a real class_id with a valid role, `primary` only for eligible
    atoms and `unassigned` only in 「未分類」; children are disjoint, their
    union is the parent, and unsplit topics are carried to level 2 unchanged;
    `leaves` is the level-2 frontier without 「未分類」; topic IDs and hashes
    match the primary members."""
    data = as_json(communities)
    problems: list[str] = []
    repo = data.get("repo", "")
    levels = data.get("levels", {})
    for level in ("1", "2"):
        seen: Counter = Counter()
        for t in levels.get(level, {}).values():
            seen.update(t["members"])
        for a in projection.info:
            if seen[a] != 1:
                problems.append(f"level {level}: atom {a} appears {seen[a]} times")
        problems += [f"level {level}: member {a} is not a class_id" for a in seen if a not in projection.info]
    level1 = levels.get("1", {})
    for level, topics in levels.items():
        for tid, t in topics.items():
            prefix = tid.rsplit("_", 1)[0]
            if set(t["roles"]) != set(t["members"]):
                problems.append(f"{tid}: roles do not match members")
            for a, role in t["roles"].items():
                if role not in ROLES:
                    problems.append(f"{tid}: member {a} has role {role!r}")
                elif role == "primary" and a not in projection.eligible:
                    problems.append(f"{tid}: excluded atom {a} is primary")
                elif (role == "unassigned") != bool(t.get("unassigned")):
                    problems.append(f"{tid}: role {role!r} of {a} does not fit the topic")
            if t["membership_hash"] != membership_hash(primary_members(t)):
                problems.append(f"{tid}: membership_hash does not match its primary members")
            expected = unassigned_topic_id(repo, prefix) if t.get("unassigned") \
                else topic_id(repo, int(t["level"]), t["membership_hash"], prefix)
            if tid != expected:
                problems.append(f"{tid}: ID does not match level and membership")
            if t.get("carried") and (tid not in level1 or level1[tid]["members"] != t["members"]):
                problems.append(f"{tid}: carried topic differs from level 1")
            children = t.get("children") or []
            if children and not t.get("carried"):
                union: Counter = Counter()
                for c in children:
                    child = levels.get("2", {}).get(c, {})
                    union.update(child.get("members", []))
                    if child.get("parent") != tid:
                        problems.append(f"{tid}: child {c} does not name it as parent")
                if any(n > 1 for n in union.values()):
                    problems.append(f"{tid}: children overlap")
                if set(union) != set(t["members"]):
                    problems.append(f"{tid}: children's union differs from the parent")
    for tid, t in level1.items():
        if not t.get("children") and tid not in levels.get("2", {}):
            problems.append(f"{tid}: unsplit topic is not carried to level 2")
    frontier = {tid for tid, t in levels.get("2", {}).items() if not t.get("unassigned")}
    if set(data.get("leaves", [])) != frontier:
        problems.append("leaves differ from the level-2 frontier")
    unassigned_members = [a for t in level1.values() if t.get("unassigned") for a in t["members"]]
    if sorted(unassigned_members) != sorted(data.get("unassigned", [])):
        problems.append("the 未分類 topic and the unassigned list differ")
    return problems


def summary_line(communities: Communities | Mapping) -> str:
    """One progress line for the pipeline log."""
    data = as_json(communities)
    m = data.get("metrics", {})
    total = sum(len(t["members"]) for t in data.get("levels", {}).get("1", {}).values())
    return (f"{m.get('topics', {}).get('1', 0)} topics, {m.get('topics', {}).get('2', 0)} subtopics, "
            f"resolution {data.get('resolution')} ({data.get('resolution_rule')}), "
            f"modularity {m.get('modularity')}, seed ARI {data.get('seed_ari')}, "
            f"{len(data.get('unassigned', []))} of {total} atoms in {UNASSIGNED_NAME}")
