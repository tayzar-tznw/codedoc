"""Cross-repo edges for codedoc_go, after every repo is ingested (spec §8.3).

Peers are the input to Phase 1.6 (spec §8.2): the CLI discovers them with
go_units.discover_peer, and resolution classifies a target under a peer's
module directory as `external` with {repo, relpath, qualname} (§5.3 row 3).
This module turns those peer targets into edges:

| Peer target in resolutions.json      | Edge                         |
|--------------------------------------|------------------------------|
| call record, method target           | CrossRepoCalls               |
| import record, package target        | CrossRepoImports             |
| embeds record, type target           | CrossRepoRef kind `embeds`   |
| implements record, interface target  | CrossRepoRef kind `implements` |

Node IDs are recomputed with `pipeline._make_id`, exactly as Phase 8 wrote
them; nothing is looked up by name. A source or target node absent from the
repos' entities is dropped (`node missing`), as is a peer target of a repo
that is not ingested (`repo not ingested`) or that names no repo (`no
owner`). A file whose resolutions.json sha256 differs from its entities.json
sha256 (it changed while the helper loaded it, "changed-during-load"
included) gives no records, and no edge ends in such a file of a peer
(`stale`, amendment R46, as Phase 9 does in one repo). Module-cache (and
vendored) targets whose module path belongs to an ingested repo are never
matched, because that copy is a published version, not the ingested source:
they are listed with the hint `--peer`, or dropped as `two owners` when two
ingested repos provide the module (a fork).

Record shapes read (resolutions.json `files[relpath]`):
- `calls`: C5 records; `site.caller` is the caller qualname in the file.
- `imports`: {line, col, import_path, status, target, external}; a package
  target is {"unit", "package", "dir"[, "clause"][, "repo"]}. Without a clause
  the target is the directory's package: the single clause of its
  build-included non-xtest files.
- `embeds` / `implements`: {type: {relpath, qualname}, embedded | iface: a C5
  target or null, status, external}.
"""

from __future__ import annotations

import glob
import json
import os
import posixpath

from . import config, pipeline
from .go_model import UNITS_VERSION

DROP_REASONS: tuple[str, ...] = ("no owner", "two owners", "node missing", "repo not ingested", "stale")
ENTITIES_VERSION = 1                     # master plan C3
RESOLUTIONS_VERSION = 1                  # master plan C5
TABLES: tuple[str, ...] = ("CrossRepoCalls", "CrossRepoImports", "CrossRepoRef")
ROWS_RECORD = "crossref_rows.json"       # R48: the keys of the last write and its Spanner target
ROWS_RECORD_VERSION = 1
_RELATIONS = (("embeds", "embedded", "embeds"), ("implements", "iface", "implements"))


# ═══════════════════════════════════════════════════════════════════
# Ingested repos
# ═══════════════════════════════════════════════════════════════════


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def discover_repos(output_dir: str, printer=print) -> list[dict]:
    """Every repo under <output_dir>/repos/*/ whose graph_meta.json,
    entities.json, resolutions.json and units.json have the current versions
    and whose graph was written under the current ID_SCHEME and ID_PREFIX
    (otherwise the recomputed IDs would not match its nodes).

    Returns {"repo", "root", "entities", "resolutions", "modules"} per repo,
    sorted by repo name.
    """
    repos: list[dict] = []
    for meta_path in sorted(glob.glob(os.path.join(output_dir, "repos", "*", "graph_meta.json"))):
        rdir = os.path.dirname(meta_path)
        name = os.path.basename(rdir)
        try:
            meta = _read_json(meta_path)
            entities = _read_json(os.path.join(rdir, "entities.json"))
            resolutions = _read_json(os.path.join(rdir, "resolutions.json"))
            units = _read_json(os.path.join(rdir, "units.json"))
        except (OSError, ValueError) as exc:
            printer(f"  [crossref] skipping {name}: {exc}")
            continue
        if (meta.get("id_scheme") != pipeline.ID_SCHEME
                or entities.get("version") != ENTITIES_VERSION
                or resolutions.get("version") != RESOLUTIONS_VERSION
                or units.get("version") != UNITS_VERSION):
            printer(f"  [crossref] skipping {name} (old ID scheme or artifact version; re-ingest)")
            continue
        if meta.get("id_prefix") != config.ID_PREFIX:
            printer(f"  [crossref] skipping {name} (ingested under ID_PREFIX="
                    f"{meta.get('id_prefix')!r}, current is {config.ID_PREFIX!r})")
            continue
        repos.append({"repo": meta.get("repo") or name, "root": meta.get("target_dir") or "",
                      "entities": entities.get("entities", {}), "resolutions": resolutions,
                      "modules": [u["key"] for u in units.get("units", []) if u.get("key")]})
    return repos


