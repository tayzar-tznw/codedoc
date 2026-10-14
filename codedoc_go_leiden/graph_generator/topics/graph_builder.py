"""Phase 4a input: the atom projection (leiden spec §3.1–3.2).

`build_projection(nodes, edges, profile)` turns the run's C6 rows (table →
list of column dicts, as `build_node_rows` / `derive_edge_rows` return them
or as `graph_rows/<Table>.jsonl` holds them) into an undirected weighted
graph over atoms. A ledger row per edge lets a reviewer trace an unexpected
grouping back to the calls behind it (Codex §7.6).

* **Atoms** are `Classes` rows: struct, interface, named, alias and the
  per-file `<package>` pseudo-type. A `Methods` row belongs to the atom in
  its `class_id`. A closure row without a usable `class_id` belongs to the
  owner of its outermost enclosing declaration (its parent qualname in the
  same file, parent spec §4.2).
* **Eligibility** (profile `strict`): origin `package` or `command`, not
  generated, and in a file the primary build compiles (`build_included`,
  amendment L-R26). Test (`test`, `xtest`), generated and build-excluded
  atoms are not clustered; their weights are kept for the attachment step
  (§3.4, L-R9).
* **Relations** (exact facts only, Codex §7.3):

  | Relation | Rows | Atoms | α | cap |
  |---|---|---|---|---|
  | `call` | `MethodCalls`, resolution `types` or `convention:*`, callee not an interface method | owner(caller) – owner(callee) | 1.0 | 4 |
  | `iface_call` | `MethodCalls` whose callee is an interface method | owner(caller) – the interface | 1.0 | 4 |
  | `embed` | `ClassEmbeds` | child – embedded | 2.0 | 1 |
  | `implements` | `ClassImplements` | type – interface | 0.5 | 1 |

  `n_r(u,v)` counts distinct directed (caller, callee method) pairs in both
  directions for the call relations, where the caller is the calling
  method's outermost declaration, so a function and its closures count once
  (L-R26); it is 1 per atom pair for `embed` and `implements`.
  `weight = Σ_r α_r·min(log2(1 + n_r), cap_r)`. Self-pairs and zero or
  non-finite weights are dropped. `PossiblyCalls`, external targets,
  directory or package co-membership and text never enter.
* **Profiles** (combinable with `+`): `strict` (the default), `degree_norm`
  (`w / sqrt(deg_u·deg_v)` over the strict weighted degrees), `calls_only`
  (an ablation without `embed` and `implements`), `no_implements` (an
  ablation without `implements`, L-R26), `with_tests` (test atoms are
  clustered).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property

from .metrics import Adjacency, Edge, adjacency

RELATIONS: dict[str, tuple[float, float]] = {
    "call": (1.0, 4.0),
    "iface_call": (1.0, 4.0),
    "embed": (2.0, 1.0),
    "implements": (0.5, 1.0),
}
CALL_RELATIONS = ("call", "iface_call")
PAIR_RELATIONS = ("embed", "implements")  # n = 1 per atom pair
PROFILE_OPTIONS = ("degree_norm", "calls_only", "no_implements", "with_tests")
ELIGIBLE_ORIGINS = frozenset({"package", "command"})
TEST_ORIGINS = frozenset({"test", "xtest"})
ROLE_OF_EXCLUSION = {"test": "attached-test", "generated": "attached-generated", "build": "attached-excluded"}

Rows = Mapping[str, list[dict]]


@dataclass
class Projection:
    """The clustering input and everything later steps join back.

    `atoms` and `edges` are canonical (sorted IDs, `(min, max)` endpoints,
    sorted edges). `directed[(u, v)][relation]` holds the sorted directed
    fact pairs from atom u to atom v (method pairs for the call relations,
    the atom pair itself for `embed`/`implements`), for every pair of
    distinct atoms, eligible or not. `attach_edges` are the weights between
    an excluded atom and an eligible one."""

    profile: str
    atoms: list[str]
    edges: list[Edge]
    ledger: list[dict]
    info: dict[str, dict]
    excluded: dict[str, str]
    attach_edges: list[Edge]
    directed: dict[tuple[str, str], dict[str, list[tuple[str, str]]]]
    method_owner: dict[str, str]
    stats: dict[str, int]
    hash: str
    eligible: frozenset[str] = field(default_factory=frozenset)
    method_names: dict[str, str] = field(default_factory=dict)  # method_id → qualname
    functions: dict[str, list[str]] = field(default_factory=dict)  # atom → its methods/functions, no closures

    @cached_property
    def adjacency(self) -> Adjacency:
        return adjacency(self.edges)

    @cached_property
    def attach_adjacency(self) -> Adjacency:
        """excluded atom → {eligible atom: weight}."""
        adj: Adjacency = defaultdict(dict)
        for u, v, w in self.attach_edges:
            x, e = (u, v) if u in self.excluded else (v, u)
            adj[x][e] = w
        return dict(adj)

    @cached_property
    def degree(self) -> dict[str, float]:
        """Weighted degree of every eligible atom (0.0 for isolates)."""
        deg = {a: 0.0 for a in self.atoms}
        for u, nbrs in self.adjacency.items():
            deg[u] = sum(nbrs.values())
        return deg


def parse_profile(profile: str) -> tuple[str, frozenset[str]]:
    """Canonical profile name and its options: "strict", "degree_norm",
    "calls_only+with_tests"; "strict" may be combined and adds nothing."""
    parts = [p.strip() for p in (profile or "strict").split("+") if p.strip()]
    unknown = [p for p in parts if p != "strict" and p not in PROFILE_OPTIONS]
    if unknown:
        raise ValueError(f"unknown topic profile option(s) {unknown}; "
                         f"use strict or a '+' combination of {', '.join(PROFILE_OPTIONS)}")
    options = frozenset(p for p in parts if p != "strict")
    name = "+".join(p for p in PROFILE_OPTIONS if p in options) or "strict"
    return name, options


def relation_weight(relation: str, n: int) -> float:
    alpha, cap = RELATIONS[relation]
    return alpha * min(math.log2(1 + n), cap)


def _short_package(package: str) -> str:
    return package.rsplit("/", 1)[-1] if package else ""


def _atom_info(row: dict, file_row: dict) -> dict:
    package = row.get("package") or file_row.get("package") or ""
    return {
        "class_id": row["class_id"],
        "name": row.get("name") or "",
        "qualname": row.get("qualname") or row.get("name") or "",
        "kind": row.get("kind") or "",
        "package": package,
        "package_name": file_row.get("package_name") or _short_package(package),
        "module": row.get("module") or file_row.get("module") or "",
        "file_id": row.get("file_id") or "",
        "path": file_row.get("path") or "",
        "file_name": file_row.get("file_name") or os.path.basename(file_row.get("path") or ""),
        "origin": row.get("origin") or file_row.get("origin") or "",
        "generated": bool(row.get("generated", file_row.get("generated", False))),
        "build_included": bool(file_row.get("build_included", True)),
        "summary": row.get("summary") or "",
        "repo": row.get("repo") or file_row.get("repo") or "",
    }


def atom_label(info: dict) -> str:
    """Deterministic display label: `billing.FeeCalculator` for a type,
    `billing/fee.go` for a file's `<package>` pseudo-type."""
    if info.get("kind") == "package":
        return f"{info.get('package_name') or '?'}/{info.get('file_name') or info.get('path') or '?'}"
    pkg = info.get("package_name")
    return f"{pkg}.{info.get('name')}" if pkg else str(info.get("name"))


