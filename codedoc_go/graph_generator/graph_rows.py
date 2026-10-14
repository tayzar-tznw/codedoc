"""Pure graph builders (spec §6, C6): the node index, node rows and edge rows.

Phase 8 writes build_node_rows(data) and Phase 9 derive_edge_rows(data,
node_index(data)), to Spanner or to graph_rows/<Table>.jsonl; pipeline.py
delegates its builder functions here. Nothing touches GCP. Every row is a
dict with exactly the columns write_columns(table) lists, in that order, so a
Spanner insert and a jsonl line carry the same values; flag columns hold
Python bools (Spanner BOOL).

Inputs: data.file_hashes (the scan), data.extracted_entities (entities.json
records, C3) and data.resolutions (resolutions.json, C5). An entity or
resolution record whose sha256 is not the scan's is ignored, so positions of
an old extraction never meet fresh answers (spec §2.6).

Zero wrong edges (spec §6.3): MethodCalls only from `resolved` records whose
target is a Methods node of this repo; PossiblyCalls only for the in-graph
candidates of resolved, external and ambiguous records, and none for a site
above POSSIBLY_CALLS_MAX_CANDIDATES; PackageImports, ClassEmbeds and
ClassImplements only from `resolved` records whose ends are nodes of this
repo. External, builtin, dynamic, unresolved and peer records (left to
crossref) are counted in EdgeRows.stats, never edged.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from typing import Any

from . import config
from .pipeline import PipelineData, _claim_id, _edge_id, _make_id
from .setup_spanner_graph import write_columns

NODE_ROW_TABLES = ("Files", "Packages", "Classes", "Methods", "Directories")
EDGE_ROW_TABLES = ("PackageImports", "FileDependsOn", "ClassEmbeds", "ClassImplements",
                   "MethodCalls", "PossiblyCalls", "FileDefinesClass", "ClassDefinesMethod",
                   "DirContainsFile", "PackageContainsFile")

PACKAGE = "<package>"
_POSSIBLY_REASONS = ("implementation", "variant")
# Records with these statuses may carry in-repo candidates for PossiblyCalls:
# interface sites (resolved or external, spec §5.4) and ambiguous records.
_CANDIDATE_STATUSES = frozenset({"resolved", "external", "ambiguous"})
# A Packages row takes the origin of its most "production" file.
_ORIGIN_RANK = {"package": 0, "command": 1, "test": 2, "xtest": 3}


_COLUMNS = {t: write_columns(t) for t in NODE_ROW_TABLES + EDGE_ROW_TABLES}


def _row(table: str, values: dict[str, Any]) -> dict[str, Any]:
    """A row with exactly write_columns(table), in order; a missing column raises."""
    return {c: values[c] for c in _COLUMNS[table]}


def _dir(rel: str) -> str:
    return rel.rpartition("/")[0]


def _fq(package: str, qualname: str) -> str:
    """fqcn / fqmn: import path + "." + qualname; the bare qualname without one."""
    return f"{package}.{qualname}" if package else qualname


def _doc(entry: dict) -> str:
    return (entry.get("doc") or "").strip()


def _pseudo_type(ent: dict) -> dict:
    """The file's <package> pseudo-type (C3); a stand-in when the record lacks it."""
    for t in ent.get("types") or []:
        if t.get("qualname") == PACKAGE:
            return t
    return {"name": PACKAGE, "qualname": PACKAGE, "kind": "package", "type_params": "",
            "embeds": [], "methods": [], "start_line": 0, "end_line": 0, "doc": ""}


# ===================================================================
# Node index
# ===================================================================


