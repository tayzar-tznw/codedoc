"""Graph explorer API (spec R51): read-only, capped JSON views of one repo's code
knowledge graph, under /api/graph/.

The source is OUTPUT_DIR/repos/<repo>/graph_rows/<Table>.jsonl, which every
`analyze --graph` writes (also with --no-spanner), so the explorer makes no
Spanner and no Gemini call. Tables are read generically: a table's primary key
and an edge table's endpoint columns come from NODE_SPEC / EDGE_SPEC when
graph_generator is importable (codedoc_go_leiden's copy carries its topic
tables), plus the leiden topics schema when present, else from the rows
themselves (Phase 8/9 write the primary key first, then an edge's source and
destination). Summaries come from the wiki's summary caches, which the docs
pages read too. The files are indexed in memory per repo and re-read when one
of them changes (mtime, size or inode: the pipeline replaces files
atomically, which gives a new inode even within one coarse mtime tick).
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sys
import threading
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Callable, Iterable
from pathlib import Path

from fastapi import APIRouter, HTTPException

MAX_NODES = 300           # nodes per graph response
MAX_EDGES = 3000          # edges per graph response
MAX_SEARCH = 50           # search results per response
MAX_PATH_HOPS = 10        # longest path the path search looks for
SUMMARY_LIMIT = 4000      # characters of a summary in node details
CACHED_REPOS = 4          # graphs kept in memory
UNASSIGNED_NAME = "未分類"  # codedoc_go_leiden's topic of the members no community took
TEST_ORIGINS = ("test", "xtest")
DIRECTIONS = {"out": ("out",), "in": ("in",), "both": ("out", "in")}

_TABLE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# Display kinds of the node tables whose kind is not a column (Classes: kind,
# Methods: member_kind). The per-file `<package>` pseudo-type is "pseudo".
_TABLE_KINDS = {"Files": "file", "Packages": "package", "Topics": "topic", "Directories": "directory"}
_KIND_COLUMNS = {"Classes": ("kind", {"package": "pseudo"}, "type"),
                 "Methods": ("member_kind", {}, "func")}
# Search tie-break among equally good matches: types and functions first.
_TABLE_RANK = {"Classes": 0, "Methods": 1, "Packages": 2, "Files": 3, "Topics": 4, "Directories": 5}


class GraphError(Exception):
    """graph_rows/ cannot be read as tables of rows."""


@functools.cache
def _spec() -> tuple[dict[str, str], dict[str, tuple[str, str, str, str]]]:
    """(node table → primary key, edge table → (source column, source table,
    destination column, destination table)) from graph_generator's schema; two
    empty dicts when it cannot be imported (the webapp image ships without it;
    a bad setting can make graph_generator.config raise), so the tables are
    then read from the rows."""
    try:
        from graph_generator.setup_spanner_graph import EDGE_SPEC, NODE_SPEC
    except Exception:  # noqa: BLE001 - any import failure means: read the rows
        return {}, {}
    nodes = {name: pk for name, (pk, _cols) in NODE_SPEC.items()}
    edge_specs = dict(EDGE_SPEC)
    try:
        from graph_generator import topics_schema       # codedoc_go_leiden only
    except ImportError:
        pass
    else:
        for name, spec in topics_schema.NEW_EDGES.items():
            edge_specs.setdefault(name, spec)
    edges = {name: (scol, stab, dcol, dtab)
             for name, (_pk, _cols, (scol, stab), (dcol, dtab)) in edge_specs.items()}
    return nodes, edges


def _scheme_id(prefix: str, kind: str, repo: str, relpath: str) -> str:
    """A node ID of the pipeline's ID scheme 1 (graph_generator.pipeline._make_id
    with an empty qualname): prefix + 16 hex chars of sha256 over the NUL-joined
    inputs. Only used to find the directories that hold no file directly; an ID
    it computes counts only when a Directories row has it."""
    raw = "\x00".join((kind, repo, relpath, ""))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8', 'surrogateescape')).hexdigest()[:16]}"


def _parent_dir(path: str) -> str:
    return path.rpartition("/")[0]


def _ancestors(path: str) -> Iterable[str]:
    """path and every directory above it, up to the repo root ""."""
    while True:
        yield path
        if not path:
            return
        path = _parent_dir(path)


def _inside(base: Path, rel: str) -> Path | None:
    """base/rel when it stays inside base (path-traversal guard)."""
    try:
        path = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    return path if path.is_relative_to(base.resolve()) else None


def _read_rows(path: Path) -> list[dict]:
    """The JSON objects of a .jsonl file. One decode of the whole file, so the
    column names are shared by every row; short strings (IDs, kinds, paths) are
    interned, which keeps a repo of etcd's size well under 100 MB."""
    lines = path.read_text(encoding="utf-8").split("\n")    # not splitlines: U+2028 may sit in a string
    try:
        rows = json.loads("[" + ",".join(line for line in lines if line.strip()) + "]")
    except json.JSONDecodeError:
        for number, line in enumerate(lines, 1):
            try:
                if line.strip():
                    json.loads(line)
            except json.JSONDecodeError as exc:
                raise GraphError(f"graph_rows/{path.name} line {number}: {exc.msg}") from exc
        raise GraphError(f"graph_rows/{path.name} is not JSON Lines") from None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row.pop("embedding", None)
        for key, value in row.items():
            if type(value) is str and len(value) <= 160:
                row[key] = sys.intern(value)
        out.append(row)
    return out


