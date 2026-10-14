"""Phase 1.6 classification (spec §5.3-5.5, master plan C5).

classify_site turns one `gohelper resolve` site record into a resolutions.json
record: the helper's class decides the rule, the location of the target's
real path decides between builtin, external and in-repo (§5.3), and an
in-repo target becomes a node only at exactly the name range of a node of the
expected kind (R2: no enclosing fallback). The gates (go_gates) run after it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_INTERNAL_UNMAPPED = "_unmapped"   # an in-repo target with no node there (G7 decides)


# ---------------------------------------------------------------- nodes

@dataclass(frozen=True)
class Node:
    kind: str           # "method" (Methods table) or "class" (Classes table)
    unit: str           # module path of the declaring file ("" under no module)
    package: str        # import path of the declaring file
    relpath: str
    qualname: str
    line: int           # the name's line
    member_kind: str    # func, method, init, closure, interface_method; or the type kind
    clause: str         # package clause name

    def target(self) -> dict:
        return {"unit": self.unit, "package": self.package, "relpath": self.relpath,
                "qualname": self.qualname, "line": self.line, "kind": self.kind}


class DeclIndex:
    """The nodes of this repo's entities, by exact name position."""

    def __init__(self, entities: dict[str, dict]) -> None:
        self._at: dict[tuple[str, int, int, str], Node] = {}
        self._named: dict[tuple[str, str, str], Node] = {}
        self._twins: dict[tuple[str, str, str, str], list[Node]] = {}
        for rel, rec in entities.items():
            unit, package = rec.get("module") or "", rec.get("package") or ""
            clause = rec.get("package_name") or ""
            for t in rec.get("types", []):
                if t.get("kind") == "package":
                    continue
                self._add(Node("class", unit, package, rel, t["qualname"], t["name_range"][0],
                               t["kind"], clause), t["name_range"])
                for m in t.get("methods", []):
                    self._add(Node("method", unit, package, rel, m["qualname"], m["name_range"][0],
                                   "interface_method", clause), m["name_range"])
            for f in rec.get("funcs", []):
                if f.get("member_kind") == "var_init":
                    continue                      # <init> is never a call target
                self._add(Node("method", unit, package, rel, f["qualname"], f["name_range"][0],
                               f["member_kind"], clause), f["name_range"])

    def _add(self, node: Node, name_range) -> None:
        self._at.setdefault((node.relpath, name_range[0], name_range[1], node.kind), node)
        self._named.setdefault((node.relpath, node.qualname, node.kind), node)
        key = (os.path.dirname(node.relpath), node.clause, node.kind, node.qualname)
        self._twins.setdefault(key, []).append(node)

    def at(self, relpath: str, line: int, col: int, kind: str) -> Node | None:
        """The node whose name starts at (line, col) of relpath, of this kind."""
        return self._at.get((relpath, line, col, kind))

    def named(self, relpath: str, qualname: str, kind: str) -> Node | None:
        return self._named.get((relpath, qualname, kind))

    def twins(self, node: Node) -> list[Node]:
        """Build twins (spec §3.4): the same qualname in other files of the same
        directory and package clause, sorted by relpath."""
        key = (os.path.dirname(node.relpath), node.clause, node.kind, node.qualname)
        return sorted((n for n in self._twins.get(key, []) if n.relpath != node.relpath),
                      key=lambda n: n.relpath)


# ---------------------------------------------------------------- locations

@dataclass(frozen=True)
class Location:
    kind: str                     # builtin, std, peer, modcache, vendor, repo, unscanned, cgo, outside
    relpath: str | None = None    # repo: the scanned relpath; peer: the path within the peer repo
    module: str | None = None
    version: str = ""
    peer: str | None = None       # the peer repo's name


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _unescape(text: str) -> str:
    """Module cache paths escape upper-case letters as "!" + lower case."""
    out, bang = [], False
    for ch in text:
        if bang:
            out.append(ch.upper())
            bang = False
        elif ch == "!":
            bang = True
        else:
            out.append(ch)
    return "".join(out)