def node_index(data: PipelineData) -> dict[str, Any]:
    """Every node ID of this repo and the lookups the builders need (spec §6.4).

    Keys: "files" relpath → file_id; "dirs" dir relpath ("" = root) → dir_id;
    "packages" (dir, clause) → package_id; "classes" and "methods"
    (relpath, qualname) → ID; "method_class" method_id → class_id (the owner
    rule); "file_package" relpath → (dir, clause); "import_clauses" the
    clauses per (dir, import path); "records" relpath → the fresh entity
    record; "stats" counts of skipped records. Files come from the scan,
    declarations from fresh entity records of scanned files.
    """
    repo = data.repo
    owners: dict[str, tuple] = {}
    idx: dict[str, Any] = {
        "files": {}, "dirs": {}, "packages": {}, "classes": {}, "methods": {},
        "method_class": {}, "file_package": {}, "records": {}, "class_entries": [],
        "method_entries": [], "package_files": {}, "stats": Counter(),
    }
    dirs = {""}
    for rel in sorted(data.file_hashes):
        file_id = _make_id("file", repo, rel, "")
        _claim_id(owners, file_id, ("file", rel))
        idx["files"][rel] = file_id
        d = _dir(rel)
        while d and d not in dirs:
            dirs.add(d)
            d = _dir(d)
    for d in sorted(dirs):
        dir_id = _make_id("dir", repo, d, "")
        _claim_id(owners, dir_id, ("dir", d))
        idx["dirs"][d] = dir_id

    for rel, ent in sorted((data.extracted_entities or {}).items()):
        if rel not in idx["files"]:
            idx["stats"]["entities_not_scanned"] += 1
        elif not isinstance(ent, dict) or ent.get("sha256") != data.file_hashes[rel]:
            idx["stats"]["entities_stale"] += 1
        else:
            idx["records"][rel] = ent

    package_files: dict[tuple[str, str], list[str]] = defaultdict(list)
    for rel, ent in idx["records"].items():
        if ent.get("package_name"):
            key = (_dir(rel), ent["package_name"])
            package_files[key].append(rel)
            idx["file_package"][rel] = key
        _index_classes(repo, rel, ent, owners, idx)
    for key in sorted(package_files):
        package_id = _make_id("package", repo, key[0], key[1])
        _claim_id(owners, package_id, ("package", *key))
        idx["packages"][key] = package_id
    idx["package_files"] = dict(package_files)
    idx["import_clauses"] = _import_clauses(idx["records"])
    for rel, ent in idx["records"].items():
        _index_methods(repo, rel, ent, owners, idx)
    return idx


def _import_clauses(records: dict[str, dict]) -> dict[str, dict[tuple[str, str], set[str]]]:
    """The package clauses of each (dir, import path): "compiled" by the files
    the primary build compiles, "every" by all files. The go command allows
    one clause per compiled package, so an import binds a certain package only
    when the set that applies to it has one member (see _import_edges)."""
    out: dict[str, dict[tuple[str, str], set[str]]] = {"compiled": defaultdict(set),
                                                        "every": defaultdict(set)}
    for rel, ent in records.items():
        if ent.get("package_name") and ent.get("package"):
            key = (_dir(rel), ent["package"])
            out["every"][key].add(ent["package_name"])
            if ent.get("build_included"):
                out["compiled"][key].add(ent["package_name"])
    return {name: dict(sets) for name, sets in out.items()}


def _index_classes(repo: str, rel: str, ent: dict, owners: dict, idx: dict) -> None:
    """The file's types and its <package> pseudo-type (spec §4.1)."""
    types = [t for t in ent.get("types") or [] if t.get("qualname")]
    if not any(t["qualname"] == PACKAGE for t in types):
        types.insert(0, _pseudo_type(ent))
    for t in types:
        q = t["qualname"]
        class_id = _make_id("class", repo, rel, q)
        if _claim_id(owners, class_id, ("class", rel, q)):
            idx["classes"][(rel, q)] = class_id
            idx["class_entries"].append((rel, t, class_id))