def _table_files(rows_dir: Path) -> list[tuple[str, Path, int, int, int]]:
    """(table, path, mtime_ns, size, inode) of each regular <Table>.jsonl
    directly in graph_rows/ (symlinks and other names are skipped)."""
    out = []
    with os.scandir(rows_dir) as entries:
        for entry in entries:
            stem, ext = os.path.splitext(entry.name)
            if ext != ".jsonl" or not _TABLE_NAME.fullmatch(stem) \
                    or not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            out.append((stem, Path(entry.path), st.st_mtime_ns, st.st_size, st.st_ino))
    return sorted(out)


def find_topic(tree, ref: str) -> dict | None:
    """The topic_tree.json entry that /topic/<ref> names: a top-level topic by
    name, else by id, else a subtopic (codedoc_go_leiden's have their own id
    and page) by id, else by name. webapp/main.py get_topic serves it."""
    if not isinstance(tree, list):
        return None
    top = [t for t in tree if isinstance(t, dict)]
    subs = [s for t in top for s in (t.get("subtopics") or []) if isinstance(s, dict)]
    for entries, key in ((top, "name"), (top, "id"), (subs, "id"), (subs, "name")):
        found = next((t for t in entries if t.get(key) == ref), None)
        if found is not None:
            return found
    return None