class _Index:
    """Node lookups for one repo, from its entities (C3)."""

    def __init__(self, repo: dict) -> None:
        self.repo: str = repo["repo"]
        self.root: str = repo.get("root") or ""
        self.modules = set(repo.get("modules") or ())
        self.files = set(repo["entities"])
        self.methods: set[tuple[str, str]] = set()
        self.classes: set[tuple[str, str]] = set()
        self.packages: set[tuple[str, str]] = set()
        self.clauses: dict[str, set[tuple[str, bool]]] = {}     # dir -> {(clause, build_included)}
        files = (repo.get("resolutions") or {}).get("files") or {}
        self.stale = {rel for rel, frec in files.items() if rel in repo["entities"]
                      and (frec or {}).get("sha256") != repo["entities"][rel].get("sha256")}
        for rel, rec in repo["entities"].items():
            for t in rec.get("types", []):
                self.classes.add((rel, t["qualname"]))
                for m in t.get("methods", []):
                    self.methods.add((rel, m["qualname"]))
            for f in rec.get("funcs", []):
                self.methods.add((rel, f["qualname"]))
            if rec.get("package_name"):
                self.packages.add((posixpath.dirname(rel), rec["package_name"]))
                if rec.get("origin") != "xtest":
                    self.clauses.setdefault(posixpath.dirname(rel), set()).add(
                        (rec["package_name"], bool(rec.get("build_included", True))))

    def clause(self, directory: str) -> str | None:
        """The package an import of `directory` names: the single clause of
        its build-included non-xtest files; None when they disagree or the
        directory has no build-included file (R50)."""
        names = {name for name, included in self.clauses.get(directory, set()) if included}
        return next(iter(names)) if len(names) == 1 else None


class _Drop(Exception):
    """A peer relation that cannot become an edge; `reason` is one of DROP_REASONS."""

    def __init__(self, reason: str, target: str) -> None:
        super().__init__(reason)
        self.reason, self.target = reason, target


def _symbol(tgt: dict) -> str:
    return f"{tgt.get('package') or ''}.{tgt['qualname']}"


def _peer_index(tgt: dict | None, ext: dict, label: str, index: dict[str, _Index]) -> _Index:
    """The ingested repo holding a peer target, or a _Drop naming why not."""
    repo = (tgt or {}).get("repo")
    if not repo:
        raise _Drop("no owner", ext.get("module") or "?")
    if repo not in index:
        raise _Drop("repo not ingested", label)
    return index[repo]


def _require(have: bool, label: str) -> None:
    if not have:
        raise _Drop("node missing", label)


def _fresh(idx: _Index, rel: str, label: str) -> None:
    if rel in idx.stale:
        raise _Drop("stale", label)


# ═══════════════════════════════════════════════════════════════════
# Pure derivation
# ═══════════════════════════════════════════════════════════════════