def _index_methods(repo: str, rel: str, ent: dict, owners: dict, idx: dict) -> None:
    """Interface methods (owned by their interface) and the file's functions,
    methods, init#k, <init> and closures (owned by the owner rule)."""
    entries: list[tuple[dict, str, str]] = []   # (entry, member_kind, class_id)
    for t in ent.get("types") or []:
        iface = idx["classes"].get((rel, t.get("qualname") or ""))
        if t.get("kind") == "interface" and iface:
            entries += [(m, "interface_method", iface) for m in t.get("methods") or []]
    funcs = {f.get("qualname"): f for f in ent.get("funcs") or []}
    for f in ent.get("funcs") or []:
        entries.append((f, f.get("member_kind") or "", _owner_class(rel, f, funcs, idx)))
    for entry, kind, class_id in entries:
        q = entry.get("qualname")
        if not q:
            continue
        method_id = _make_id("method", repo, rel, q)
        if _claim_id(owners, method_id, ("method", rel, q)):
            idx["methods"][(rel, q)] = method_id
            idx["method_class"][method_id] = class_id
            idx["method_entries"].append((rel, entry, kind, method_id, class_id))


def _owner_class(rel: str, f: dict, funcs: dict[str, dict], idx: dict) -> str:
    """class_id of a func entry (spec §6.1, R18).

    A method belongs to its receiver's type in receiver.owner_relpath, also
    when it is declared in another file. A closure belongs to the owner of its
    outermost enclosing declaration. Functions, init#k, <init>, and methods
    whose receiver type was not found (receiver_unresolved, or a type with no
    node) belong to the file's <package>.
    """
    seen: set[str] = set()
    while f.get("member_kind") == "closure":
        parent = funcs.get(f.get("parent") or "")
        if parent is None or parent.get("qualname") in seen:
            break
        seen.add(parent.get("qualname"))
        f = parent
    receiver = f.get("receiver") or {}
    if f.get("member_kind") == "method" and not receiver.get("receiver_unresolved"):
        owner = idx["classes"].get((receiver.get("owner_relpath") or "",
                                    receiver.get("base") or ""))
        if owner and receiver.get("base") != PACKAGE:
            return owner
    return idx["classes"][(rel, PACKAGE)]


# ===================================================================
# Node rows (Phase 8)
# ===================================================================