class Locator:
    """Location classification of real paths (spec §5.3); first match wins."""

    def __init__(self, *, repo_root: str, scanned: dict[str, str], goroot: str,
                 modcaches=(), gocache: str | None = None, peers=()) -> None:
        self.repo_root = _norm(repo_root)
        self.scanned = {_norm(p): rel for p, rel in scanned.items()}
        src = os.path.join(_norm(goroot), "src")
        self.goroot_src = src
        self.builtin_dirs = (os.path.join(src, "builtin"), os.path.join(src, "unsafe"))
        self.modcaches = [_norm(m) for m in modcaches if m]
        self.gocache = _norm(gocache) if gocache else None
        dirs = []
        for p in peers:
            root = _norm(p.root)
            for u in p.units:
                dirs.append((_norm(os.path.join(root, u.root)) if u.root else root, p.name, root, u.key))
        self.peer_dirs = sorted(dirs, key=lambda d: -len(d[0]))   # deepest module dir first
        self._memo: dict[str, Location] = {}

    def locate(self, path: str) -> Location:
        """The location of a path, memoized: a pure function of the path and
        the fixed roots, and Location is frozen. Sites and candidates repeat
        the same target files, so most calls skip the path arithmetic."""
        loc = self._memo.get(path)
        if loc is None:
            loc = self._memo[path] = self._locate(path)
        return loc

    def _locate(self, path: str) -> Location:
        if not path:
            return Location("builtin")             # a universe object has no position
        p = _norm(path)
        if any(_under(p, d) for d in self.builtin_dirs):
            return Location("builtin")
        if _under(p, self.goroot_src):
            return Location("std", module="std")
        for mod_dir, name, root, key in self.peer_dirs:
            if _under(p, mod_dir):
                return Location("peer", relpath=os.path.relpath(p, root).replace(os.sep, "/"),
                                module=key, peer=name)
        for cache in self.modcaches:
            if _under(p, cache):
                module, version = _cache_module(os.path.relpath(p, cache))
                return Location("modcache", module=module, version=version)
        if _under(p, self.repo_root):
            rel = os.path.relpath(p, self.repo_root).replace(os.sep, "/")
            if "vendor" in rel.split("/")[:-1]:
                return Location("vendor")
            if p in self.scanned:
                return Location("repo", relpath=self.scanned[p])
            return Location("unscanned")
        if self.gocache and _under(p, self.gocache):
            return Location("cgo", module="C")
        return Location("outside")


def _cache_module(rel: str) -> tuple[str | None, str]:
    """(module path, version) of a path below a module cache root."""
    parts = rel.replace(os.sep, "/").split("/")
    for i, seg in enumerate(parts):
        if "@" in seg:
            name, version = seg.split("@", 1)
            return _unescape("/".join(parts[:i] + [name])), _unescape(version)
    return None, ""


# ---------------------------------------------------------------- the table

@dataclass
class ClassifyContext:
    decls: DeclIndex
    locator: Locator
    extra: dict = field(default_factory=dict)


def blank_record(build: str | None) -> dict:
    """Every C5 record field, unset."""
    return {"status": None, "via": "none", "target": None, "static_target": None, "external": None,
            "candidates": [], "open_world": False, "build_variants": [], "reason": None,
            "build": build}


def qualname_of(t: dict) -> str:
    """The C3 qualname of a helper target: the helper's (R37) or from name and receiver."""
    if t.get("qualname"):
        return t["qualname"]
    name = t.get("name") or ""
    recv = (t.get("recv") or "").lstrip("*")
    if t.get("obj") in ("method", "iface_method") and recv:
        return f"{recv}.{name}"
    return name


def _expected_kind(t: dict) -> str:
    return "class" if t.get("obj") == "type" else "method"


def classify_site(rec: dict, ctx: ClassifyContext) -> dict:
    """The C5 record of one helper site record, before the gates."""
    out = blank_record(rec.get("build"))
    cls = rec.get("class")
    rng = rec.get("site_kind") == "range"
    if cls == "builtin":
        out.update(status="builtin", via="none")
        return out
    if cls == "linkname" and rec.get("target") is None and rec.get("linkname"):
        # A pull stub whose named symbol is not in the load: outside the repo (§5.5).
        out.update(status="external", via="convention:linkname",
                   external=linkname_external(rec["linkname"]))
        return out
    if cls in ("func", "method", "linkname", "funclit"):
        via = {"linkname": "convention:linkname", "funclit": "convention:funclit"}.get(cls, "types")
        if rng:
            via = "convention:range"
        return _with_target(out, rec.get("target"), ctx, via)
    if cls in ("iface", "typeparam"):
        return _iface(out, rec.get("target"), rec.get("iface"), ctx,
                      "convention:range" if rng else "types")
    if cls == "value":
        alias = rec.get("alias")
        if alias is None:
            out.update(status="dynamic", via="none")
            return out
        if alias.get("obj") == "iface_method":
            return _iface(out, alias, rec.get("iface"), ctx, "convention:func-alias")
        return _with_target(out, alias, ctx, "convention:func-alias")
    reason = "missing-module" if cls == "unknown" and rec.get("pkg_errors") else "load-error"
    out.update(status="unresolved", via="none", reason=reason)
    return out


def linkname_external(symbol: str) -> dict:
    """The external record of a //go:linkname target `pkg.F`, `pkg.(*T).m` or
    `pkg.T.m` (R36): module "std" for a standard-library path, else unknown."""
    slash = symbol.rfind("/")
    dot = symbol.find(".", slash + 1)
    pkg, name = (symbol[:dot], symbol[dot + 1:]) if dot > 0 else ("", symbol)
    name = name.replace("(*", "").replace(")", "")
    module = "std" if pkg and "." not in pkg.split("/", 1)[0] else ""
    return {"module": module, "version": "", "package": pkg, "qualname": name, "peer": False}