def _json_file(path: Path):
    """A JSON file's content, cached by (path, mtime, size, inode); None when
    missing or unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    return _json_cached(str(path), st.st_mtime_ns, st.st_size, st.st_ino)


@functools.lru_cache(maxsize=64)
def _json_cached(path: str, _mtime: int, _size: int, _inode: int):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Node:
    __slots__ = ("id", "table", "row", "kind", "label", "sub", "path", "line", "test",
                 "generated", "name_key", "alt_key", "keys")

    def __init__(self, node_id: str, table: str, row: dict):
        self.id, self.table, self.row = node_id, table, row
        self.kind, self.label, self.sub, self.path = table.lower(), node_id, "", ""
        self.line, self.test, self.generated = 0, False, False
        self.name_key = self.alt_key = ""      # search: the node's name, its other exact name
        self.keys: tuple[str, ...] = ()

    def view(self, **extra) -> dict:
        out = {"id": self.id, "table": self.table, "kind": self.kind, "label": self.label,
               "sub": self.sub, "path": self.path, "line": self.line, "test": self.test,
               "generated": self.generated}
        out.update(extra)
        return out


class EdgeTable:
    __slots__ = ("name", "pk", "src_col", "dst_col", "src_table", "dst_table", "rows", "src",
                 "dst", "adj", "dangling")

    def __init__(self, name: str, pk: str, src_col: str, dst_col: str):
        self.name, self.pk, self.src_col, self.dst_col = name, pk, src_col, dst_col
        self.src_table = self.dst_table = ""
        self.rows: list[dict] = []
        self.src: list[str] = []
        self.dst: list[str] = []
        self.adj: dict[str, dict[str, list[int]]] = {"out": defaultdict(list), "in": defaultdict(list)}
        self.dangling = 0

    def edge_id(self, i: int) -> str:
        value = self.rows[i].get(self.pk) if self.pk else None
        return value if isinstance(value, str) and value else f"{self.name}:{i}"

    def view(self, i: int) -> dict:
        skip = (self.pk, self.src_col, self.dst_col)
        attrs = {k: v for k, v in self.rows[i].items()
                 if k not in skip and (v is None or isinstance(v, (bool, int, float))
                                       or (isinstance(v, str) and len(v) <= 300))}
        return {"id": self.edge_id(i), "table": self.name, "source": self.src[i],
                "target": self.dst[i], "attrs": attrs}

    def other(self, direction: str, i: int) -> str:
        return self.dst[i] if direction == "out" else self.src[i]


def _ends(edges: EdgeTable, direction: str, found: list[int]) -> Iterable[str]:
    """The far ends of the edges `found` of one node, followed in `direction`."""
    for i in found:
        yield edges.other(direction, i)


class Graph:
    """The in-memory indexes of one repo's graph_rows/: nodes by ID, edge
    tables with their adjacency, and the display fields of every node."""

    def __init__(self, root: Path, tables: dict[str, list[dict]]):
        self.root = root
        node_spec, edge_spec = _spec()
        self.nodes: dict[str, Node] = {}
        self.node_tables: dict[str, int] = {}
        self.edges: dict[str, EdgeTable] = {}
        self.empty_tables: list[str] = []
        pending_edges = []
        for table, rows in tables.items():
            first = list(rows[0]) if rows else []
            if table in node_spec or (table not in edge_spec and first and first[0] != "edge_id"):
                pk = node_spec.get(table) or first[0]
                self.node_tables[table] = 0
                for row in rows:
                    node_id = row.get(pk)
                    if isinstance(node_id, str) and node_id and node_id not in self.nodes:
                        self.nodes[node_id] = Node(node_id, table, row)
                        self.node_tables[table] += 1
            elif table in edge_spec:
                scol, _stab, dcol, _dtab = edge_spec[table]
                pending_edges.append((EdgeTable(table, "edge_id", scol, dcol), rows))
            elif len(first) >= 3:
                pending_edges.append((EdgeTable(table, first[0], first[1], first[2]), rows))
            else:
                self.empty_tables.append(table)
        for edges, rows in pending_edges:
            self._add_edges(edges, rows, edge_spec.get(edges.name))
        self.dir_paths = self._dir_paths()
        for node in self.nodes.values():
            self._describe(node)
        self.kinds = Counter(n.kind for n in self.nodes.values())
        self._role_counts: dict[str, dict[str, int]] | None = None

    # -- building ----------------------------------------------------------
    def _add_edges(self, edges: EdgeTable, rows: list[dict], spec) -> None:
        self.edges[edges.name] = edges
        src_tables, dst_tables = Counter(), Counter()
        for row in rows:
            s, d = row.get(edges.src_col), row.get(edges.dst_col)
            if s not in self.nodes or d not in self.nodes:
                edges.dangling += 1
                continue
            i = len(edges.rows)
            edges.rows.append(row)
            edges.src.append(s)
            edges.dst.append(d)
            edges.adj["out"][s].append(i)
            edges.adj["in"][d].append(i)
            if not spec:
                src_tables[self.nodes[s].table] += 1
                dst_tables[self.nodes[d].table] += 1
        if spec:
            edges.src_table, edges.dst_table = spec[1], spec[3]
        else:
            edges.src_table = src_tables.most_common(1)[0][0] if src_tables else ""
            edges.dst_table = dst_tables.most_common(1)[0][0] if dst_tables else ""
        edges.adj = {k: dict(v) for k, v in edges.adj.items()}

    def _dir_paths(self) -> dict[str, str]:
        """Directories node ID → repo-relative path ("" = the root): from
        DirContainsFile for the directories that hold files, else by the ID
        scheme for every directory above a file (verified against the rows)."""
        dirs = {n.id: n for n in self.nodes.values() if n.table == "Directories"}
        if not dirs:
            return {}
        paths: dict[str, str] = {}
        holds = self.edges.get("DirContainsFile")
        if holds:
            for s, d in zip(holds.src, holds.dst):
                directory = self.nodes[d].row.get("directory")
                if s in dirs and isinstance(directory, str):
                    paths.setdefault(s, directory)
        if len(paths) < len(dirs):
            wanted = {a for n in self.nodes.values() if n.table == "Files"
                      for a in _ancestors(str(n.row.get("directory") or ""))}
            for prefix, repo in {(i.rpartition("_")[0], str(n.row.get("repo") or ""))
                                 for i, n in dirs.items() if i not in paths}:
                for path in wanted:
                    node_id = _scheme_id(prefix, "dir", repo, path)
                    if node_id in dirs:
                        paths.setdefault(node_id, path)
        return paths

    def _describe(self, node: Node) -> None:
        row, table = node.row, node.table

        def text(column: str) -> str:
            value = row.get(column)
            return value if isinstance(value, str) else ""

        if table in _KIND_COLUMNS:
            column, renames, default = _KIND_COLUMNS[table]
            node.kind = renames.get(text(column), text(column)) or default
        else:
            node.kind = _TABLE_KINDS.get(table, table.lower())
        file = self.nodes.get(text("file_id"))
        file_row = file.row if file and file.table == "Files" else {}
        if table == "Files":
            node.label, node.sub, node.path = text("file_name") or text("path"), text("directory"), text("path")
            node.test = text("origin") in TEST_ORIGINS or node.path.endswith("_test.go")
            node.generated = bool(row.get("generated"))
            names = (text("file_name"), text("path"))
        elif table in ("Classes", "Methods"):
            qualname = text("qualname") or text("name")
            node.label = qualname
            if qualname == "<package>":
                node.label = f"<package> {file_row.get('file_name') or ''}".strip()
            node.sub = text("package") or str(file_row.get("package") or "")
            node.path = str(file_row.get("path") or "")
            node.line = row.get("start_line") if isinstance(row.get("start_line"), int) else 0
            node.test = text("origin") in TEST_ORIGINS
            node.generated = bool(file_row.get("generated"))
            clause = str(file_row.get("package_name") or "")
            names = (qualname, text("name"), f"{clause}.{qualname}" if clause else "",
                     text("fqcn") or text("fqmn"))
        elif table == "Packages":
            node.label = text("name") or text("import_path")
            node.sub = text("import_path") or text("directory")
            node.test = text("origin") in TEST_ORIGINS
            names = (node.label, text("import_path"), text("directory"))
        elif table == "Directories":
            path = self.dir_paths.get(node.id)
            node.label = text("name") or (path or "/")
            node.path = path or ""
            node.sub = "/" if path == "" else (path or "")
            names = (node.label, node.path)
        else:
            node.label = text("name") or text("title") or node.id
            names = (node.label,)
        node.name_key = names[0].lower()
        node.alt_key = names[1].lower() if len(names) > 1 else ""
        node.keys = tuple(dict.fromkeys(k.lower() for k in names if k)) + (node.id.lower(),)

    # -- queries -----------------------------------------------------------
    def node(self, node_id: str) -> Node:
        node = self.nodes.get(node_id) if len(node_id) <= 256 else None
        if node is None:
            raise HTTPException(404, {"error": f"node {node_id!r} not found",
                                      "hint": "search for the node; IDs change when the repo is re-analyzed"})
        return node

    def stats(self) -> dict:
        return {
            "node_tables": [{"table": t, "rows": n} for t, n in self.node_tables.items()],
            "edge_tables": [{"table": e.name, "source": e.src_table, "target": e.dst_table,
                             "rows": len(e.rows), "dangling": e.dangling}
                            for e in self.edges.values()],
            "empty_tables": self.empty_tables,
            "kinds": dict(self.kinds.most_common()),
            "topics": self._topic_mode(),
            "tests": sum(n.test for n in self.nodes.values()),
            "generated": sum(n.generated for n in self.nodes.values()),
        }

    def search(self, query: str, kind: str | None, limit: int, hide) -> tuple[list[dict], int]:
        q = query.strip()
        ql = q.lower()
        tokens = ql.split()
        if not tokens:
            return [], 0
        ranked = []
        for node in self.nodes.values():
            if kind and kind not in (node.table, node.kind):
                continue
            if hide(node):
                continue
            score = self._match(node, q, ql, tokens)
            if score is not None:
                ranked.append((score, node.test, _TABLE_RANK.get(node.table, 9), len(node.label),
                               node.label, node.id, node))
        ranked.sort(key=lambda r: r[:6])
        return [r[-1].view() for r in ranked[:limit]], len(ranked)

    @staticmethod
    def _match(node: Node, q: str, ql: str, tokens: list[str]) -> int | None:
        """0 (best) to 6, or None: exact label, exact name, exact other key,
        prefix, segment prefix, substring of the name, substring of any key.
        A query of several words matches when each word is in some key."""
        keys = node.keys
        if len(tokens) > 1:
            return 7 if all(any(t in k for k in keys) for t in tokens) else None
        if node.label == q:
            return 0
        if ql in (node.name_key, node.alt_key):
            return 1
        if ql in keys:
            return 2
        if node.name_key.startswith(ql):
            return 3
        if any(k.startswith(ql) or f".{ql}" in k or f"/{ql}" in k for k in keys):
            return 4
        if ql in node.name_key:
            return 5
        if any(ql in k for k in keys):
            return 6
        return None

    def details(self, node: Node) -> dict:
        row = node.row
        file = self.nodes.get(row.get("file_id")) if isinstance(row.get("file_id"), str) else None
        summary, source = self._summary(node)
        doc = row.get("summary") if node.table != "Topics" and isinstance(row.get("summary"), str) else ""
        if source == "graph_rows":
            doc = ""
        edges = []
        for e in self.edges.values():
            for direction in ("out", "in"):
                count = len(e.adj[direction].get(node.id, ()))
                if count:
                    edges.append({"table": e.name, "direction": direction, "count": count,
                                  "peer": e.dst_table if direction == "out" else e.src_table})
        refs = {}
        for column, value in row.items():
            target = self.nodes.get(value) if isinstance(value, str) else None
            if target is not None and target is not node:
                refs[column] = target.view()
        end = row.get("end_line") if isinstance(row.get("end_line"), int) else 0
        out = {
            "node": node.view(),
            "columns": {k: v for k, v in row.items() if k != "summary"},
            "summary": summary[:SUMMARY_LIMIT], "summary_source": source,
            "summary_truncated": len(summary) > SUMMARY_LIMIT,
            "doc": (doc or "")[:SUMMARY_LIMIT],
            "location": {"path": node.path, "start_line": node.line, "end_line": end} if node.path else None,
            "module": row.get("module") or (file.row.get("module") if file else "") or "",
            "edges": edges,
            "refs": refs,
            "links": self._links(node),
        }
        if node.table == "Topics":
            out["roles"] = self._roles().get(node.id, {})
            out["unassigned"] = self._unassigned(node)
        return out

    def neighbours(self, center: Node, tables: list[str], directions: tuple[str, ...],
                   depth: int, limit: int, hide) -> dict:
        seen = {center.id: 0}
        omitted: dict[str, set] = defaultdict(set)
        frontier = [center.id]
        for level in range(1, depth + 1):
            groups = deque()
            for node_id in frontier:
                for table in tables:
                    e = self.edges[table]
                    for direction in directions:
                        found = e.adj[direction].get(node_id)
                        if found:
                            groups.append((table, _ends(e, direction, found)))
            frontier = []
            while groups:       # round robin, so every (node, table, direction) gets a share
                table, candidates = groups.popleft()
                for other in candidates:
                    if other in seen or hide(self.nodes[other]):
                        continue
                    if len(seen) >= limit:
                        omitted[table].add(other)
                        continue
                    seen[other] = level
                    frontier.append(other)
                    groups.append((table, candidates))
                    break
        nodes = [self.nodes[i].view(depth=d, degree=self._degree(i, tables)) for i, d in seen.items()]
        edges, edges_cut = self._induced(seen, tables)
        omitted_counts = {t: len(ids - seen.keys()) for t, ids in omitted.items()}
        return {"center": center.id, "nodes": nodes, "edges": edges,
                "truncated": bool(omitted_counts) or edges_cut,
                "omitted": {t: n for t, n in omitted_counts.items() if n}}

    def _degree(self, node_id: str, tables: list[str]) -> int:
        return sum(len(self.edges[t].adj[d].get(node_id, ())) for t in tables for d in ("out", "in"))

    def _induced(self, included, tables: list[str]) -> tuple[list[dict], bool]:
        """Every edge of `tables` between two included nodes, at most MAX_EDGES."""
        out = []
        for node_id in included:
            for table in tables:
                e = self.edges[table]
                for i in e.adj["out"].get(node_id, ()):
                    if e.dst[i] in included:
                        if len(out) >= MAX_EDGES:
                            return out, True
                        out.append(e.view(i))
        return out, False

    def path(self, source: Node, target: Node, tables: list[str], directions: tuple[str, ...],
             max_hops: int) -> dict | None:
        """A shortest path from source to target over `tables`, or None."""
        prev: dict[str, tuple[str, str, int] | None] = {source.id: None}
        frontier = [source.id]
        for _hop in range(max_hops):
            if target.id in prev or not frontier:
                break
            nxt = []
            for node_id in frontier:
                for table in tables:
                    e = self.edges[table]
                    for direction in directions:
                        for i in e.adj[direction].get(node_id, ()):
                            other = e.other(direction, i)
                            if other not in prev:
                                prev[other] = (node_id, table, i)
                                nxt.append(other)
            frontier = nxt
        if target.id not in prev:
            return None
        chain, edges, at = [target.id], [], target.id
        while prev[at] is not None:
            at, table, i = prev[at]
            chain.append(at)
            edges.append(self.edges[table].view(i))
        chain.reverse()
        edges.reverse()
        return {"nodes": [self.nodes[i].view(depth=n) for n, i in enumerate(chain)],
                "edges": edges, "hops": len(edges)}

    # -- overviews -----------------------------------------------------------
    def package_map(self, limit: int, hide) -> dict:
        """Packages with the imports between them: a PackageImports row (file →
        package) counts for the package that contains the file."""
        packages = {n.id: n for n in self.nodes.values() if n.table == "Packages" and not hide(n)}
        owner = {}
        contains = self.edges.get("PackageContainsFile")
        if contains:
            owner = {f: p for p, f in zip(contains.src, contains.dst)}
        weights: Counter = Counter()
        imports = self.edges.get("PackageImports")
        if imports:
            for f, d in zip(imports.src, imports.dst):
                p = owner.get(f)
                if p in packages and d in packages and p != d and not hide(self.nodes[f]):
                    weights[(p, d)] += 1
        degree: Counter = Counter()
        for (s, d), w in weights.items():
            degree[s] += w
            degree[d] += w
        chosen = sorted(packages.values(), key=lambda n: (-degree[n.id], n.sub, n.id))[:limit]
        keep = {n.id for n in chosen}
        pairs = sorted(((s, d, w) for (s, d), w in weights.items() if s in keep and d in keep),
                       key=lambda x: (-x[2], x[0], x[1]))
        fan_out, fan_in = Counter(s for s, _d, _w in pairs), Counter(d for _s, d, _w in pairs)
        nodes = [n.view(module=n.row.get("module") or "", size=n.row.get("file_count") or 0,
                        imports=fan_out[n.id], imported_by=fan_in[n.id]) for n in chosen]
        edges = [{"id": f"PackageImports:{s}>{d}", "table": "PackageImports", "source": s,
                  "target": d, "attrs": {"weight": w}} for s, d, w in pairs[:MAX_EDGES]]
        return {"mode": "packages", "nodes": nodes, "edges": edges,
                "truncated": len(packages) > limit or len(pairs) > MAX_EDGES,
                "omitted": {"Packages": max(0, len(packages) - limit)} if len(packages) > limit else {}}

    def directory_tree(self, limit: int, hide, root: str = "") -> dict:
        """The directories that hold scanned files, as a tree from `root` (""
        is the repo root), breadth first and largest subtree first until the
        cap; a node's `more` counts its subdirectories past the cap."""
        direct: Counter = Counter()
        known: set[str] = set()        # every directory above a scanned file, hidden or not
        for n in self.nodes.values():
            if n.table == "Files":
                known.update(_ancestors(str(n.row.get("directory") or "")))
                if not hide(n):
                    direct[str(n.row.get("directory") or "")] += 1
        total: Counter = Counter()
        for d, count in direct.items():
            for a in _ancestors(d):
                total[a] += count
        children: dict[str, list[str]] = defaultdict(list)
        for d in total:
            if d:
                children[_parent_dir(d)].append(d)
        if root and root not in known:
            raise HTTPException(404, {"error": f"directory {root!r} not found",
                                      "hint": "pick a directory from the directory map"})
        by_path = {p: i for i, p in self.dir_paths.items()}
        root_name = self.nodes[by_path[""]].label if "" in by_path else "/"
        # a root whose files are all hidden stays, with 0 files
        nodes, edges, queue, shown = [], [], deque([root] if total or root else []), set()
        while queue and len(nodes) < limit:
            d = queue.popleft()
            shown.add(d)
            kids = sorted(children.get(d, ()), key=lambda c: (-total[c], c))
            queue.extend(kids)
            node_id = by_path.get(d)
            base = self.nodes[node_id].view() if node_id else {
                "id": f"dir:{d}", "table": "Directories", "kind": "directory",
                "label": d.rpartition("/")[2] or root_name, "sub": d or "/", "path": d, "line": 0,
                "test": False, "generated": False}
            base.update(label=root_name if not d else base["label"], path=d, files=direct[d],
                        total=total[d], virtual=node_id is None)
            nodes.append(base)
            if d != root:
                parent = by_path.get(_parent_dir(d)) or f"dir:{_parent_dir(d)}"
                edges.append({"id": f"tree:{parent}>{base['id']}", "table": "tree",
                              "source": parent, "target": base["id"], "attrs": {}})
        hidden = sum(1 for d in total if d not in shown and (not root or d == root or d.startswith(root + "/")))
        for n in nodes:
            n["more"] = sum(1 for c in children.get(n["path"], ()) if c not in shown)
        return {"mode": "directories", "root": root, "nodes": nodes, "edges": edges,
                "truncated": hidden > 0, "omitted": {"Directories": hidden} if hidden else {}}

    def _topic_mode(self) -> str:
        topics = [n for n in self.nodes.values() if n.table == "Topics"]
        if not topics:
            return "none"
        leveled = any(isinstance(n.row.get("level"), int) and n.row["level"] >= 1 for n in topics)
        return "levels" if leveled else "files"

    def _unassigned(self, node: Node) -> bool:
        return node.row.get("name") == UNASSIGNED_NAME or node.row.get("label_source") == "fixed"

    def _roles(self) -> dict[str, dict[str, int]]:
        """Topic ID → member count per role (ClassBelongsToTopic.role)."""
        if self._role_counts is None:
            roles: dict[str, Counter] = defaultdict(Counter)
            e = self.edges.get("ClassBelongsToTopic")
            if e:
                for i, topic in enumerate(e.dst):
                    roles[topic][str(e.rows[i].get("role") or "member")] += 1
            self._role_counts = {t: dict(c.most_common()) for t, c in roles.items()}
        return self._role_counts

    def topic_map(self, limit: int, hide) -> dict:
        mode = self._topic_mode()
        if mode == "none":
            return {"mode": "none", "nodes": [], "edges": [], "truncated": False, "omitted": {}}
        topics = {n.id: n for n in self.nodes.values() if n.table == "Topics"}
        return self._topic_levels(topics, limit) if mode == "levels" else \
            self._topic_files(topics, limit, hide)

    def _topic_levels(self, topics: dict[str, Node], limit: int) -> dict:
        """codedoc_go_leiden: level-1 topics holding their level-2 subtopics,
        weighted TopicDependsOn between them, and 「未分類」 flagged apart."""
        parent = {}
        contains = self.edges.get("TopicContainsTopic")
        if contains:
            parent = {c: p for p, c in zip(contains.src, contains.dst)}
        for n in topics.values():
            p = n.row.get("parent_topic_id")
            if isinstance(p, str) and p in topics:
                parent.setdefault(n.id, p)
        roles = self._roles()

        def level(n: Node) -> int:
            value = n.row.get("level")
            return value if isinstance(value, int) else 1

        def size(n: Node) -> int:
            value = n.row.get("size")
            return value if isinstance(value, int) else 0

        ordered = sorted(topics.values(), key=lambda n: (level(n), -size(n), n.label, n.id))
        chosen = ordered[:limit]
        keep = {n.id for n in chosen}
        nodes = []
        for n in chosen:
            p = parent.get(n.id)
            nodes.append(n.view(level=level(n), parent=p if p in keep else None, size=size(n),
                                files=n.row.get("file_count") or 0, unassigned=self._unassigned(n),
                                roles=roles.get(n.id, {}),
                                label_source=n.row.get("label_source") or ""))
        rows = []
        depends = self.edges.get("TopicDependsOn")
        if depends:
            rows = sorted((i for i in range(len(depends.rows))
                           if depends.src[i] in keep and depends.dst[i] in keep),
                          key=lambda i: -(depends.rows[i].get("weight") or 0))
        edges = [depends.view(i) for i in rows[:MAX_EDGES]] if depends else []
        cut = len(topics) - len(chosen)
        return {"mode": "levels", "nodes": nodes, "edges": edges,
                "truncated": cut > 0 or len(rows) > MAX_EDGES,
                "omitted": {"Topics": cut} if cut else {}}

    def _topic_files(self, topics: dict[str, Node], limit: int, hide) -> dict:
        """codedoc_go: the LLM topics and their files (FileBelongsToTopic), the
        files taken in turn from each topic until the cap."""
        members: dict[str, list[str]] = defaultdict(list)
        belongs = self.edges.get("FileBelongsToTopic")
        if belongs:
            for f, t in zip(belongs.src, belongs.dst):
                if t in topics and not hide(self.nodes[f]):
                    members[t].append(f)
        ordered = sorted(topics.values(), key=lambda n: (-len(members[n.id]), n.label, n.id))[:limit]
        nodes = [n.view(files=len(members[n.id]), unassigned=self._unassigned(n)) for n in ordered]
        included = {n.id for n in ordered}
        queues = deque((t.id, deque(sorted(members[t.id], key=lambda f: self.nodes[f].path)))
                       for t in ordered if members[t.id])
        edges, omitted = [], set()
        while queues:
            topic, files = queues.popleft()
            f = files.popleft()
            if files:
                queues.append((topic, files))
            if f not in included:
                if len(included) >= limit:
                    omitted.add(f)
                    continue
                included.add(f)
                nodes.append(self.nodes[f].view())
            edges.append({"id": f"FileBelongsToTopic:{f}>{topic}", "table": "FileBelongsToTopic",
                          "source": f, "target": topic, "attrs": {}})
        cut = len(topics) - len(ordered)
        out_omitted = {k: v for k, v in (("Topics", cut), ("Files", len(omitted - included))) if v}
        return {"mode": "files", "nodes": nodes, "edges": edges[:MAX_EDGES],
                "truncated": bool(out_omitted) or len(edges) > MAX_EDGES, "omitted": out_omitted}

    # -- summaries and links ---------------------------------------------------
    def _node_cache(self, relpath: str) -> dict:
        path = _inside(self.root / "summaries" / "nodes", f"{relpath}.json") if relpath else None
        data = _json_file(path) if path else None
        return data if isinstance(data, dict) else {}

    def _summary(self, node: Node) -> tuple[str, str | None]:
        """(text, source): the wiki summary from the summary cache, else the
        summary column of graph_rows (the Go doc comment, or a topic's page)."""
        row, text = node.row, None
        if node.table == "Files":
            text = self._node_cache(node.path).get("file")
        elif node.table == "Classes" and row.get("kind") == "package":
            text = self._node_cache(node.path).get("file")    # the <package> takes the file summary
        elif node.table in ("Classes", "Methods"):
            text = (self._node_cache(node.path).get("nodes") or {}).get(row.get("qualname"))
        elif node.table == "Packages":
            entries = _json_file(self.root / "summaries" / "packages.json") or {}
            entry = entries.get(f"{row.get('directory') or ''}|{row.get('name') or ''}") \
                if isinstance(entries, dict) else None
            text = entry.get("summary") if isinstance(entry, dict) else None
        elif node.table == "Directories" and node.id in self.dir_paths:
            entry = self._dir_entry(self.dir_paths[node.id])
            text = entry.get("summary") if isinstance(entry, dict) else None
        if isinstance(text, str) and text.strip():
            return text, "wiki"
        column = row.get("summary")
        if isinstance(column, str) and column.strip():
            return column, "graph_rows"
        return "", None

    def _dir_entry(self, path: str):
        entries = _json_file(self.root / "summaries" / "dirs.json")
        if not isinstance(entries, dict):
            return None
        if path:
            return entries.get(path)
        return next((entries[k] for k in ("", ".") if k in entries), None)

    def _topic_parent(self, topic: Node) -> Node | None:
        contains = self.edges.get("TopicContainsTopic")
        found = contains.adj["in"].get(topic.id) if contains else None
        parent = contains.src[found[0]] if found else topic.row.get("parent_topic_id")
        return self.nodes.get(parent) if isinstance(parent, str) else None

    def _topic_page(self, topic: Node) -> tuple[str, str] | None:
        """(ref for /topic/<ref>, label) of the docs page for this topic: its
        own when topic_tree.json lists it (a top-level topic, or a leiden
        subtopic with its own id) and the page exists, else its parent's, else,
        for a topic without parent, a top-level entry of the same name. The ref
        is what find_topic resolves to that entry: its name when unambiguous,
        else its id."""
        tree = _json_file(self.root / "topics" / "topic_tree.json")
        if not isinstance(tree, list):
            return None
        top = [t for t in tree if isinstance(t, dict)]
        listed = [t for t in top + [s for t in top for s in (t.get("subtopics") or []) if isinstance(s, dict)]
                  if t.get("id")]
        entry = next((t for t in listed if t["id"] == topic.id), None)
        parent = self._topic_parent(topic) if entry is None else None
        if entry is None and parent is not None:
            entry = next((t for t in listed if t["id"] == parent.id), None)
        if entry is None and parent is None:
            entry = next((t for t in top if t.get("id") and t.get("name") == topic.label), None)
        if entry is None:
            return None
        page = _inside(self.root / "topics", f"{entry['id']}.md")
        if page is None or not page.is_file():
            return None
        name = entry.get("name")
        ref = name if name and find_topic(tree, name) is entry else entry["id"]
        if find_topic(tree, ref) is not entry:      # an id shadowed by another topic's name
            return None
        return ref, str(name or entry["id"])

    def _topics_of(self, node: Node) -> list[Node]:
        """The topics a node belongs to: a class's (or a method's class's)
        ClassBelongsToTopic topics when there are any, else its file's
        FileBelongsToTopic topics."""
        if node.table == "Topics":
            return [node]
        row = node.row
        owners = {"Files": [("FileBelongsToTopic", node.id)],
                  "Classes": [("ClassBelongsToTopic", node.id), ("FileBelongsToTopic", row.get("file_id"))],
                  "Methods": [("ClassBelongsToTopic", row.get("class_id")),
                              ("FileBelongsToTopic", row.get("file_id"))]}.get(node.table, [])
        for table, owner in owners:
            e = self.edges.get(table)
            found = e.adj["out"].get(owner, ()) if e and isinstance(owner, str) else ()
            if found:
                return [self.nodes[t] for t in dict.fromkeys(e.dst[i] for i in found)]
        return []

    def _links(self, node: Node) -> list[dict]:
        """The docs pages that exist for this node: its summary page and topic pages."""
        links, row = [], node.row
        if node.table in ("Files", "Classes", "Methods") and node.path \
                and self._node_cache(node.path):
            links.append({"type": "summary", "kind": "files", "name": node.path})
        elif node.table == "Packages":
            key = f"{row.get('directory') or ''}|{row.get('name') or ''}"
            entries = _json_file(self.root / "summaries" / "packages.json")
            if isinstance(entries, dict) and key in entries:
                links.append({"type": "summary", "kind": "packages", "name": key})
        elif node.table == "Directories" and node.id in self.dir_paths:
            path = self.dir_paths[node.id]
            if self._dir_entry(path) is not None:
                links.append({"type": "summary", "kind": "dirs", "name": path or "/"})
        for topic in self._topics_of(node):
            page = self._topic_page(topic)
            if page and not any(link["type"] == "topic" and link["name"] == page[0] for link in links):
                links.append({"type": "topic", "name": page[0], "label": page[1]})
        return links