def build_node_rows(data: PipelineData) -> dict[str, list[dict]]:
    """Node rows of spec §6.1 as table → [column dict]: Files, Packages,
    Classes, Methods and Directories. Summaries start as the Go doc comment
    (spec §2.1); the wiki track replaces them. Topics are written by the wiki
    track (Phase 10)."""
    idx = node_index(data)
    repo = data.repo
    rows: dict[str, list[dict]] = {t: [] for t in NODE_ROW_TABLES}

    for rel, file_id in idx["files"].items():
        ent = idx["records"].get(rel, {})
        name = rel.rpartition("/")[2]
        rows["Files"].append(_row("Files", {
            "file_id": file_id, "file_name": name, "extension": os.path.splitext(name)[1],
            "directory": _dir(rel), "path": rel, "origin": ent.get("origin") or "",
            "generated": bool(ent.get("generated")), "package": ent.get("package") or "",
            "package_name": ent.get("package_name") or "", "module": ent.get("module") or "",
            "build_constraint": ent.get("build_constraint") or "",
            "build_included": bool(ent.get("build_included")),
            "summary": _doc(_pseudo_type(ent)) if ent else "", "repo": repo}))

    for key, package_id in idx["packages"].items():
        files = idx["package_files"][key]
        records = [idx["records"][rel] for rel in files]
        origin = min((r.get("origin") or "" for r in records),
                     key=lambda o: _ORIGIN_RANK.get(o, len(_ORIGIN_RANK)))
        # The package comment: from the non-test files, else from any (go/doc
        # concatenates every file's package comment)
        docs = [_doc(_pseudo_type(r)) for r in records
                if (r.get("origin") or "") in ("package", "command")]
        docs = [d for d in docs if d] or [d for d in (_doc(_pseudo_type(r)) for r in records) if d]
        rows["Packages"].append(_row("Packages", {
            "package_id": package_id,
            "import_path": next((r["package"] for r in records if r.get("package")), ""),
            "name": key[1], "directory": key[0],
            "module": next((r["module"] for r in records if r.get("module")), ""),
            "origin": origin, "file_count": len(files), "summary": "\n\n".join(docs),
            "repo": repo}))

    for rel, t, class_id in idx["class_entries"]:
        ent = idx["records"][rel]
        package = ent.get("package") or ""
        embeds = [("*" if e.get("pointer") else "") + (e.get("text") or "")
                  for e in t.get("embeds") or []]
        rows["Classes"].append(_row("Classes", {
            "class_id": class_id, "name": t.get("name") or t["qualname"],
            "qualname": t["qualname"], "package": package, "fqcn": _fq(package, t["qualname"]),
            "file_id": idx["files"][rel], "kind": t.get("kind") or "",
            "type_params": t.get("type_params") or "",
            "embeds": json.dumps(embeds, ensure_ascii=False),
            "start_line": t.get("start_line") or 0, "end_line": t.get("end_line") or 0,
            "origin": ent.get("origin") or "", "module": ent.get("module") or "",
            "summary": _doc(t), "repo": repo}))

    for rel, entry, kind, method_id, class_id in idx["method_entries"]:
        ent = idx["records"][rel]
        package = ent.get("package") or ""
        interface_method = kind == "interface_method"
        rows["Methods"].append(_row("Methods", {
            "method_id": method_id, "name": entry.get("name") or entry["qualname"],
            "class_id": class_id, "file_id": idx["files"][rel], "qualname": entry["qualname"],
            "fqmn": _fq(package, entry["qualname"]), "signature": entry.get("signature") or "",
            "member_kind": kind,
            "receiver": "" if interface_method else (entry.get("receiver") or {}).get("text") or "",
            "type_params": entry.get("type_params") or "",
            "has_body": False if interface_method else bool(entry.get("has_body")),
            "start_line": entry.get("start_line") or 0, "end_line": entry.get("end_line") or 0,
            "origin": ent.get("origin") or "", "module": ent.get("module") or "",
            "entry_point": _entry_point(data, rel, ent, entry, kind),
            "summary": _doc(entry), "repo": repo}))

    for d, dir_id in idx["dirs"].items():
        name = d.rpartition("/")[2] if d else os.path.basename(data.target_dir.rstrip("/\\"))
        rows["Directories"].append(_row("Directories", {
            "dir_id": dir_id, "name": name, "summary": "", "repo": repo}))
    return rows