def _with_target(out: dict, t: dict | None, ctx: ClassifyContext, via: str) -> dict:
    """Resolved, external, builtin or unresolved by the target's location. A
    plain static call that leaves the repo has via "none" (C5); the rules keep
    their convention."""
    external_via = "none" if via == "types" else via
    if t is None:
        out.update(status="unresolved", via="none", reason="load-error")
        return out
    loc = ctx.locator.locate(t.get("path") or "")
    q = qualname_of(t)
    if loc.kind == "builtin":
        out.update(status="builtin", via="none")
    elif loc.kind == "repo":
        node = ctx.decls.at(loc.relpath, t.get("line", 0), t.get("col", 0), _expected_kind(t))
        out.update(status="resolved", via=via, target=node.target() if node else None)
        if node is None:
            out[_INTERNAL_UNMAPPED] = True
    elif loc.kind == "peer":
        out.update(status="external", via=external_via,
                   external={"module": loc.module, "version": "", "package": t.get("pkg") or "",
                             "qualname": q, "peer": True},
                   target={"repo": loc.peer, "unit": loc.module, "package": t.get("pkg") or "",
                           "relpath": loc.relpath, "qualname": q, "line": t.get("line", 0),
                           "kind": _expected_kind(t)})
    elif loc.kind in ("std", "modcache", "vendor", "cgo"):
        module = loc.module or t.get("module") or ""
        version = (loc.version or t.get("version") or "") if loc.kind in ("modcache", "vendor") else ""
        out.update(status="external", via=external_via,
                   external={"module": module, "version": version, "package": t.get("pkg") or "",
                             "qualname": q, "peer": False})
    else:
        out.update(status="unresolved", via="none",
                   reason="unscanned" if loc.kind == "unscanned" else "outside-repo")
    return out


def _iface(out: dict, t: dict | None, info: dict | None, ctx: ClassifyContext, via: str) -> dict:
    """Interface dispatch (spec §5.4): the static target plus in-repo candidates.
    An interface that is not a package-level named type (recv_type "", R2)
    has no node: ambiguous with the candidates, else dynamic."""
    info = info or {}
    items = info.get("candidates") or []
    cands = candidates(items, ctx)
    if info and info.get("recv_type") == "":
        if cands:
            out.update(status="ambiguous", via="types", reason="implementation", candidates=cands,
                       open_world=bool(info.get("open_world")))
            _mark_partial(out, info, items, ctx)
        else:
            out.update(status="dynamic", via="types")
        return out
    out = _with_target(out, t, ctx, via)
    if out["status"] in ("resolved", "external"):
        out["candidates"] = cands
        out["open_world"] = bool(info.get("open_world"))
        _mark_partial(out, info, items, ctx)
    return out


# candidates_partial (R50): a record whose candidate set is not closed, so a
# consumer must not read its candidates as every in-repo target there is.
# The key is present only on such records, with the first reason that holds;
# PossiblyCalls rows do not change.
PARTIAL_SUPPRESSED = "suppressed"                    # G4: no candidate search at all
PARTIAL_VIA_EMBEDDED_EXTERNAL = "via_embedded_external"   # a candidate through an embedded
                                                          # interface is outside the repo


def _mark_partial(out: dict, info: dict, items: list[dict], ctx: ClassifyContext) -> None:
    if info.get("suppressed"):
        out["candidates_partial"] = PARTIAL_SUPPRESSED
    elif any(c.get("via_embedded_iface") and ctx.locator.locate(c.get("path") or "").kind != "repo"
             for c in items):
        out["candidates_partial"] = PARTIAL_VIA_EMBEDDED_EXTERNAL


def candidates(items: list[dict], ctx: ClassifyContext) -> list[dict]:
    """In-repo candidates mapped to Methods nodes, first occurrence per node.
    Candidates without a node (other locations, no exact name range) are left out."""
    out, seen = [], set()
    for c in items:
        loc = ctx.locator.locate(c.get("path") or "")
        if loc.kind != "repo":
            continue
        node = ctx.decls.at(loc.relpath, c.get("line", 0), c.get("col", 0), "method")
        if node is None or (node.relpath, node.qualname) in seen:
            continue
        seen.add((node.relpath, node.qualname))
        out.append({**node.target(), "test_only": bool(c.get("test_only")),
                    "via_embedded_iface": bool(c.get("via_embedded_iface"))})
    return out


def variant_candidates(targets: list[dict], ctx: ClassifyContext) -> list[dict]:
    """The in-repo nodes of the targets package variants disagree on (G1)."""
    items = [{"path": t.get("path"), "line": t.get("line"), "col": t.get("col"),
              "test_only": (t.get("path") or "").endswith("_test.go"), "via_embedded_iface": False}
             for t in targets if t and t.get("obj") != "type"]
    return candidates(items, ctx)