# ---------------------------------------------------------------------------
# Loading and caching
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_graphs: OrderedDict[str, tuple[tuple, Graph]] = OrderedDict()


def load_graph(root: Path) -> Graph:
    """The Graph of one repo dir, from memory unless a graph_rows file changed."""
    rows_dir = root / "graph_rows"
    if not rows_dir.is_dir():
        raise HTTPException(404, {"error": f"graph_rows not found in {root.name}",
                                  "hint": "run `python -m graph_generator analyze <dir> --graph` first"})
    busy = HTTPException(503, {"error": "graph_rows changed while it was read",
                               "hint": "an analyze run is rewriting it; retry"})
    try:
        files = _table_files(rows_dir)
    except FileNotFoundError:
        raise busy from None
    signature = tuple((table, mtime, size, inode) for table, _path, mtime, size, inode in files)
    key = str(rows_dir.resolve())
    with _lock:
        hit = _graphs.get(key)
        if hit and hit[0] == signature:
            _graphs.move_to_end(key)
            return hit[1]
        try:
            graph = Graph(root, {table: _read_rows(path) for table, path, *_stat in files})
        except FileNotFoundError:
            raise busy from None
        except (GraphError, UnicodeDecodeError) as exc:
            raise HTTPException(500, {"error": str(exc),
                                      "hint": "re-run `python -m graph_generator analyze <dir> --graph`"}) from None
        _graphs[key] = (signature, graph)
        _graphs.move_to_end(key)
        while len(_graphs) > CACHED_REPOS:
            _graphs.popitem(last=False)
        return graph