def atom_kind(info: dict) -> str:
    """Kind for prompts: the type kind, or ファイルの関数 for a `<package>`
    pseudo-type (it holds one file's functions)."""
    return "ファイルの関数" if info.get("kind") == "package" else info.get("kind") or "?"


def first_line(text: str, limit: int = 160) -> str:
    """First non-empty line of a summary, without Markdown heading marks."""
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return ""


def _exclusion(info: dict, options: frozenset[str]) -> str | None:
    if info["generated"]:
        return "generated"
    if not info["build_included"]:
        return "build"
    origin = info["origin"]
    if origin in TEST_ORIGINS:
        return None if "with_tests" in options else "test"
    if origin in ELIGIBLE_ORIGINS:
        return None
    return "origin"


def _outermost(methods: dict[str, dict]) -> dict[str, str]:
    """closure method_id → its outermost enclosing declaration in the same
    file (`F.func1.func2` → `F`); the nearest existing ancestor when an
    intermediate row is missing, the closure itself when none exists."""
    by_name = {(r.get("file_id"), r.get("qualname")): mid for mid, r in methods.items()}
    outer: dict[str, str] = {}
    for mid, row in methods.items():
        if row.get("member_kind") != "closure":
            continue
        found, qual = mid, row.get("qualname") or ""
        while "." in qual:
            qual = qual.rsplit(".", 1)[0]
            parent = by_name.get((row.get("file_id"), qual))
            if parent is not None:
                found = parent
                if methods[parent].get("member_kind") != "closure":
                    break
        outer[mid] = found
    return outer