def cached_summaries(data: PipelineData, rows: dict[str, list[dict]]
                     ) -> dict[str, list[tuple[str, str]]]:
    """(node ID, summary) per table, from the wiki's summary cache, for the
    node rows *rows* whose cached summaries still describe the sources.

    Phase 8 writes the doc comments into the node rows (graph_rows/*.jsonl,
    which Phase 2 reads as the doc comments) and then re-applies these to
    Spanner, so re-running --graph after --wiki keeps the wiki's summaries
    there (spec §7.4). The values are those Phase 10 writes (spec §7.5): the
    file and node summaries of a summaries/nodes/<relpath>.json record whose
    sha256 is the file's (a stale context hash is acceptable for display;
    --wiki refreshes it), and the package and directory summaries of
    summaries/packages.json and dirs.json whose children hash as they did.
    Read-only, no LLM call; directories as node_summaries.summary_dirs.
    """
    from . import node_summaries
    from .pipeline import _SUMMARY_LIMIT, _children_hash, _read_json, _summary_cache
    dirs = node_summaries.summary_dirs(data)
    files: dict[str, str] = {}
    nodes: dict[str, dict] = {}
    for rel, sha in sorted(data.file_hashes.items()):
        for directory in dirs:
            record = _read_json(node_summaries._record_path(directory, rel))
            if (isinstance(record, dict) and record.get("sha256") == sha
                    and isinstance(record.get("file"), str) and record["file"]
                    and isinstance(record.get("nodes"), dict)):
                files[rel], nodes[rel] = record["file"], record["nodes"]
                break

    out: dict[str, list[tuple[str, str]]] = {t: [] for t in NODE_ROW_TABLES}

    def add(table: str, node_id: str | None, summary: Any) -> None:
        if node_id and isinstance(summary, str) and summary:
            out[table].append((node_id, summary[:_SUMMARY_LIMIT]))

    path_of = {r["file_id"]: r["path"] for r in rows.get("Files", [])}
    for r in rows.get("Files", []):
        add("Files", r["file_id"], files.get(r["path"]))
    for r in rows.get("Classes", []):
        rel = path_of.get(r["file_id"], "")
        add("Classes", r["class_id"], files.get(rel) if r["kind"] == "package"
            else nodes.get(rel, {}).get(r["qualname"]))
    for r in rows.get("Methods", []):
        rel = path_of.get(r["file_id"], "")
        add("Methods", r["method_id"], nodes.get(rel, {}).get(r["qualname"]))

    def reused(cache: dict, key: str, children: list[tuple[str, str]]) -> str | None:
        hit = cache.get(key)
        if children and isinstance(hit, dict) and hit.get("hash") == _children_hash(children):
            return hit.get("summary")
        return None

    package_files: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in rows.get("Files", []):
        if r.get("package_name"):
            package_files[(r["directory"], r["package_name"])].append(r["path"])
    packages = _summary_cache(data, "packages")
    for r in rows.get("Packages", []):
        children = [(rel, files[rel]) for rel in package_files[(r["directory"], r["name"])]
                    if files.get(rel)]
        add("Packages", r["package_id"],
            reused(packages, f"{r['directory']}|{r['name']}", children))

    # Directories bottom-up, with the children Phase 3 hashes: the direct
    # files by name and the subdirectories by "name/", each with a summary
    dir_ids = {r["dir_id"] for r in rows.get("Directories", [])}
    all_dirs, subdirs, direct = {""}, defaultdict(list), defaultdict(list)
    for rel in data.file_hashes:
        direct[_dir(rel)].append(rel)
        d = _dir(rel)
        while d and d not in all_dirs:
            all_dirs.add(d)
            d = _dir(d)
    for d in all_dirs - {""}:
        subdirs[_dir(d)].append(d)
    dir_cache, dir_summary = _summary_cache(data, "dirs"), {}
    for d in sorted(all_dirs, key=lambda d: (-(d.count("/") + bool(d)), d)):
        children = [(rel.rpartition("/")[2], files[rel]) for rel in direct[d] if files.get(rel)]
        children += [(sub.rpartition("/")[2] + "/", dir_summary[sub])
                     for sub in subdirs[d] if dir_summary.get(sub)]
        summary = reused(dir_cache, d, children)
        if summary:
            dir_summary[d] = summary
            dir_id = _make_id("dir", data.repo, d, "")
            add("Directories", dir_id if dir_id in dir_ids else None, summary)
    return out


def _entry_point(data: PipelineData, rel: str, ent: dict, entry: dict, kind: str) -> str:
    """The command name (the directory name) of a `func main` (spec §6.1)."""
    if not (kind == "func" and entry.get("qualname") == "main"
            and ent.get("package_name") == "main" and ent.get("origin") == "command"):
        return ""
    d = _dir(rel)
    return d.rpartition("/")[2] if d else os.path.basename(data.target_dir.rstrip("/\\"))


# ===================================================================
# Edge rows (Phase 9)
# ===================================================================


class EdgeRows(dict):
    """derive_edge_rows' result: table → rows (C6). `stats` counts the
    records and sites that wrote no row, by kind and reason (for example
    calls_external, calls_peer, possibly_capped, files_stale);
    `external_imports` counts the external import records per module path
    ("std", "C", or the module), spec §6.3."""

    def __init__(self, rows: dict[str, list[dict]], stats: Counter, external_imports: Counter):
        super().__init__(rows)
        self.stats = stats
        self.external_imports = external_imports