def derive_cross_repo(repos: list[dict], printer=print) -> dict:
    """{"CrossRepoCalls", "CrossRepoImports", "CrossRepoRef": rows (column dicts),
    "drops", "module_cache", "stats"}.

    Only `external` records count. A peer target becomes a row when both ends
    are nodes of ingested repos; rows are deduplicated per (table, kind,
    source, target), keeping the first site in (file, line, column) order.
    Drops: {"reason", "repo", "source", "target"} with source
    "<relpath>:<line>:<col>" (or "<relpath>:<type qualname>" for embeds and
    implements) and a target label in the F3 style ("<repo>:<relpath>:<qualname>",
    "<repo>:<dir>|<clause>", or "<module>@<version>").
    """
    index = {r["repo"]: _Index(r) for r in repos}
    rows: dict[str, list[dict]] = {t: [] for t in TABLES}
    seen: set[tuple] = set()
    drops: list[dict] = []
    module_cache: list[dict] = []
    stats = {"repos": sorted(index), "external_records": 0, "call_edges": 0, "import_edges": 0,
             "ref_edges": 0, "module_cache": 0}
    owners: dict[str, list[_Index]] = {}
    for idx in index.values():
        for module in idx.modules:
            owners.setdefault(module, []).append(idx)

    def emit(table: str, kind: str | None, sid: str, did: str, row: dict) -> None:
        if (table, kind, sid, did) in seen:
            return
        seen.add((table, kind, sid, did))
        edge_table = f"{table}:{kind}" if kind else table
        rows[table].append({"edge_id": pipeline._edge_id(edge_table, sid, did), **row})
        stats[{"CrossRepoCalls": "call_edges", "CrossRepoImports": "import_edges",
               "CrossRepoRef": "ref_edges"}[table]] += 1

    def published(src: _Index, source: str, ext: dict) -> None:
        """A non-peer external target: listed when an ingested repo provides its module."""
        module, version = ext.get("module") or "", ext.get("version") or ""
        mine = [i for i in owners.get(module, []) if i is not src]
        if not mine:
            return
        if len(mine) > 1:
            raise _Drop("two owners", f"{module}@{version}" if version else module)
        owner = mine[0]
        module_cache.append({"repo": src.repo, "source": source, "module": module,
                             "version": version, "owner": owner.repo,
                             "hint": f"re-ingest {src.repo} with --peer {owner.repo}="
                                     f"{owner.root or '<path>'} to edge into its source"})
        stats["module_cache"] += 1

    for repo in sorted(repos, key=lambda r: r["repo"]):
        src = index[repo["repo"]]
        files = repo["resolutions"].get("files", {})
        for rel in sorted(files):
            frec = files[rel]
            work: list[tuple[str, str, dict]] = []
            for rec in sorted(frec.get("calls", []), key=lambda r: (r["site"]["line"], r["site"]["col"])):
                work.append(("call", f"{rel}:{rec['site']['line']}:{rec['site']['col']}", rec))
            for rec in sorted(frec.get("imports", []), key=lambda r: (r.get("line", 0), r.get("col", 0))):
                work.append(("import", f"{rel}:{rec.get('line', 0)}:{rec.get('col', 0)}", rec))
            for key, field, kind in _RELATIONS:
                for rec in sorted(frec.get(key, []), key=lambda r: r["type"]["qualname"]):
                    work.append((kind, f"{rel}:{rec['type']['qualname']}", rec))
            for what, source, rec in work:
                if rec.get("status") != "external":
                    continue
                stats["external_records"] += 1
                ext = rec.get("external") or {}
                try:
                    if not ext.get("peer"):
                        if rel not in src.stale:
                            published(src, source, ext)
                    elif what == "call":
                        _call_edge(src, rel, rec, index, emit)
                    elif what == "import":
                        _import_edge(src, rel, rec, index, emit)
                    else:
                        _ref_edge(src, rel, what, rec, index, emit)
                except _Drop as d:
                    drops.append({"reason": d.reason, "repo": src.repo, "source": source,
                                  "target": d.target})
    stats["drops_by_reason"] = {r: sum(d["reason"] == r for d in drops) for r in DROP_REASONS}
    return {**rows, "drops": drops, "module_cache": module_cache, "stats": stats}