def _method_owners(methods: dict[str, dict], outer: Mapping[str, str], atoms: Mapping[str, dict],
                   stats: Counter) -> dict[str, str]:
    """method_id → owning atom: the row's class_id, or for a closure without
    a usable one, its outermost declaration's class_id."""
    owner: dict[str, str] = {}
    for mid, row in methods.items():
        cid = row.get("class_id")
        if cid not in atoms and mid in outer:
            cid = methods[outer[mid]].get("class_id")
            if cid in atoms:
                stats["closures_owned_by_parent"] += 1
        if cid in atoms:
            owner[mid] = cid
        else:
            stats["methods_without_owner"] += 1
    return owner


def _fact_pairs(nodes: Rows, edges: Rows, info: dict[str, dict], options: frozenset[str],
                stats: Counter) -> tuple[dict, dict, dict[str, str]]:
    """Directed facts per atom pair: (facts[(u,v)][rel] = set of pairs,
    rows[(u,v)][rel] = input rows), and the method owners."""
    methods = {r["method_id"]: r for r in nodes.get("Methods", []) if r.get("method_id")}
    outer = _outermost(methods)
    owner = _method_owners(methods, outer, info, stats)
    facts: dict[tuple[str, str], dict[str, set]] = defaultdict(lambda: defaultdict(set))
    rows: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in edges.get("MethodCalls", []):
        resolution = r.get("resolution") or ""
        if resolution != "types" and not resolution.startswith("convention:"):
            stats["calls_excluded_resolution"] += 1
            continue
        caller, callee = r.get("caller_method"), r.get("callee_method")
        caller = outer.get(caller, caller)  # closures collapse into their declaration (L-R26)
        u, v = owner.get(caller), owner.get(callee)
        if u is None or v is None:
            stats["calls_unmapped"] += 1
            continue
        if u == v:
            stats["calls_same_atom"] += 1
            continue
        rel = "iface_call" if methods[callee].get("member_kind") == "interface_method" else "call"
        facts[(u, v)][rel].add((caller, callee))
        rows[(u, v)][rel] += 1
    for table, rel, src_col, dst_col in (("ClassEmbeds", "embed", "child_class", "embedded_class"),
                                         ("ClassImplements", "implements", "class_id", "interface_id")):
        if "calls_only" in options or (rel == "implements" and "no_implements" in options):
            stats[f"{rel}_ablated"] += len(edges.get(table, []))
            continue
        for r in edges.get(table, []):
            u, v = r.get(src_col), r.get(dst_col)
            if u not in info or v not in info:
                stats[f"{rel}_unmapped"] += 1
                continue
            if u == v:
                stats[f"{rel}_same_atom"] += 1
                continue
            facts[(u, v)][rel].add((u, v))
            rows[(u, v)][rel] += 1
    return facts, rows, owner