class _EdgeSink:
    """Edge rows under construction: one row per (table, source, target), the
    first one wins (spec §6.3)."""

    def __init__(self, stats: Counter):
        self.rows: dict[str, list[dict]] = {t: [] for t in EDGE_ROW_TABLES}
        self.stats = stats
        self.external_imports: Counter = Counter()
        self._seen: set[str] = set()

    def emit(self, table: str, src: str, dst: str, values: dict[str, Any]) -> None:
        edge_id = _edge_id(table, src, dst)
        if edge_id not in self._seen:
            self._seen.add(edge_id)
            self.rows[table].append(_row(table, {"edge_id": edge_id, **values}))


def derive_edge_rows(data: PipelineData, idx: dict[str, Any]) -> EdgeRows:
    """Edge rows of spec §6.3 from resolutions.json, as table → [column dict].

    MethodCalls, PossiblyCalls, PackageImports, ClassEmbeds, ClassImplements
    and FileDependsOn come from the records of each scanned file whose
    resolutions sha256 is the scan's; FileDefinesClass, ClassDefinesMethod,
    DirContainsFile and PackageContainsFile from the node index. A file whose
    resolutions sha256 differs (it changed while the helper loaded it) gives
    no records, and no edge ends in it: positions mapped into it are
    untrusted (spec §2.6, G8). Rows are deduplicated per (table, source,
    target); records are taken in source order, so the earliest site keeps
    its line and kind.
    """
    sink = _EdgeSink(Counter(idx.get("stats") or {}))
    _structural_edges(idx, sink)
    files = (data.resolutions or {}).get("files") or {}
    stale = {rel for rel, frec in files.items()
             if (frec or {}).get("sha256") != data.file_hashes.get(rel)}
    if stale:
        idx = {**idx,
               "methods": {k: v for k, v in idx["methods"].items() if k[0] not in stale},
               "classes": {k: v for k, v in idx["classes"].items() if k[0] not in stale}}
    for rel in sorted(files):
        frec = files[rel] or {}
        if rel not in idx["files"]:
            sink.stats["files_not_scanned"] += 1
            continue
        if rel in stale:
            sink.stats["files_stale"] += 1
            continue
        reached = _call_edges(rel, frec.get("calls") or [], idx, sink)
        reached += _embed_edges(frec.get("embeds") or [], idx, sink)
        _implements_edges(frec.get("implements") or [], idx, sink)
        _import_edges(rel, idx["records"].get(rel) or {}, frec.get("imports") or [], idx, sink)
        _depends_on(reached, idx, sink)
    return EdgeRows(sink.rows, sink.stats, sink.external_imports)


def _structural_edges(idx: dict[str, Any], sink: _EdgeSink) -> None:
    """FileDefinesClass, ClassDefinesMethod, DirContainsFile, PackageContainsFile."""
    for (rel, _q), class_id in idx["classes"].items():
        file_id = idx["files"][rel]
        sink.emit("FileDefinesClass", file_id, class_id, {"file_id": file_id, "class_id": class_id})
    for method_id, class_id in idx["method_class"].items():
        sink.emit("ClassDefinesMethod", class_id, method_id,
                  {"class_id": class_id, "method_id": method_id})
    for rel, file_id in idx["files"].items():
        dir_id = idx["dirs"][_dir(rel)]
        sink.emit("DirContainsFile", dir_id, file_id, {"dir_id": dir_id, "file_id": file_id})
    for rel, key in idx["file_package"].items():
        package_id, file_id = idx["packages"][key], idx["files"][rel]
        sink.emit("PackageContainsFile", package_id, file_id,
                  {"package_id": package_id, "file_id": file_id})


def _dicts(records: list) -> list[dict]:
    return [r for r in records if isinstance(r, dict)]


def _site_position(record: dict) -> tuple[int, int]:
    site = record.get("site") or {}
    return site.get("line") or 0, site.get("col") or 0


def _is_peer(record: dict, target: Any) -> bool:
    return bool((isinstance(target, dict) and target.get("repo"))
                or (record.get("external") or {}).get("peer"))


