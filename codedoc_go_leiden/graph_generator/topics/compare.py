"""compare-topics: LLM topics against Leiden topics on one repo (leiden spec §6).

`compare_topics(llm_tree, communities, nodes, edges, ...)` returns the
`topic_comparison.json` document and `render_markdown(result)` the
`topic_comparison.md` report (English; Japanese topic names as they are).
No metric picks a winner: every group is shown side by side (Codex §13.5).

Views:
* **Files.** The LLM topics are codedoc_go's `topic_tree.json`: a top-level
  topic's files are its own plus its subtopics'; the leaves are the
  subtopics, plus a residual leaf for top-level files that no subtopic
  lists; overlaps are kept. A Leiden topic's files are its members' files
  (as `FileBelongsToTopic`). Paths that are not repo files are reported as
  invented and dropped. File metrics use production files only (origin
  `package` or `command`, not generated, compiled by the primary build, as
  the eligible atoms), so tests attached to Leiden
  topics and tests listed by the LLM do not count either way.
* **Atoms.** Both are projected onto the same atom graph (§3.2): a Leiden
  atom has its topic; an LLM atom takes the topic of its file. Atoms of
  files in several LLM topics, atoms without an LLM topic and atoms in
  Leiden's 「未分類」 are counted and kept out of the structural metrics.
* **Ablation** (L-R26): Leiden is run again without `implements` edges and
  its level-1 partition is compared with the reported one.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

from .community import Communities, as_json, detect, level_map
from .graph_builder import ELIGIBLE_ORIGINS, Projection, build_projection
from .metrics import adjacency, ami, ari, conductance, modularity, nmi, volume
from .reports import aggregate_grounding, build_name_set, grounding_check

VERSION = 1
FEATURE_COVER = 0.8
LEVELS = ("top", "leaf")


def _norm_path(path: str) -> str:
    path = str(path).strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


class CompareInputError(ValueError):
    """The two runs did not see the same input (L-R27)."""


def _sha(record: object) -> object:
    return record.get("sha256") if isinstance(record, Mapping) else record


def hash_mismatches(entities_a: Mapping[str, object], entities_b: Mapping[str, object]) -> list[str]:
    """Relpaths whose sha256 differs between two `entities.json` file maps
    (records or plain hashes), or that only one of them has; [] means both
    runs saw the same files."""
    out = []
    for rel in sorted(set(entities_a) | set(entities_b)):
        a, b = entities_a.get(rel), entities_b.get(rel)
        if a is None or b is None or _sha(a) != _sha(b):
            out.append(rel)
    return out


def run_mismatches(llm_run: Mapping, leiden_run: Mapping) -> list[str]:
    """Why two runs cannot be compared (L-R27): each run is
    `{"entities": {relpath: record or sha256}, "build": …, "mode": …,
    "resolutions_hash": …}`; [] when entity hashes, build configuration,
    mode and the `resolutions.json` hash are all identical."""
    problems = []
    changed = hash_mismatches(llm_run.get("entities") or {}, leiden_run.get("entities") or {})
    if changed:
        problems.append(f"{len(changed)} files differ: " + ", ".join(changed[:10]) + (" …" if len(changed) > 10 else ""))
    for key in ("build", "mode", "resolutions_hash"):
        if llm_run.get(key) != leiden_run.get(key):
            problems.append(f"{key} differs: {llm_run.get(key)!r} vs {leiden_run.get(key)!r}")
    return problems


# ═══════════════════════════════════════════════════════════════════
# Views
# ═══════════════════════════════════════════════════════════════════


def _llm_view(tree: list[dict], repo_files: set[str], production: set[str]) -> dict:
    """{"top": {key: {"name", "files"}}, "leaf": {...}, "invented": [...],
    "non_production": n} with production file sets."""
    top: dict[str, dict] = {}
    leaf: dict[str, dict] = {}
    invented: set[str] = set()
    listed: set[str] = set()

    def real(paths: Iterable[str]) -> set[str]:
        out = set()
        for p in paths or []:
            p = _norm_path(p)
            listed.add(p)
            if p in repo_files:
                out.add(p)
            else:
                invented.add(p)
        return out

    for i, topic in enumerate(tree or []):
        key = str(topic.get("id") or f"llm-{i + 1}")
        name = topic.get("name") or key
        own = real(topic.get("files") or topic.get("linked_files") or [])
        subs = topic.get("subtopics") or []
        sub_sets = []
        for j, sub in enumerate(subs):
            files = real(sub.get("files") or sub.get("linked_files") or [])
            sub_sets.append(files)
            leaf[f"{key}/{j + 1}"] = {"name": f"{name} / {sub.get('name') or j + 1}", "files": files & production}
        everything = own.union(*sub_sets)
        top[key] = {"name": name, "files": everything & production}
        rest = everything - set().union(*sub_sets) if subs else everything
        if rest or not subs:
            leaf[f"{key}/-" if subs else key] = {"name": name, "files": rest & production}
    return {"top": top, "leaf": leaf, "invented": sorted(invented),
            "non_production": len({p for p in listed if p in repo_files} - production)}


def _leiden_view(data: Mapping, projection: Projection, names: Mapping[str, str], production: set[str]) -> dict:
    view: dict[str, dict] = {}
    levels = data.get("levels", {})
    every = {tid: t for lv in levels.values() for tid, t in lv.items()}
    top = sorted(tid for tid, t in levels.get("1", {}).items() if not t.get("unassigned"))
    for level, ids in (("top", top), ("leaf", data.get("leaves", []))):
        view[level] = {}
        for tid in ids:
            t = every[tid]
            files = {projection.info[a]["path"] for a in t["members"] if a in projection.info}
            view[level][tid] = {"name": names.get(tid) or t.get("label") or tid,
                                "files": files & production,
                                "atoms": {a for a in t["members"] if a in projection.eligible}}
    return view


def _topics_of_file(view: Mapping[str, dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for key, t in view.items():
        for f in t["files"]:
            out[f].add(key)
    return out


# ═══════════════════════════════════════════════════════════════════
# Metric groups
# ═══════════════════════════════════════════════════════════════════


def _dist(values: list[int]) -> dict:
    if not values:
        return {"min": None, "median": None, "max": None, "mean": None}
    return {"min": min(values), "median": statistics.median(values), "max": max(values),
            "mean": round(sum(values) / len(values), 3)}


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 6) if d else None


def _coverage(view: Mapping[str, dict], production: set[str], atoms: list[str],
              atom_topics: Mapping[str, set[str]]) -> dict:
    top_of = _topics_of_file(view["top"])
    leaf_of = _topics_of_file(view["leaf"])
    covered_files = {f for f in production if top_of.get(f)}
    covered_atoms = [a for a in atoms if atom_topics.get(a)]
    return {"files": len(covered_files), "files_total": len(production),
            "file_share": _ratio(len(covered_files), len(production)),
            "atoms": len(covered_atoms), "atoms_total": len(atoms),
            "atom_share": _ratio(len(covered_atoms), len(atoms)),
            "files_in_several_topics": sum(1 for f in production if len(top_of.get(f, ())) > 1),
            "files_in_several_leaves": sum(1 for f in production if len(leaf_of.get(f, ())) > 1)}


def _sizes(view: Mapping[str, dict], atoms_of: Mapping[str, set[str]], covered_atoms: int) -> dict:
    files = [len(t["files"]) for t in view.values()]
    atom_counts = [len(atoms_of.get(k, ())) for k in view]
    return {"topics": len(view), "files": _dist(files), "atoms": _dist(atom_counts),
            "largest_atom_share": _ratio(max(atom_counts, default=0), covered_atoms)}


def _agreement(llm: Mapping[str, dict], leiden: Mapping[str, dict], production: set[str]) -> dict:
    llm_of, leiden_of = _topics_of_file(llm), _topics_of_file(leiden)
    single = [f for f in sorted(production) if len(llm_of.get(f, ())) == 1 and len(leiden_of.get(f, ())) == 1]
    a = [next(iter(llm_of[f])) for f in single]
    b = [next(iter(leiden_of[f])) for f in single]
    best_llm = _best_matches(llm, leiden)
    best_leiden = _best_matches(leiden, llm)
    return {"files": len(single),
            "ami": round(ami(a, b), 6) if len(single) >= 2 else None,
            "ari": round(ari(a, b), 6) if len(single) >= 2 else None,
            "nmi": round(nmi(a, b), 6) if len(single) >= 2 else None,
            "jaccard_llm_to_leiden": _mean([m["jaccard"] for m in best_llm]),
            "jaccard_leiden_to_llm": _mean([m["jaccard"] for m in best_leiden]),
            "matches": best_llm}


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _best_matches(source: Mapping[str, dict], target: Mapping[str, dict]) -> list[dict]:
    """For each source topic with files: the target topic of highest file
    Jaccard (ties: larger overlap, then smaller target size, then key)."""
    target_of = _topics_of_file(target)
    out = []
    for key, s in source.items():
        if not s["files"]:
            continue
        overlap = Counter(t for f in s["files"] for t in target_of.get(f, ()))
        best = None
        for t, k in overlap.items():
            j = k / (len(s["files"]) + len(target[t]["files"]) - k)
            cand = (-j, -k, len(target[t]["files"]), t)
            if best is None or cand < best[0]:
                best = (cand, t, k, j)
        row = {"key": key, "name": s["name"], "files": len(s["files"]),
               "match": None, "match_name": None, "match_files": 0, "overlap": 0, "jaccard": 0.0}
        if best:
            _c, t, k, j = best
            row.update(match=t, match_name=target[t]["name"], match_files=len(target[t]["files"]),
                       overlap=k, jaccard=round(j, 6))
        out.append(row)
    return out


def _directory_alignment(view: Mapping[str, dict], production: set[str], files: Mapping[str, dict]) -> dict:
    of = _topics_of_file(view)
    single = [f for f in sorted(production) if len(of.get(f, ())) == 1]
    topic = [next(iter(of[f])) for f in single]
    top_dir = [f.split("/", 1)[0] if "/" in f else "." for f in single]
    package = [files[f].get("package") or files[f].get("directory") or "." for f in single]
    ok = len(single) >= 2
    return {"files": len(single),
            "ami_top_directory": round(ami(topic, top_dir), 6) if ok else None,
            "ami_package": round(ami(topic, package), 6) if ok else None}


def _structure(llm_atoms: Mapping[str, set[str]], leiden_atoms: Mapping[str, str], projection: Projection) -> dict:
    atoms = projection.atoms
    multi = [a for a in atoms if len(llm_atoms.get(a, ())) > 1]
    no_llm = [a for a in atoms if not llm_atoms.get(a)]
    unassigned = [a for a in atoms if a not in leiden_atoms]
    shared = {a for a in atoms if len(llm_atoms.get(a, ())) == 1 and a in leiden_atoms}
    sub_edges = [(u, v, w) for u, v, w in projection.edges if u in shared and v in shared]
    adj = adjacency(sub_edges)
    vol = volume(adj)
    total = sum(w for _u, _v, w in sub_edges)
    out = {"shared_atoms": len(shared), "shared_edges": len(sub_edges),
           "atoms_in_multi_topic_files": len(multi), "atoms_without_llm_topic": len(no_llm),
           "atoms_unassigned_by_leiden": len(unassigned)}
    for method, labels in (("llm", {a: next(iter(llm_atoms[a])) for a in shared}),
                           ("leiden", {a: leiden_atoms[a] for a in shared})):
        groups: dict[str, list[str]] = defaultdict(list)
        for a, t in labels.items():
            groups[t].append(a)
        inside = sum(w for u, v, w in sub_edges if labels[u] == labels[v])
        conds = [conductance(adj, g, vol) for _t, g in sorted(groups.items())]
        out[method] = {"modularity": round(modularity(sub_edges, labels), 6),
                       "intra_weight_share": _ratio_f(inside, total),
                       "mean_conductance": _mean(conds), "topics": len(groups)}
    return out


def _ratio_f(n: float, d: float) -> float | None:
    return round(n / d, 6) if d else None


def _llm_run_ari(tree_a: list[dict], tree_b: list[dict], repo_files: set[str], production: set[str]) -> dict:
    a_of = _topics_of_file(_llm_view(tree_a, repo_files, production)["top"])
    b_of = _topics_of_file(_llm_view(tree_b, repo_files, production)["top"])
    single = [f for f in sorted(production) if len(a_of.get(f, ())) == 1 and len(b_of.get(f, ())) == 1]
    a = [next(iter(a_of[f])) for f in single]
    b = [next(iter(b_of[f])) for f in single]
    return {"files": len(single), "ari": round(ari(a, b), 6) if len(single) >= 2 else None,
            "ami": round(ami(a, b), 6) if len(single) >= 2 else None}


def topic_names(llm_tree: list[dict] | None, communities: Communities | Mapping,
                leiden_names: Mapping[str, object] | None = None) -> set[str]:
    """Every topic name of both runs (LLM topics and subtopics, Leiden names
    and labels): pages quote them, and they are not code identifiers."""
    out: set[str] = set()
    for t in llm_tree or []:
        out.add(t.get("name") or "")
        out.update(sub.get("name") or "" for sub in t.get("subtopics") or [])
    named = dict((leiden_names or {}).get("names", leiden_names or {}))
    out.update((e.get("name") if isinstance(e, Mapping) else e) or "" for e in named.values())
    out.update(t.get("label") or "" for lv in as_json(communities).get("levels", {}).values() for t in lv.values())
    out.discard("")
    return out


def _cost(stats: Mapping | None) -> dict | None:
    if not stats:
        return None
    phases = {k: dict(v) for k, v in stats.items() if isinstance(v, Mapping)}
    total: dict[str, float] = defaultdict(float)
    for v in phases.values():
        for field in ("calls", "input_tokens", "output_tokens", "seconds"):
            if isinstance(v.get(field), (int, float)):
                total[field] += v[field]
    phases["total"] = {k: round(v, 3) for k, v in total.items()}
    return phases


ABLATION_PARAMS = ("seeds", "randomness", "iterations", "band", "subtopic_min_atoms", "resolutions",
                   "level2_resolution", "min_size", "jaccard", "tolerance")


def _ablation(data: Mapping, nodes: Mapping, edges: Mapping, projection: Projection) -> dict:
    """Leiden without `implements` edges, with the reported run's parameters;
    agreement of the two level-1 partitions on atoms both assign."""
    if {"calls_only", "no_implements"} & set(str(data.get("profile", "")).split("+")):
        return {"profile": None, "reason": "the reported profile already has no implements edges"}
    params = {k: data["params"][k] for k in ABLATION_PARAMS if k in data.get("params", {})}
    ablated = detect(build_projection(nodes, edges, f"{data.get('profile') or 'strict'}+no_implements"),
                     {**params, "repo": data.get("repo") or ""}).data
    base = {a: t for a, t in level_map(data, 1).items() if a in projection.eligible}
    other = {a: t for a, t in level_map(ablated, 1).items() if a in projection.eligible}
    common = sorted(set(base) & set(other))
    a, b = [base[x] for x in common], [other[x] for x in common]
    ok = len(common) >= 2
    return {"profile": ablated["profile"], "resolution": ablated["resolution"],
            "topics": ablated["metrics"]["topics"]["1"], "modularity": ablated["metrics"]["modularity"],
            "membership_hash": ablated["membership_hash"], "atoms_compared": len(common),
            "ami_vs_reported": round(ami(a, b), 6) if ok else None,
            "ari_vs_reported": round(ari(a, b), 6) if ok else None}


def _cover_count(feature: set[str], topics: Mapping[str, dict]) -> int | None:
    """Topics needed (greedy) to cover FEATURE_COVER of a feature's files."""
    need = math.ceil(FEATURE_COVER * len(feature))
    covered: set[str] = set()
    count = 0
    while len(covered) < need:
        gains = {k: len((t["files"] & feature) - covered) for k, t in topics.items()}
        best = min(gains, key=lambda k: (-gains[k], k), default=None)
        if best is None or gains[best] == 0:
            return None
        covered |= topics[best]["files"] & feature
        count += 1
    return count