# ---------------------------------------------------------------------------
# Request parameters
# ---------------------------------------------------------------------------
def _hide(hide_tests: bool, hide_generated: bool, keep: str | None = None) -> Callable[[Node], bool]:
    def hidden(node: Node) -> bool:
        return node.id != keep and ((hide_tests and node.test) or (hide_generated and node.generated))
    return hidden


def _tables(graph: Graph, tables: str | None) -> list[str]:
    """The edge tables named by ?tables= (comma-separated), in that order;
    every table present when the parameter is absent, none when it is empty.
    A schema table this graph lacks is skipped; an unknown name is a 400."""
    if tables is None:
        return list(graph.edges)
    names = list(dict.fromkeys(t.strip() for t in tables.split(",") if t.strip()))
    unknown = [t for t in names if t not in graph.edges and t not in _spec()[1]]
    if unknown:
        raise HTTPException(400, f"unknown edge table(s): {', '.join(unknown)}; "
                                 f"this graph has {', '.join(graph.edges) or 'none'}")
    return [t for t in names if t in graph.edges]


def _direction(direction: str) -> tuple[str, ...]:
    if direction not in DIRECTIONS:
        raise HTTPException(400, "direction must be 'out', 'in' or 'both'")
    return DIRECTIONS[direction]


def _limit(limit: int, cap: int) -> int:
    if not 1 <= limit <= cap:
        raise HTTPException(400, f"limit must be between 1 and {cap}")
    return limit