def _not_edged(sink: _EdgeSink, kind: str, record: dict, target: Any) -> None:
    status = record.get("status") or "none"
    sink.stats[f"{kind}_peer" if _is_peer(record, target) else f"{kind}_{status}"] += 1


def _simple_name(target: dict) -> str:
    """`M` of `T.M`, `func1` of `F.func1`: the callee name of a site without a
    token name (paren and other shapes, C3)."""
    return (target.get("qualname") or "").rsplit(".", 1)[-1]


def _node(idx: dict[str, Any], table: str, target: Any, kind: str) -> str | None:
    """The node of an in-repo C5 target of this kind ("method" or "class"), else None."""
    if not isinstance(target, dict) or target.get("repo") or target.get("kind") != kind:
        return None
    return idx[table].get((target.get("relpath") or "", target.get("qualname") or ""))


def _call_edges(rel: str, records: list[dict], idx: dict[str, Any],
                sink: _EdgeSink) -> list[tuple[str, str, str]]:
    """MethodCalls and PossiblyCalls from one file's `calls` records; returns
    (source relpath, target relpath, via) for each MethodCalls site."""
    reached: list[tuple[str, str, str]] = []
    for r in sorted(_dicts(records), key=_site_position):  # source order: first site wins
        site, status, target = r.get("site") or {}, r.get("status"), r.get("target")
        caller = idx["methods"].get((rel, site.get("caller") or ""))
        name = site.get("name") or ""
        if status != "resolved" or _is_peer(r, target):
            _not_edged(sink, "calls", r, target)
        else:
            callee = _node(idx, "methods", target, "method")
            if caller and callee:
                via = r.get("via") or "types"
                sink.emit("MethodCalls", caller, callee, {
                    "caller_method": caller, "callee_method": callee,
                    "callee_name": name or _simple_name(target),
                    "resolution": via, "site_kind": site.get("kind") or "call",
                    "call_line": site.get("line") or 0})
                reached.append((rel, target["relpath"], via))
                sink.stats["calls_resolved"] += 1
            else:
                sink.stats["calls_unmapped"] += 1
        if status in _CANDIDATE_STATUSES and r.get("candidates"):
            reason = r.get("reason") if status == "ambiguous" else "implementation"
            _possibly(r, caller, name, reason, idx, sink)
    return reached


def _possibly(record: dict, caller: str | None, name: str, reason: str | None,
              idx: dict[str, Any], sink: _EdgeSink) -> None:
    """PossiblyCalls rows for one site's in-graph candidates; none at all above
    the cap (spec §5.4, §6.3)."""
    callees: dict[str, str] = {}   # method_id → callee name, in candidate order
    for c in record["candidates"]:
        method_id = _node(idx, "methods", c, "method")
        if method_id:
            callees.setdefault(method_id, name or _simple_name(c))
    if reason not in _POSSIBLY_REASONS:
        sink.stats["possibly_bad_reason"] += 1
    elif not caller:
        sink.stats["possibly_unmapped_caller"] += 1
    elif not callees:
        sink.stats["possibly_no_candidates"] += 1
    elif len(callees) > config.POSSIBLY_CALLS_MAX_CANDIDATES:
        sink.stats["possibly_capped"] += 1
    else:
        for callee, callee_name in callees.items():
            sink.emit("PossiblyCalls", caller, callee, {
                "caller_method": caller, "callee_method": callee, "callee_name": callee_name,
                "reason": reason, "candidate_count": len(callees),
                "open_world": bool(record.get("open_world"))})
        sink.stats[f"possibly_{reason}"] += 1