def _features(features: Mapping, views: Mapping[str, Mapping[str, Mapping[str, dict]]],
              production: set[str]) -> dict:
    rows = []
    for feat in features.get("features", []):
        listed = {_norm_path(p) for p in feat.get("files", [])}
        fset = listed & production
        row = {"name": feat.get("name"), "files": len(fset), "not_production_or_missing": len(listed - fset)}
        for method, by_level in views.items():
            for level, topics in by_level.items():
                cell = {"coverage": None, "best": None, "dispersion": None, "irrelevant": None}
                if fset:
                    best = min(topics, default=None,
                               key=lambda k: (-len(topics[k]["files"] & fset), len(topics[k]["files"]), k))
                    hit = len(topics[best]["files"] & fset) if best else 0
                    if best and hit:
                        g = topics[best]["files"]
                        cell = {"coverage": round(hit / len(fset), 6), "best": topics[best]["name"],
                                "dispersion": _cover_count(fset, topics),
                                "irrelevant": round(len(g - fset) / len(g), 6)}
                    else:
                        cell["coverage"] = 0.0
                row[f"{method}_{level}"] = cell
        rows.append(row)
    summary = {}
    for method, by_level in views.items():
        for level in by_level:
            cells = [r[f"{method}_{level}"] for r in rows if r["files"]]
            summary[f"{method}_{level}"] = {
                "mean_coverage": _mean([c["coverage"] for c in cells if c["coverage"] is not None]),
                "mean_dispersion": _mean([c["dispersion"] for c in cells if c["dispersion"] is not None]),
                "uncoverable": sum(1 for c in cells if c["dispersion"] is None),
                "mean_irrelevant": _mean([c["irrelevant"] for c in cells if c["irrelevant"] is not None])}
    return {"features": rows, "summary": summary}