def build_projection(nodes: Rows, edges: Rows, profile: str = "strict") -> Projection:
    """The atom projection of one repo's C6 rows (module docstring)."""
    name, options = parse_profile(profile)
    stats: Counter = Counter()
    files = {r["file_id"]: r for r in nodes.get("Files", []) if r.get("file_id")}
    info: dict[str, dict] = {}
    for row in nodes.get("Classes", []):
        cid = row.get("class_id")
        if not cid:
            stats["classes_without_id"] += 1
            continue
        info[cid] = _atom_info(row, files.get(row.get("file_id"), {}))
    excluded = {cid: reason for cid, a in info.items() if (reason := _exclusion(a, options))}
    eligible = frozenset(info) - frozenset(excluded)
    for reason in excluded.values():
        stats[f"excluded_{reason}"] += 1

    facts, rows, owner = _fact_pairs(nodes, edges, info, options, stats)
    directed = {pair: {rel: sorted(fs) for rel, fs in sorted(rels.items())}
                for pair, rels in sorted(facts.items())}

    # Undirected aggregation: both directions of every atom pair.
    undirected: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for (u, v), rels in directed.items():
        key = (u, v) if u < v else (v, u)
        for rel, pairs in rels.items():
            entry = undirected[key].setdefault(rel, {"rows": 0, "facts": []})
            entry["rows"] += rows[(u, v)][rel]
            entry["facts"].extend(pairs)

    projected: list[tuple[str, str, float, dict]] = []
    attach: list[tuple[str, str, float, dict]] = []
    for (u, v), rels in sorted(undirected.items()):
        detail: dict[str, dict] = {}
        total = 0.0
        for rel in RELATIONS:
            if rel not in rels:
                continue
            fs = sorted(set(map(tuple, rels[rel]["facts"])))
            n = 1 if rel in PAIR_RELATIONS else len(fs)
            contribution = relation_weight(rel, n)
            total += contribution
            detail[rel] = {"rows": rels[rel]["rows"], "pairs": len(fs), "n": n,
                           "contribution": contribution, "facts": [list(f) for f in fs]}
        if not (total > 0 and math.isfinite(total)):
            stats["dropped_weights"] += 1
            continue
        if u in eligible and v in eligible:
            projected.append((u, v, total, detail))
        elif (u in eligible) != (v in eligible):
            if excluded[v if u in eligible else u] in ROLE_OF_EXCLUSION:
                attach.append((u, v, total, detail))

    weights = {(u, v): w for u, v, w, _ in projected}
    if "degree_norm" in options:
        deg: dict[str, float] = defaultdict(float)
        for (u, v), w in weights.items():
            deg[u] += w
            deg[v] += w
        weights = {(u, v): w / math.sqrt(deg[u] * deg[v]) for (u, v), w in weights.items()}

    ledger: list[dict] = []
    proj_edges: list[Edge] = []
    for u, v, raw, detail in projected:
        w = weights[(u, v)]
        if not (w > 0 and math.isfinite(w)):  # never pass zero or NaN to the backend (L-R8)
            stats["dropped_weights"] += 1
            continue
        proj_edges.append((u, v, w))
        ledger.append({"u": u, "v": v, "projected": True, "relations": detail,
                       "raw_weight": raw, "weight": w})
    attach_edges: list[Edge] = []
    for u, v, raw, detail in attach:
        attach_edges.append((u, v, raw))
        ledger.append({"u": u, "v": v, "projected": False, "relations": detail,
                       "raw_weight": raw, "weight": raw})

    method_names = {r["method_id"]: r.get("qualname") or r.get("name") or r["method_id"]
                    for r in nodes.get("Methods", []) if r.get("method_id")}
    owned: dict[str, list[str]] = defaultdict(list)
    for r in nodes.get("Methods", []):
        if r.get("method_id") in owner and r.get("member_kind") != "closure":
            owned[owner[r["method_id"]]].append(method_names[r["method_id"]])
    functions = {a: sorted(qs) for a, qs in sorted(owned.items())}
    atoms = sorted(eligible)
    connected = {x for u, v, _ in proj_edges for x in (u, v)}
    stats.update({"atoms_total": len(info), "atoms_eligible": len(atoms),
                  "atoms_isolated": len(eligible - connected), "edges": len(proj_edges),
                  "attach_edges": len(attach_edges), "methods_owned": len(owner)})
    for (_u, _v, _w, detail) in projected:
        for rel in detail:
            stats[f"edges_with_{rel}"] += 1
    return Projection(profile=name, atoms=atoms, edges=proj_edges, ledger=ledger, info=info,
                      excluded=dict(sorted(excluded.items())), attach_edges=attach_edges,
                      directed=directed, method_owner=owner, stats=dict(sorted(stats.items())),
                      hash=projection_hash(name, atoms, proj_edges, attach_edges, excluded, info),
                      eligible=eligible, method_names=method_names, functions=functions)


def projection_hash(profile: str, atoms: list[str], edges: list[Edge], attach_edges: list[Edge],
                    excluded: Mapping[str, str], info: Mapping[str, dict]) -> str:
    """16 hex chars over everything `community.detect` reads: the profile,
    atoms, edge weights (12 significant digits), exclusions and each atom's
    file (isolates attach by file)."""
    doc = {
        "profile": profile,
        "atoms": atoms,
        "edges": [[u, v, f"{w:.12g}"] for u, v, w in edges],
        "attach": [[u, v, f"{w:.12g}"] for u, v, w in attach_edges],
        "excluded": sorted(excluded.items()),
        "files": sorted((cid, a.get("file_id") or "") for cid, a in info.items()),
    }
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_ledger(projection: Projection, path: str) -> None:
    """`topics/projection.jsonl`: one ledger row per line, written atomically."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in projection.ledger:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)