def _embed_edges(records: list[dict], idx: dict[str, Any],
                 sink: _EdgeSink) -> list[tuple[str, str, str]]:
    """ClassEmbeds from one file's `embeds` records (both ends in-repo types);
    returns (child relpath, embedded relpath, "types") per row."""
    reached: list[tuple[str, str, str]] = []
    for r in _dicts(records):
        child_type, embedded = r.get("type") or {}, r.get("embedded")
        if r.get("status") != "resolved" or _is_peer(r, embedded):
            _not_edged(sink, "embeds", r, embedded)
            continue
        child = idx["classes"].get((child_type.get("relpath") or "",
                                    child_type.get("qualname") or ""))
        parent = _node(idx, "classes", embedded, "class")
        if not child or not parent:
            sink.stats["embeds_unmapped"] += 1
            continue
        sink.emit("ClassEmbeds", child, parent, {
            "child_class": child, "embedded_class": parent, "pointer": bool(r.get("pointer")),
            "resolution": "types"})
        reached.append((child_type["relpath"], embedded["relpath"], "types"))
    return reached


def _implements_edges(records: list[dict], idx: dict[str, Any], sink: _EdgeSink) -> None:
    """ClassImplements from one file's `implements` records (an in-repo
    concrete type and an in-repo interface)."""
    for r in _dicts(records):
        concrete, iface_target = r.get("type") or {}, r.get("iface")
        if r.get("status") != "resolved" or _is_peer(r, iface_target):
            _not_edged(sink, "implements", r, iface_target)
            continue
        cls = idx["classes"].get((concrete.get("relpath") or "", concrete.get("qualname") or ""))
        iface = _node(idx, "classes", iface_target, "class")
        if not cls or not iface or cls == iface:
            sink.stats["implements_unmapped"] += 1
            continue
        sink.emit("ClassImplements", cls, iface, {
            "class_id": cls, "interface_id": iface,
            "pointer_receiver": bool(r.get("pointer_receiver")), "resolution": "types"})


def _import_edges(rel: str, ent: dict, records: list[dict], idx: dict[str, Any],
                  sink: _EdgeSink) -> None:
    """PackageImports from one file's `imports` records whose target is an
    in-repo package (dir, clause), when that clause is the only one the import
    can bind. A file the primary build compiles imports the primary build's
    package: the clause of its compiled files (of all files when it has none,
    a package only an extra build compiles), so a `//go:build ignore`
    generator with `package main` beside a library never counts. A file only
    an extra build compiles may see other files of the target directory, so
    there every file must agree. alias and kind come from the entity import
    at the same position (the import path literal, C3)."""
    source = idx["files"][rel]
    declared = {(i.get("line"), i.get("col")): i for i in ent.get("imports") or []}
    compiled, every = idx["import_clauses"]["compiled"], idx["import_clauses"]["every"]
    for r in sorted(_dicts(records), key=lambda r: (r.get("line") or 0, r.get("col") or 0)):
        target = r.get("target")
        if r.get("status") != "resolved" or _is_peer(r, target):
            _not_edged(sink, "imports", r, target)
            if r.get("status") == "external" and not _is_peer(r, target):
                sink.external_imports[(r.get("external") or {}).get("module") or ""] += 1
            continue
        target = target or {}
        package = idx["packages"].get((target.get("dir"), target.get("clause")))
        key = (target.get("dir"), r.get("import_path"))
        importable = ((compiled.get(key) or every.get(key)) if ent.get("build_included")
                      else every.get(key))
        entry = declared.get((r.get("line"), r.get("col")))
        if (not package or importable != {target.get("clause")} or entry is None
                or entry.get("path") != r.get("import_path")):
            sink.stats["imports_unmapped"] += 1
            continue
        sink.emit("PackageImports", source, package, {
            "source_file": source, "target_package": package,
            "import_path": r["import_path"], "alias": entry.get("alias") or "",
            "kind": entry.get("kind") or "normal"})


def _depends_on(reached: list[tuple[str, str, str]], idx: dict[str, Any],
                sink: _EdgeSink) -> None:
    """FileDependsOn: the files MethodCalls and ClassEmbeds rows reach, not
    the file itself; resolution is the via of the first site that reaches it."""
    for src_rel, dst_rel, via in reached:
        src, dst = idx["files"].get(src_rel), idx["files"].get(dst_rel)
        if src and dst and src != dst:
            sink.emit("FileDependsOn", src, dst,
                      {"source_file": src, "target_file": dst, "resolution": via})