# ═══════════════════════════════════════════════════════════════════
# compare_topics
# ═══════════════════════════════════════════════════════════════════


def compare_topics(llm_tree: list[dict], communities: Communities | Mapping, nodes: Mapping[str, list[dict]],
                   edges: Mapping[str, list[dict]], features: Mapping | None = None,
                   llm_tree2: list[dict] | None = None, *, leiden_names: Mapping[str, str] | None = None,
                   communities2: Communities | Mapping | None = None, llm_stats: Mapping | None = None,
                   leiden_stats: Mapping | None = None, llm_pages: Mapping[str, str] | None = None,
                   leiden_pages: Mapping[str, str] | None = None, projection: Projection | None = None,
                   ablation: bool = True, llm_run: Mapping | None = None,
                   leiden_run: Mapping | None = None) -> dict:
    """Every metric of leiden spec §6 for one repo (module docstring).

    `llm_run` / `leiden_run` describe the two runs for the L-R27 check
    (`run_mismatches`); compare-topics passes both, and any difference
    raises CompareInputError. `leiden_names` maps topic IDs to names
    (names.json; hub labels otherwise); `communities2` is a rerun on the
    same input; `*_stats` are the runs' `topics/stats.json`; `*_pages` map
    page names to Markdown; `ablation` runs the no_implements Leiden (needs
    graspologic-native)."""
    if (llm_run is None) != (leiden_run is None):
        raise ValueError("pass both llm_run and leiden_run, or neither")
    if llm_run is not None:
        problems = run_mismatches(llm_run, leiden_run)
        if problems:
            raise CompareInputError("the LLM and Leiden runs did not see the same input; run both on the same "
                                    "commit, build configuration and mode: " + "; ".join(problems))
    data = as_json(communities)
    projection = projection or build_projection(nodes, edges, data.get("profile", "strict"))
    files = {_norm_path(f["path"]): f for f in nodes.get("Files", []) if f.get("path")}
    repo_files = set(files)
    production = {p for p, f in files.items() if f.get("origin") in ELIGIBLE_ORIGINS and not f.get("generated")
                  and f.get("build_included", True)}
    names = dict(leiden_names or {})
    llm = _llm_view(llm_tree, repo_files, production)
    leiden = _leiden_view(data, projection, names, production)
    atom_file = {a: projection.info[a]["path"] for a in projection.atoms}

    llm_top_of = _topics_of_file(llm["top"])
    llm_atoms = {a: llm_top_of.get(atom_file[a], set()) for a in projection.atoms}
    leiden_l1 = {a: t for a, t in level_map(data, 1).items() if a in projection.eligible}
    leiden_atom_topics = {a: {t} for a, t in leiden_l1.items()}

    def atoms_of_llm(view: Mapping[str, dict]) -> dict[str, set[str]]:
        file_atoms: dict[str, set[str]] = defaultdict(set)
        for a, f in atom_file.items():
            file_atoms[f].add(a)
        return {k: set().union(*(file_atoms.get(f, set()) for f in t["files"])) for k, t in view.items()}

    llm_covered = sum(1 for a in projection.atoms if llm_atoms.get(a))
    result: dict = {
        "version": VERSION,
        "inputs": {"repo": data.get("repo"), "profile": data.get("profile"), "runs_verified": llm_run is not None,
                   "projection_hash": data.get("projection_hash"), "membership_hash": data.get("membership_hash"),
                   "production_files": len(production), "eligible_atoms": len(projection.atoms),
                   "llm_topics": len(llm["top"]), "llm_leaves": len(llm["leaf"]),
                   "leiden_topics": len(leiden["top"]), "leiden_leaves": len(leiden["leaf"]),
                   "leiden_resolution": data.get("resolution"), "leiden_resolution_rule": data.get("resolution_rule")},
        "coverage": {
            "llm": {**_coverage(llm, production, projection.atoms, llm_atoms),
                    "invented_paths": len(llm["invented"]), "invented": llm["invented"][:50],
                    "non_production_files_listed": llm["non_production"]},
            "leiden": _coverage(leiden, production, projection.atoms, leiden_atom_topics),
        },
        "size": {
            "llm": {"top": _sizes(llm["top"], atoms_of_llm(llm["top"]), llm_covered),
                    "leaf": _sizes(llm["leaf"], atoms_of_llm(llm["leaf"]), llm_covered)},
            "leiden": {lv: _sizes(leiden[lv], {k: t["atoms"] for k, t in leiden[lv].items()}, len(leiden_l1))
                       for lv in LEVELS},
        },
        "agreement": {lv: _agreement(llm[lv], leiden[lv], production) for lv in LEVELS},
        "directory_alignment": {"llm": _directory_alignment(llm["top"], production, files),
                                "leiden": _directory_alignment(leiden["top"], production, files)},
        "structure": _structure(llm_atoms, leiden_l1, projection),
        "stability": {
            "leiden": {"seed_ari": data.get("seed_ari"),
                       "rerun_hash_equal": (as_json(communities2).get("membership_hash") == data.get("membership_hash"))
                       if communities2 is not None else None},
            "llm": _llm_run_ari(llm_tree, llm_tree2, repo_files, production) if llm_tree2 is not None else None,
        },
        "cost": {"llm": _cost(llm_stats), "leiden": _cost(leiden_stats)},
        "grounding": {"llm": None, "leiden": None},
        "features": None,
        "ablation": _ablation(data, nodes, edges, projection) if ablation else None,
    }
    result["structure"]["leiden_full_graph_modularity"] = data.get("metrics", {}).get("modularity")
    if llm_pages or leiden_pages:
        name_set = build_name_set(nodes)
        quoted = topic_names(llm_tree, data, names)  # both runs' topic names, skipped on every page
        for method, pages in (("llm", llm_pages), ("leiden", leiden_pages)):
            if pages:
                result["grounding"][method] = aggregate_grounding(
                    grounding_check(md, name_set, quoted) for _n, md in sorted(pages.items()))
    if features:
        result["features"] = _features(features, {"llm": {lv: llm[lv] for lv in LEVELS},
                                                  "leiden": {lv: leiden[lv] for lv in LEVELS}}, production)
    return result