def make_router(repo_dir: Callable[[str | None], Path]) -> APIRouter:
    """The /api/graph routes; repo_dir maps ?repo= to OUTPUT_DIR/repos/<repo>/
    (or raises the docs API's 404)."""
    router = APIRouter(prefix="/api/graph")

    def graph_of(repo: str | None) -> Graph:
        return load_graph(repo_dir(repo))

    @router.get("/stats")
    def stats(repo: str | None = None):
        root = repo_dir(repo)
        graph = load_graph(root)
        return {"repo": root.name, **graph.stats(),
                "limits": {"nodes": MAX_NODES, "edges": MAX_EDGES, "search": MAX_SEARCH,
                           "path_hops": MAX_PATH_HOPS}}

    @router.get("/search")
    def search(q: str = "", kind: str | None = None, limit: int = MAX_SEARCH,
               hide_tests: bool = False, hide_generated: bool = False, repo: str | None = None):
        graph = graph_of(repo)
        limit = _limit(limit, MAX_SEARCH)
        if kind and kind not in graph.node_tables and kind not in graph.kinds:
            raise HTTPException(400, f"unknown kind {kind!r}: use a node table "
                                     f"({', '.join(graph.node_tables)}) or a node kind")
        if len(q) > 200:
            raise HTTPException(400, "q is longer than 200 characters")
        results, total = graph.search(q, kind, limit, _hide(hide_tests, hide_generated))
        return {"query": q, "results": results, "total": total, "truncated": total > len(results)}

    @router.get("/node/{node_id}")
    def node(node_id: str, repo: str | None = None):
        graph = graph_of(repo)
        return graph.details(graph.node(node_id))

    @router.get("/neighbors/{node_id}")
    def neighbours(node_id: str, tables: str | None = None, direction: str = "both", depth: int = 1,
                   limit: int = MAX_NODES, hide_tests: bool = False, hide_generated: bool = False,
                   repo: str | None = None):
        graph = graph_of(repo)
        center = graph.node(node_id)
        if depth not in (1, 2):
            raise HTTPException(400, "depth must be 1 or 2")
        result = graph.neighbours(center, _tables(graph, tables), _direction(direction), depth,
                                  _limit(limit, MAX_NODES), _hide(hide_tests, hide_generated, center.id))
        return {**result, "depth": depth, "direction": direction}

    @router.get("/path")
    def path(source: str, target: str, tables: str | None = None, direction: str = "out",
             max_hops: int = 6, repo: str | None = None):
        graph = graph_of(repo)
        start, end = graph.node(source), graph.node(target)
        if not 1 <= max_hops <= MAX_PATH_HOPS:
            raise HTTPException(400, f"max_hops must be between 1 and {MAX_PATH_HOPS}")
        found = graph.path(start, end, _tables(graph, tables), _direction(direction), max_hops)
        if found is None:
            raise HTTPException(404, {"error": f"no path from {start.label} to {end.label} "
                                               f"within {max_hops} hops",
                                      "hint": "allow more edge tables, both directions or more hops"})
        return found

    @router.get("/overview/{name}")
    def overview(name: str, limit: int = MAX_NODES, hide_tests: bool = False,
                 hide_generated: bool = False, root: str = "", repo: str | None = None):
        maps = {"packages": Graph.package_map, "directories": Graph.directory_tree,
                "topics": Graph.topic_map}
        if name not in maps:
            raise HTTPException(404, f"overview must be one of {', '.join(maps)}")
        if root and name != "directories":
            raise HTTPException(400, "root applies to the directories overview only")
        graph = graph_of(repo)
        hide = _hide(hide_tests, hide_generated)
        if name == "directories":
            return graph.directory_tree(_limit(limit, MAX_NODES), hide, root.strip("/"))
        return maps[name](graph, _limit(limit, MAX_NODES), hide)

    return router