def _call_edge(src: _Index, rel: str, rec: dict, index: dict[str, _Index], emit) -> None:
    tgt, site = rec.get("target"), rec["site"]
    label = f"{(tgt or {}).get('repo')}:{(tgt or {}).get('relpath')}:{(tgt or {}).get('qualname')}"
    _fresh(src, rel, label)
    dst = _peer_index(tgt, rec.get("external") or {}, label, index)
    _fresh(dst, tgt["relpath"], label)
    _require((rel, site["caller"]) in src.methods, label)
    _require((tgt["relpath"], tgt["qualname"]) in dst.methods, label)
    sid = pipeline._make_id("method", src.repo, rel, site["caller"])
    did = pipeline._make_id("method", dst.repo, tgt["relpath"], tgt["qualname"])
    emit("CrossRepoCalls", None, sid, did,
         {"caller_method": sid, "callee_method": did, "source_repo": src.repo,
          "target_repo": dst.repo, "symbol": _symbol(tgt), "site_kind": site["kind"]})


def _import_edge(src: _Index, rel: str, rec: dict, index: dict[str, _Index], emit) -> None:
    tgt = rec.get("target")
    label = f"{(tgt or {}).get('repo')}:{(tgt or {}).get('dir')}|{(tgt or {}).get('clause') or '?'}"
    _fresh(src, rel, label)
    dst = _peer_index(tgt, rec.get("external") or {}, label, index)
    clause = tgt.get("clause") or dst.clause(tgt["dir"])
    label = f"{dst.repo}:{tgt['dir']}|{clause or '?'}"
    _require(rel in src.files, label)
    _require(clause is not None and (tgt["dir"], clause) in dst.packages, label)
    sid = pipeline._make_id("file", src.repo, rel, "")
    did = pipeline._make_id("package", dst.repo, tgt["dir"], clause)
    emit("CrossRepoImports", None, sid, did,
         {"source_file": sid, "target_package": did, "source_repo": src.repo,
          "target_repo": dst.repo, "import_path": rec.get("import_path") or tgt.get("package") or ""})


def _ref_edge(src: _Index, rel: str, kind: str, rec: dict, index: dict[str, _Index], emit) -> None:
    tgt = rec.get("embedded" if kind == "embeds" else "iface")
    own = rec["type"]
    label = f"{(tgt or {}).get('repo')}:{(tgt or {}).get('relpath')}:{(tgt or {}).get('qualname')}"
    _fresh(src, rel, label)
    _fresh(src, own["relpath"], label)
    dst = _peer_index(tgt, rec.get("external") or {}, label, index)
    _fresh(dst, tgt["relpath"], label)
    _require((own["relpath"], own["qualname"]) in src.classes, label)
    _require((tgt["relpath"], tgt["qualname"]) in dst.classes, label)
    sid = pipeline._make_id("class", src.repo, own["relpath"], own["qualname"])
    did = pipeline._make_id("class", dst.repo, tgt["relpath"], tgt["qualname"])
    emit("CrossRepoRef", kind, sid, did,
         {"source_class": sid, "target_class": did, "source_repo": src.repo,
          "target_repo": dst.repo, "symbol": _symbol(tgt), "kind": kind})


# ═══════════════════════════════════════════════════════════════════
# Report + write
# ═══════════════════════════════════════════════════════════════════


def format_drops(drops: list[dict], limit: int = 20) -> str:
    """Console view of what was not edged and why: a count per reason, then the sites."""
    if not drops:
        return "  (nothing dropped)"
    lines = []
    for reason in DROP_REASONS:
        mine = [d for d in drops if d["reason"] == reason]
        if not mine:
            continue
        lines.append(f"  {reason}: {len(mine)} site(s)")
        for d in mine[:limit]:
            lines.append(f"    {d['repo']} {d['source']} -> {d['target']}")
        if len(mine) > limit:
            lines.append(f"    ... and {len(mine) - limit} more")
    return "\n".join(lines)