# ═══════════════════════════════════════════════════════════════════
# Markdown
# ═══════════════════════════════════════════════════════════════════


def _f(x: object, digits: int = 3) -> str:
    if x is None:
        return "–"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def _pct(n: int, d: int) -> str:
    return f"{n} / {d} ({100 * n / d:.1f} %)" if d else f"{n} / 0"


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _dist_text(d: Mapping) -> str:
    return " / ".join(_f(d.get(k), 1) for k in ("min", "median", "max"))


def render_markdown(result: Mapping) -> str:
    """`topic_comparison.md` from a `compare_topics` result."""
    i = result["inputs"]
    cov, size = result["coverage"], result["size"]
    lines = [f"# Topic comparison: LLM topics vs Leiden topics ({i.get('repo') or 'repo'})", "",
             "Both methods are measured on the same repo and commit. No single metric picks a winner; "
             "read the groups side by side.", "",
             "| Input | Value |", "|---|---|",
             f"| Production files | {i['production_files']} |",
             f"| Eligible atoms (types and per-file `<package>` pseudo-types) | {i['eligible_atoms']} |",
             f"| LLM topics / leaves | {i['llm_topics']} / {i['llm_leaves']} |",
             f"| Leiden topics / leaves | {i['leiden_topics']} / {i['leiden_leaves']} |",
             f"| Leiden profile, resolution (rule) | {i.get('profile')}, {_f(i.get('leiden_resolution'), 2)} "
             f"({i.get('leiden_resolution_rule')}) |",
             f"| Projection hash, membership hash | `{i.get('projection_hash')}`, `{i.get('membership_hash')}` |",
             f"| Same files, build configuration, mode and resolutions (checked) | {_f(i.get('runs_verified'))} |",
             ""]

    lines += ["## Coverage", "", "| Metric | LLM | Leiden |", "|---|---|---|",
              f"| Production files with a topic | {_pct(cov['llm']['files'], cov['llm']['files_total'])} | "
              f"{_pct(cov['leiden']['files'], cov['leiden']['files_total'])} |",
              f"| Eligible atoms with a topic | {_pct(cov['llm']['atoms'], cov['llm']['atoms_total'])} | "
              f"{_pct(cov['leiden']['atoms'], cov['leiden']['atoms_total'])} |",
              f"| Files in several topics (top level) | {cov['llm']['files_in_several_topics']} | "
              f"{cov['leiden']['files_in_several_topics']} |",
              f"| Files in several leaves | {cov['llm']['files_in_several_leaves']} | "
              f"{cov['leiden']['files_in_several_leaves']} |",
              f"| Listed paths that are not repo files (invented) | {cov['llm']['invented_paths']} | – |", ""]
    if cov["llm"]["invented"]:
        lines += ["Invented paths: " + ", ".join(f"`{p}`" for p in cov["llm"]["invented"][:20]), ""]

    lines += ["## Size", "", "| Metric | LLM top | LLM leaf | Leiden level 1 | Leiden leaf |", "|---|---|---|---|---|"]
    cols = [size["llm"]["top"], size["llm"]["leaf"], size["leiden"]["top"], size["leiden"]["leaf"]]
    lines += [f"| Topics | " + " | ".join(str(c["topics"]) for c in cols) + " |",
              f"| Files per topic (min / median / max) | " + " | ".join(_dist_text(c["files"]) for c in cols) + " |",
              f"| Atoms per topic (min / median / max) | " + " | ".join(_dist_text(c["atoms"]) for c in cols) + " |",
              f"| Largest topic's share of covered atoms | " + " | ".join(_f(c["largest_atom_share"]) for c in cols)
              + " |", ""]

    lines += ["## Agreement", "",
              "AMI and ARI are chance-corrected (read them first); NMI is for reference. They use production "
              "files with exactly one topic in both methods.", "",
              "| Granularity | Files compared | AMI | ARI | NMI | Mean best Jaccard LLM→Leiden | Leiden→LLM |",
              "|---|---|---|---|---|---|---|"]
    for lv in LEVELS:
        a = result["agreement"][lv]
        lines.append(f"| {lv} | {a['files']} | {_f(a['ami'])} | {_f(a['ari'])} | {_f(a['nmi'])} | "
                     f"{_f(a['jaccard_llm_to_leiden'])} | {_f(a['jaccard_leiden_to_llm'])} |")
    lines += ["", "### Best Leiden match for each LLM topic (top level)", "",
              "| LLM topic | Files | Best Leiden topic | Files | Overlap | Jaccard |", "|---|---|---|---|---|---|"]
    for m in result["agreement"]["top"]["matches"]:
        lines.append(f"| {_cell(m['name'])} | {m['files']} | {_cell(m['match_name'] or '–')} | {m['match_files']} | "
                     f"{m['overlap']} | {_f(m['jaccard'])} |")
    lines.append("")

    d = result["directory_alignment"]
    lines += ["## Directory alignment", "",
              "AMI between each method's topics and the layout (production files with exactly one topic). "
              "Near 1 means the topics repeat the directories; near 0 means they ignore them.", "",
              "| Method | Files | AMI vs top-level directory | AMI vs package |", "|---|---|---|---|",
              f"| LLM | {d['llm']['files']} | {_f(d['llm']['ami_top_directory'])} | {_f(d['llm']['ami_package'])} |",
              f"| Leiden | {d['leiden']['files']} | {_f(d['leiden']['ami_top_directory'])} | "
              f"{_f(d['leiden']['ami_package'])} |", ""]

    s = result["structure"]
    lines += ["## Structural quality on the shared atom graph", "",
              f"Shared atoms: {s['shared_atoms']} ({s['shared_edges']} edges). Left out: "
              f"{s['atoms_in_multi_topic_files']} atoms in files of several LLM topics, "
              f"{s['atoms_without_llm_topic']} atoms without an LLM topic, "
              f"{s['atoms_unassigned_by_leiden']} atoms Leiden left unassigned. "
              f"Leiden's modularity on its full graph: {_f(s.get('leiden_full_graph_modularity'))}.", "",
              "| Method | Topics | Modularity | Weight inside topics | Mean conductance |", "|---|---|---|---|---|"]
    for method in ("llm", "leiden"):
        m = s[method]
        lines.append(f"| {method.upper() if method == 'llm' else 'Leiden'} | {m['topics']} | {_f(m['modularity'])} | "
                     f"{_f(m['intra_weight_share'])} | {_f(m['mean_conductance'])} |")
    lines.append("")

    st = result["stability"]
    llm_st = st.get("llm") or {}
    lines += ["## Stability", "", "| Method | Measure | Value |", "|---|---|---|",
              f"| Leiden | Mean pairwise ARI over seeds | {_f(st['leiden']['seed_ari'])} |",
              f"| Leiden | Rerun gives the same membership hash | {_f(st['leiden']['rerun_hash_equal'])} |",
              f"| LLM | ARI between two runs ({llm_st.get('files', 0)} files) | {_f(llm_st.get('ari'))} |", ""]

    lines += ["## Cost", "", "| Method | Phase | Calls | Input tokens | Output tokens | Seconds |",
              "|---|---|---|---|---|---|"]
    for method in ("llm", "leiden"):
        phases = result["cost"].get(method)
        if not phases:
            lines.append(f"| {method} | – | – | – | – | – |")
            continue
        for phase, v in phases.items():
            lines.append(f"| {method} | {phase} | {_f(v.get('calls'))} | {_f(v.get('input_tokens'))} | "
                         f"{_f(v.get('output_tokens'))} | {_f(v.get('seconds'), 1)} |")
    lines.append("")

    lines += ["## Grounding", "",
              "Code identifiers in backticks on the topic pages that exist in the graph (types, functions, "
              "methods, packages, files).", "",
              "| Method | Pages | Identifiers | Found | Rate | Most frequent misses |", "|---|---|---|---|---|---|"]
    for method in ("llm", "leiden"):
        g = result["grounding"].get(method)
        if not g:
            lines.append(f"| {method} | – | – | – | – | – |")
            continue
        misses = ", ".join(f"`{m}`" for m in g["top_missing"][:5])
        lines.append(f"| {method} | {g['pages']} | {g['checked']} | {g['found']} | {_f(g['rate'])} | {misses} |")
    lines.append("")

    ab = result.get("ablation")
    if ab:
        lines += ["## Ablation: Leiden without implements edges", ""]
        if ab.get("profile") is None:
            lines += [ab["reason"], ""]
        else:
            lines += ["| Profile | Resolution | Topics | Modularity | Atoms compared | AMI vs reported | ARI vs reported |",
                      "|---|---|---|---|---|---|---|",
                      f"| {ab['profile']} | {_f(ab['resolution'], 2)} | {ab['topics']} | {_f(ab['modularity'])} | "
                      f"{ab['atoms_compared']} | {_f(ab['ami_vs_reported'])} | {_f(ab['ari_vs_reported'])} |", ""]

    feats = result.get("features")
    if feats:
        lines += ["## Feature recovery", "",
                  f"Per reviewed feature F: best coverage max |F ∩ G| / |F|, dispersion (topics needed to cover "
                  f"{int(FEATURE_COVER * 100)} % of F, greedy; – when not reachable) and the irrelevant share of "
                  "the best G. Top level / leaves.", "",
                  "| Feature | Files | LLM coverage | LLM dispersion | LLM irrelevant | Leiden coverage | "
                  "Leiden dispersion | Leiden irrelevant |", "|---|---|---|---|---|---|---|---|"]
        for r in feats["features"]:
            cells = []
            for method in ("llm", "leiden"):
                for key in ("coverage", "dispersion", "irrelevant"):
                    cells.append(" / ".join(_f(r[f"{method}_{lv}"][key]) for lv in LEVELS))
            lines.append(f"| {_cell(r['name'])} | {r['files']} | " + " | ".join(cells) + " |")
        summ = feats["summary"]
        lines += ["", "| Mean | LLM top | LLM leaf | Leiden top | Leiden leaf |", "|---|---|---|---|---|"]
        for key in ("mean_coverage", "mean_dispersion", "uncoverable", "mean_irrelevant"):
            lines.append(f"| {key} | " + " | ".join(_f(summ[f'{m}_{lv}'][key]) for m in ("llm", "leiden")
                                                   for lv in LEVELS) + " |")
        lines.append("")
    return "\n".join(lines)