def run_crossref(printer=print, write: bool = True) -> dict:
    """Discover the ingested repos, derive the cross-repo edges, print the
    drops and module-cache targets, write OUTPUT_DIR/crossref_report.json and,
    unless write=False (--dry-run, which leaves Spanner and crossref_rows.json
    alone), write the rows to Spanner and prune the ones of the previous write
    to the same target that this run no longer derives (`_write_spanner`, R48)."""
    out_root = os.path.abspath(config.OUTPUT_DIR)
    repos = discover_repos(out_root, printer)
    if not repos:
        printer(f"  [crossref] no ingested repos under {out_root}/repos/")
        return {**{t: [] for t in TABLES}, "drops": [], "module_cache": [], "stats": {}}
    if len(repos) == 1:
        printer(f"  [crossref] only one ingested repo ({repos[0]['repo']}); peer targets in "
                "other repos are dropped as 'repo not ingested'")
    printer(f"  [crossref] repos: {', '.join(r['repo'] for r in repos)}")
    derived = derive_cross_repo(repos, printer)
    st = derived["stats"]
    printer(f"  [crossref] {st['call_edges']} CrossRepoCalls, {st['import_edges']} CrossRepoImports, "
            f"{st['ref_edges']} CrossRepoRef edges")
    printer("  [crossref] dropped:")
    printer(format_drops(derived["drops"]))
    for m in derived["module_cache"][:20]:
        printer(f"  [crossref] module cache, not matched: {m['repo']} {m['source']} -> "
                f"{m['module']}@{m['version']} ({m['hint']})")
    report = {"stats": st, "drops": derived["drops"], "module_cache": derived["module_cache"]}
    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, "crossref_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=True, indent=1)
    if write:
        _write_spanner(derived, out_root, printer)
    return derived


def _write_spanner(derived: dict, out_root: str, printer=print) -> None:
    """Upsert the rows, then delete the keys the previous write to the same
    Spanner target (instance, database, graph, ID prefix) wrote and this one
    did not (R48), with pipeline._batch_delete. OUTPUT_DIR/crossref_rows.json keeps the written keys per
    table and the target; until the deletes finish it also holds the keys still
    to delete, so an interrupted run prunes them next time. A record of another
    target prunes nothing and is replaced."""
    target = pipeline._spanner_target(False)
    written = {t: sorted({row["edge_id"] for row in derived[t]}) for t in TABLES}
    path = os.path.join(out_root, ROWS_RECORD)
    previous = _read_rows_record(path, printer)
    stale: dict[str, list[str]] = {t: [] for t in TABLES}
    if previous is not None and previous.get("target") == target:
        stale = {t: sorted(set(previous["tables"].get(t) or []) - set(written[t])) for t in TABLES}
    _write_rows_record(path, target, {t: sorted({*written[t], *stale[t]}) for t in TABLES})
    if any(derived[t] for t in TABLES) or any(stale.values()):
        db = pipeline._get_spanner_db()
        for table in TABLES:
            if derived[table]:
                columns = pipeline._columns(table)
                pipeline._batch_insert(db, table, columns,
                                       [pipeline._row(columns, r) for r in derived[table]])
                printer(f"  [crossref] wrote {len(derived[table])} {table} edges")
        for table in TABLES:
            if stale[table]:
                pipeline._batch_delete(db, table, stale[table])
                printer(f"  [crossref] deleted {len(stale[table])} {table} edges it no longer derives")
    _write_rows_record(path, target, written)


def _read_rows_record(path: str, printer=print) -> dict | None:
    """The previous write's {"version", "target", "tables"}; None when there is none."""
    if not os.path.exists(path):
        return None
    try:
        doc = _read_json(path)
        if doc.get("version") == ROWS_RECORD_VERSION and isinstance(doc.get("tables"), dict):
            return doc
    except (OSError, ValueError, AttributeError):
        pass
    printer(f"  [crossref] ignoring an unreadable {ROWS_RECORD}; this write prunes nothing")
    return None


def _write_rows_record(path: str, target: dict, tables: dict[str, list[str]]) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": ROWS_RECORD_VERSION, "target": target, "tables": tables}, f,
                  ensure_ascii=True, indent=1)
    os.replace(tmp, path)
