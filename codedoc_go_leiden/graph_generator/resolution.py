"""Phase 1.6: `gohelper resolve` per group and build configuration, and
resolutions.json (spec §5, master plan C2 and C5).

Each group is loaded once per build configuration, primary first. A file is
answered by the first configuration whose load covers it (spec §3.4): the
helper's `file` record says which files a load accepted, and only the records
of those files are taken from that load. Entity sites join helper records on
(relpath, offset) (C2/C3): a `site` record is classified (go_classify) and
gated (go_gates); a `conversion` record drops the site, as a `notcall` does
for a range candidate; a range candidate without a site record is dropped;
any other site without a record gets its file's reason (G5, G6, R5,
no-module, load-error).

Freshness (spec §2.6): Phase 1.6 starts with go_extract.refresh_entities;
run_resolution refuses entities that differ from the disk before the first
load (StaleEntitiesError, retried once by resolve_phase); a file whose bytes
in a load or after the last load differ from its entities is stale (G8), and
so is every record pointing into it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import config
from .go_classify import ClassifyContext, DeclIndex, Locator, classify_site, qualname_of
from .go_extract import StaleEntitiesError, refresh_entities
from .go_gates import (SiteContext, apply_gates, load_failure_reason, mark_stale, no_answer_reason,
                       no_answer_record)

__all__ = ["RESOLUTIONS_VERSION", "StaleEntitiesError", "HelperRun", "resolve_phase",
           "run_resolution", "write_json_atomic"]

RESOLUTIONS_VERSION = 1

Printer = Callable[[str], None]


@dataclass
class HelperRun:
    """One `gohelper resolve` process: its JSON lines and how it ended."""
    records: list[dict] = field(default_factory=list)
    returncode: int = 0
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False


# (argv, env, cwd, out_path) -> HelperRun
Runner = Callable[[list[str], dict[str, str], str, str], HelperRun]


def write_json_atomic(data: Any, path: str) -> None:
    """ASCII JSON in a temp file of the same directory, then os.replace."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        # json.dumps uses the C encoder; json.dump streams through the pure-Python
        # one, several times slower. The bytes are the same.
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=True))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def run_helper(argv: list[str], env: dict[str, str], cwd: str, out_path: str, *,
               timeout: float) -> HelperRun:
    """Run `gohelper resolve ... --out out_path` and read its JSON lines."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([*argv, "--out", out_path], cwd=cwd, env=env, capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return HelperRun(returncode=-1, stderr=f"timed out after {timeout:.0f}s (GO_HELPER_TIMEOUT)",
                         seconds=time.perf_counter() - t0, timed_out=True)
    run = HelperRun(returncode=proc.returncode, stderr=proc.stderr.decode("utf-8", "replace"),
                    seconds=time.perf_counter() - t0)
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                try:
                    run.records.append(json.loads(line))
                except ValueError:
                    run.returncode = run.returncode or 1
                    run.stderr += f"\nresolve output line is not JSON: {line[:200]!r}"
                    break
    return run


def resolve_args(helper: str, repo_root: str, group, build, peer_dirs: list[str]) -> list[str]:
    """The `gohelper resolve` command line for one group and build (C2)."""
    return [helper, "resolve", "--repo-root", repo_root, "--dir", group.dir,
            "--pattern", group.pattern, "--gowork", group.gowork or "off",
            "--modfile", group.modfile or "", "--goos", build.goos, "--goarch", build.goarch,
            "--tags", ",".join(build.tags), "--cgo", "1" if build.cgo else "0",
            "--build-id", build.id, "--main-modules", ",".join(group.modules),
            "--peer-dirs", ",".join(peer_dirs)]


def _hash_file(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def _sites(record: dict):
    for fn in record.get("funcs", []):
        for site in fn.get("sites", []):
            yield site


# ---------------------------------------------------------------- the loads

@dataclass
class _Loads:
    """What the group loads answered, keyed by relpath and (relpath, offset)."""
    coverage: dict[str, tuple[str, str]] = field(default_factory=dict)   # rel -> (group, build)
    observed: dict[str, set] = field(default_factory=dict)              # rel -> sha256 every load saw
    sites: dict[tuple[str, int], dict] = field(default_factory=dict)
    conversions: set = field(default_factory=set)
    notcalls: set = field(default_factory=set)
    imports: dict[str, list[dict]] = field(default_factory=dict)
    embeds: dict[str, list[dict]] = field(default_factory=dict)
    implements: dict[str, list[dict]] = field(default_factory=dict)
    files_by_build: dict[str, set] = field(default_factory=dict)        # build -> files its loads compiled
    dirs_by_build: dict[str, set] = field(default_factory=dict)         # build -> dirs of those files
    implements_seen: dict = field(default_factory=dict)                   # pair key -> the kept record
    groups: dict[str, dict] = field(default_factory=dict)                 # artifact "groups"
    timings: dict[str, dict] = field(default_factory=dict)                # stats "groups"
    modcaches: list[str] = field(default_factory=list)
    gocaches: list[str] = field(default_factory=list)


def _run_loads(disc, *, scanned: dict[str, str], helper: str, runner: Runner, env_for,
               out_root: str, printer: Printer) -> _Loads:
    loads = _Loads()
    units = disc.unit_of_file
    peer_dirs = [os.path.join(p.root, u.root) if u.root else p.root
                 for p in disc.peers for u in p.units]
    builds = sorted(disc.builds, key=lambda b: not b.primary)
    files_of = {g.id: {rel for rel, key in units.items() if key in g.modules} for g in disc.groups}
    for g in disc.groups:
        reason = None
        if g.status == "error":
            reason = "load-error"
        loads.groups[g.id] = {"status": g.status, "reason": reason, "packages": 0, "errors": 0,
                              "builds": [], "message": "; ".join(g.warnings[:1])}
        loads.timings[g.id] = {}
    for build in builds:
        for g in disc.groups:
            info = loads.groups[g.id]
            if info["status"] != "ok":
                continue
            pending = files_of[g.id] - set(loads.coverage)
            if not build.primary and not pending:
                continue
            env = env_for(g, build)
            for key, bucket in (("GOMODCACHE", loads.modcaches), ("GOCACHE", loads.gocaches)):
                if env.get(key) and env[key] not in bucket:
                    bucket.append(env[key])
            out_path = os.path.join(out_root, "go", g.id, f"resolve-{build.id}.jsonl")
            run = runner(resolve_args(helper, disc.repo_root, g, build, peer_dirs), env, g.dir,
                         out_path)
            summary = next((r for r in reversed(run.records) if r.get("kind") == "summary"), None)
            fatal = [r for r in run.records if r.get("kind") == "error" and not r.get("pkg")]
            if (run.returncode != 0 or run.timed_out or summary is None
                    or (fatal and not summary.get("packages"))):
                text = "\n".join([run.stderr.strip()] + [r.get("msg", "") for r in fatal]).strip()
                info.update(status="error", reason=load_failure_reason(text),
                            message=(text.splitlines() or ["the helper ended without a summary"])[-1][:500])
                printer(f"[Phase 1.6] warning: group {g.id} ({build.id}) failed to load: "
                        f"{info['message']}; its sites are unresolved ({info['reason']})")
                continue
            info["packages"] = max(info["packages"], int(summary.get("packages") or 0))
            info["errors"] = max(info["errors"], int(summary.get("errors") or 0))
            info["builds"].append(build.id)
            loads.timings[g.id][build.id] = {
                "load_seconds": summary.get("load_seconds"), "seconds": round(run.seconds, 3),
                "peak_rss_bytes": summary.get("peak_rss_bytes"), "sites": summary.get("sites"),
                "conversions": summary.get("conversions"), "notcalls": summary.get("notcalls")}
            fresh: set[str] = set()
            for r in run.records:
                if r.get("kind") == "file":
                    rel = scanned.get(os.path.normpath(r.get("path") or ""))
                    if rel is None:
                        continue
                    loads.observed.setdefault(rel, set()).add(r.get("sha256") or "")
                    loads.files_by_build.setdefault(build.id, set()).add(rel)
                    loads.dirs_by_build.setdefault(build.id, set()).add(os.path.dirname(rel))
                    if rel in pending:
                        loads.coverage[rel] = (g.id, build.id)
                        fresh.add(rel)
            for r in run.records:
                kind = r.get("kind")
                if kind in ("site", "conversion", "notcall", "import"):
                    rel = scanned.get(os.path.normpath(r.get("path") or ""))
                    if rel not in fresh:
                        continue
                    key = (rel, r.get("offset"))
                    if kind == "site":
                        loads.sites[key] = r
                    elif kind == "conversion":
                        loads.conversions.add(key)
                    elif kind == "notcall":
                        loads.notcalls.add(key)
                    else:
                        loads.imports.setdefault(rel, []).append(r)
                elif kind == "embed":
                    rel = scanned.get(os.path.normpath((r.get("type") or {}).get("path") or ""))
                    if rel in fresh:
                        loads.embeds.setdefault(rel, []).append(r)
                elif kind == "implements" and build.primary:
                    # Pairs of the primary build (spec §5.8), from every group's load:
                    # a replaced in-repo module's type is only paired with the
                    # interfaces of the modules that load it.
                    rel = scanned.get(os.path.normpath((r.get("type") or {}).get("path") or ""))
                    key = json.dumps([r.get("type"), r.get("iface"), r.get("pointer_receiver")], sort_keys=True)
                    kept = loads.implements_seen.get(key)
                    if rel is not None and kept is None:
                        loads.implements_seen[key] = r
                        loads.implements.setdefault(rel, []).append(r)
                    elif kept is not None and r.get("incomplete"):
                        kept["incomplete"] = True       # the most conservative load wins (R43)
    return loads


# ---------------------------------------------------------------- imports, embeds, implements

class _Builds:
    """Which scanned files a build configuration compiles: the files its loads
    reported; for a directory no load of that build reported, the primary
    build's build_included (extract) or go_units.match_file."""

    def __init__(self, entities: dict[str, dict], loads: "_Loads", repo_root: str) -> None:
        self.entities, self.loads, self.root = entities, loads, repo_root
        self.by_dir: dict[str, list[str]] = {}
        for rel in sorted(entities):
            self.by_dir.setdefault(os.path.dirname(rel), []).append(rel)
        self._heads: dict[str, bytes] = {}

    def compiles(self, rel: str, build) -> bool:
        if os.path.dirname(rel) in self.loads.dirs_by_build.get(build.id, ()):
            return rel in self.loads.files_by_build[build.id]
        if build.primary:
            return bool(self.entities[rel].get("build_included", True))
        from .go_units import match_file
        if rel not in self._heads:
            try:
                with open(os.path.join(self.root, rel), "rb") as f:
                    self._heads[rel] = f.read()
            except OSError:
                self._heads[rel] = b""
        return bool(self._heads[rel]) and match_file(rel, self._heads[rel], build)

    def clause(self, rel_dir: str, build) -> tuple[str | None, str | None]:
        """(clause, None) or (None, reason) of the package in rel_dir under
        *build*: the unique clause of its compiled non-xtest files (C5)."""
        files = [r for r in self.by_dir.get(rel_dir, []) if build is not None and self.compiles(r, build)]
        found = {self.entities[r].get("package_name") or "" for r in files
                 if self.entities[r].get("origin") != "xtest"}
        if len(found) == 1:
            return next(iter(found)), None
        return None, ("no-declaration" if found else "excluded-by-build")


_CLAUSE_RE = re.compile(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def _peer_clause(directory: str, build, cache: dict) -> str | None:
    """The clause of a peer package directory: that of its build-included
    non-test files (go_units.match_file), when they agree."""
    key = (directory, build.id if build is not None else None)
    if key in cache:
        return cache[key]
    from .go_units import match_file
    clauses = set()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".go") or name.endswith("_test.go") or name.startswith(("_", ".")):
            continue
        try:
            with open(os.path.join(directory, name), "rb") as f:
                head = f.read()
        except OSError:
            continue
        if build is not None and not match_file(name, head, build):
            continue
        text = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", " ", head.decode("utf-8", "replace"), flags=re.S))
        m = _CLAUSE_RE.search(text)
        if m:
            clauses.add(m.group(1))
    cache[key] = next(iter(clauses)) if len(clauses) == 1 else None
    return cache[key]


def _import_record(imp: dict, ctx: ClassifyContext, builds: _Builds, build,
                   peer_cache: dict) -> dict:
    """One `imports[]` record (C5 rulings 2026-10-14); *build* is the build
    configuration that answered the importing file."""
    out = {"line": imp.get("line"), "col": imp.get("col"), "import_path": imp.get("import_path"),
           "status": "unresolved", "target": None, "external": None, "reason": None}
    path = imp.get("import_path") or ""
    if imp.get("error"):
        out["reason"] = "missing-module"
        return out
    if path == "C" or imp.get("module") == "C":
        out.update(status="external", external={"module": "C", "version": "", "package": path, "peer": False})
        return out
    if imp.get("std"):
        out.update(status="external", external={"module": "std", "version": "", "package": path, "peer": False})
        return out
    target_dir = os.path.normpath(imp.get("target_dir") or "") if imp.get("target_dir") else ""
    loc = ctx.locator.locate(os.path.join(target_dir, "_.go")) if target_dir else None
    if loc is None:
        out["reason"] = "load-error"
    elif loc.kind in ("repo", "unscanned"):
        rel_dir = os.path.relpath(target_dir, ctx.locator.repo_root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        clause, reason = builds.clause(rel_dir, build)
        if clause is not None:
            out.update(status="resolved",
                       target={"unit": imp.get("module") or "", "package": path, "dir": rel_dir,
                               "clause": clause})
        else:
            out["reason"] = reason
    elif loc.kind == "peer":
        out.update(status="external",
                   external={"module": loc.module, "version": "", "package": path, "peer": True},
                   target={"repo": loc.peer, "unit": loc.module, "package": path,
                           "dir": os.path.dirname(loc.relpath or ""),
                           "clause": _peer_clause(target_dir, build, peer_cache)})
    elif loc.kind in ("modcache", "vendor", "std", "cgo"):
        out.update(status="external",
                   external={"module": imp.get("module") or loc.module or "",
                             "version": imp.get("version") or loc.version or "", "package": path,
                             "peer": False})
    else:
        out["reason"] = "outside-repo"
    return out


def _type_ref(ref: dict | None, ctx: ClassifyContext, kinds: tuple[str, ...]) -> dict | None:
    """{"relpath","qualname"} of an in-repo type declaration of one of *kinds*."""
    if not ref:
        return None
    loc = ctx.locator.locate(ref.get("path") or "")
    if loc.kind != "repo":
        return None
    node = ctx.decls.at(loc.relpath, ref.get("line", 0), ref.get("col", 0), "class")
    if node is None or node.member_kind not in kinds:
        return None
    return {"relpath": node.relpath, "qualname": node.qualname}


def _type_end(ref: dict | None, ctx: ClassifyContext,
              kinds: tuple[str, ...] | None = None) -> tuple[str, dict | None, dict | None]:
    """(status, C5 class target or peer target, external) of the other end of
    an embed or implements record."""
    if not ref:
        return "unresolved", None, None
    loc = ctx.locator.locate(ref.get("path") or "")
    name = ref.get("qualname") or ref.get("name") or ""
    if loc.kind == "repo":
        node = ctx.decls.at(loc.relpath, ref.get("line", 0), ref.get("col", 0), "class")
        if node is None or (kinds is not None and node.member_kind not in kinds):
            return "unresolved", None, None
        return "resolved", node.target(), None
    if loc.kind == "peer":
        tgt = {"repo": loc.peer, "unit": loc.module, "package": ref.get("pkg") or "",
               "relpath": loc.relpath, "qualname": name, "line": ref.get("line", 0), "kind": "class"}
        return "external", tgt, {"module": loc.module, "version": "", "package": ref.get("pkg") or "",
                                 "qualname": name, "peer": True}
    if loc.kind in ("builtin", "std", "modcache", "vendor", "cgo"):
        module = {"builtin": "std", "std": "std", "cgo": "C"}.get(loc.kind) or loc.module or ref.get("module") or ""
        version = (loc.version or ref.get("version") or "") if loc.kind in ("modcache", "vendor") else ""
        return "external", None, {"module": module, "version": version, "package": ref.get("pkg") or "",
                                  "qualname": name, "peer": False}
    return "unresolved", None, None


def _embed_record(r: dict, ctx: ClassifyContext) -> dict | None:
    """One `embeds[]` record: an embedded field of an in-repo struct or interface."""
    owner = _type_ref(r.get("type"), ctx, ("struct", "interface", "named", "alias"))
    if owner is None:
        return None
    status, embedded, external = _type_end(r.get("embedded"), ctx)
    return {"type": owner, "embedded": embedded, "status": status, "external": external,
            "pointer": bool(r.get("pointer")), "reason": None}


def _implements_record(r: dict, ctx: ClassifyContext) -> dict | None:
    """One `implements[]` record: an in-repo concrete type and an interface.
    A pair the helper marks incomplete (G3, G4, R43: a method of the pair may
    be missing or shadowed) is unresolved (incomplete-type), so no edge."""
    concrete = _type_ref(r.get("type"), ctx, ("struct", "named"))
    if concrete is None:
        return None
    status, iface, external = _type_end(r.get("iface"), ctx, ("interface",))
    if iface is None:
        return None
    reason = None
    if r.get("incomplete"):
        status, external, reason = "unresolved", None, "incomplete-type"
    return {"type": concrete, "iface": iface, "status": status, "external": external,
            "pointer_receiver": bool(r.get("pointer_receiver")), "reason": reason}


def _broken_dirs(entities: dict[str, dict], loads: _Loads, builds: _Builds, configs) -> frozenset:
    """Directories with a file that has syntax errors and that some build
    compiles: an ignored generator does not count, a _test.go file does (a
    test-only method can shadow a promotion in the test variant)."""
    return frozenset(os.path.dirname(rel) for rel, rec in entities.items()
                     if rec.get("syntax_errors")
                     and (rel in loads.coverage or any(builds.compiles(rel, b) for b in configs)))


# ---------------------------------------------------------------- the run

def _stats(files: dict[str, dict], counts: Counter, timings: dict, elapsed: float) -> dict:
    by_status: Counter = Counter()
    by_via: Counter = Counter()
    by_reason: Counter = Counter()
    external: Counter = Counter()
    for entry in files.values():
        for rec in entry["calls"]:
            by_status[rec["status"]] += 1
            by_via[rec["via"]] += 1
            if rec["reason"]:
                by_reason[rec["reason"]] += 1
            if rec["status"] == "external" and rec["external"]:
                external[rec["external"]["module"]] += 1
    return {"sites_total": sum(by_status.values()), "by_status": dict(sorted(by_status.items())),
            "by_via": dict(sorted(by_via.items())), "by_reason": dict(sorted(by_reason.items())),
            "by_class": dict(sorted(counts["class"].items())) if "class" in counts else {},
            "external_by_module": dict(sorted(external.items())),
            "conversions": counts["conversions"], "notcalls": counts["notcalls"],
            "range_dropped": counts["range_dropped"], "join_misses": counts["join_misses"],
            "stale": counts["stale"], "groups": timings, "seconds": round(elapsed, 3)}


def run_resolution(entities: dict[str, dict], disc, *, file_hashes: dict[str, str], helper: str,
                   runner: Runner, env_for, goroot: str, out_root: str, engine: dict,
                   printer: Printer = print) -> dict:
    """The resolutions.json artifact (spec §5.8) for this repo.

    Raises StaleEntitiesError before the first load when an entity's sha256
    differs from its file on disk."""
    t0 = time.perf_counter()
    root = disc.repo_root
    paths = {rel: os.path.join(root, rel) for rel in file_hashes}
    stale = sorted(rel for rel in entities if _hash_file(paths.get(rel, os.path.join(root, rel)))
                   != entities[rel].get("sha256"))
    if stale:
        raise StaleEntitiesError(stale)
    scanned = {os.path.normpath(os.path.realpath(p)): rel for rel, p in paths.items()}
    loads = _run_loads(disc, scanned=scanned, helper=helper, runner=runner, env_for=env_for,
                       out_root=out_root, printer=printer)
    ctx = ClassifyContext(
        decls=DeclIndex(entities),
        locator=Locator(repo_root=root, scanned=scanned, goroot=goroot, modcaches=loads.modcaches,
                        gocache=loads.gocaches[0] if loads.gocaches else None, peers=disc.peers))
    group_of = {key: g.id for g in disc.groups for key in g.modules}
    builds = _Builds(entities, loads, root)
    by_id = {b.id: b for b in disc.builds}
    peer_cache: dict = {}
    broken = _broken_dirs(entities, loads, builds, disc.builds)
    counts: Counter = Counter()
    counts["class"] = Counter()
    files: dict[str, dict] = {}
    for rel in sorted(entities):
        rec = entities[rel]
        unit = disc.unit_of_file.get(rel)
        ginfo = loads.groups.get(group_of.get(unit, ""), {})
        cover = loads.coverage.get(rel)
        errors = rec.get("syntax_errors") or []
        counts["conversions"] += len(rec.get("conversions") or [])  # by syntax, no site (R53)
        calls = []
        for site in sorted(_sites(rec), key=lambda s: s["offset"]):
            key = (rel, site["offset"])
            sc = dict(line=site["line"], syntax_errors=errors, relpath=rel, broken_dirs=broken)
            helper_rec = loads.sites.get(key) if cover else None
            if cover and key in loads.conversions:
                counts["conversions"] += 1
                continue
            if cover and key in loads.notcalls:
                counts["notcalls"] += 1
                continue
            if helper_rec is None:
                if site["kind"] == "range":
                    counts["range_dropped"] += 1
                    continue
                if cover:
                    counts["join_misses"] += 1
                    reason = "load-error"
                else:
                    reason = no_answer_reason(unit=unit, group_status=ginfo.get("status"),
                                              group_reason=ginfo.get("reason"),
                                              build_included=bool(rec.get("build_included", True)))
                record = apply_gates(no_answer_record(cover[1] if cover else None),
                                     SiteContext(no_answer=reason, **sc))
            else:
                counts["class"][helper_rec.get("class") or "unknown"] += 1
                record = apply_gates(classify_site(helper_rec, ctx),
                                     SiteContext(helper=helper_rec, ctx=ctx, **sc))
            calls.append({"site": {"caller": site.get("caller"), "kind": site["kind"],
                                   "name": site["name"], "line": site["line"], "col": site["col"]},
                          **record})
        files[rel] = {
            "sha256": rec.get("sha256"), "calls": calls,
            "imports": [_import_record(imp, ctx, builds, by_id.get(cover[1]) if cover else None, peer_cache)
                        for imp in sorted(loads.imports.get(rel, []), key=lambda i: i.get("offset", 0))],
            "embeds": [e for e in (_embed_record(r, ctx) for r in loads.embeds.get(rel, [])) if e],
            "implements": [i for i in (_implements_record(r, ctx) for r in loads.implements.get(rel, []))
                           if i],
        }
    # files[rel].sha256 is the scan's hash only if every load saw those bytes
    # and they are still on disk; otherwise the hash seen instead (C5 ruling).
    changed: dict[str, str] = {}
    for rel in files:
        expected = entities[rel].get("sha256")
        other = sorted(sha for sha in loads.observed.get(rel, ()) if sha != expected)
        disk = _hash_file(paths.get(rel, os.path.join(root, rel)))
        if other:
            changed[rel] = other[0]
        elif disk != expected:
            changed[rel] = disk or "deleted"
    for rel, sha in changed.items():
        files[rel]["sha256"] = sha
    counts["stale"] = mark_stale(files, set(changed))
    if changed:
        printer(f"[Phase 1.6] {len(changed)} file(s) changed during the run; their sites and the "
                f"sites pointing into them are unresolved (stale)")
    return {"version": RESOLUTIONS_VERSION, "engine": engine, "repo": disc.repo,
            "groups": loads.groups, "files": files,
            "stats": _stats(files, counts, loads.timings, time.perf_counter() - t0)}


# ---------------------------------------------------------------- the phase

def helper_version(helper: str) -> dict:
    """`gohelper version` (C2): protocol, go, x_tools, revision."""
    proc = subprocess.run([helper, "version"], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin"})
    try:
        return json.loads(proc.stdout.splitlines()[0])
    except (ValueError, IndexError):
        return {}


def resolve_phase(data, *, out_root: str, output_dir: str, printer: Printer = print) -> dict:
    """Phase 1.6 on PipelineData (spec §5): fresh entities, control dirs, module
    download in download mode, the group loads, resolutions.json."""
    from . import go_modcache, go_toolchain, go_units
    t0 = time.perf_counter()
    if data.toolchain is None:
        data.toolchain = go_toolchain.find_toolchain()
    tc = data.toolchain
    refresh_entities(data, out_root=out_root, output_dir=output_dir, printer=printer)
    disc = data.discovery
    go_units.prepare_control_dirs(disc, output_dir)
    if disc.mode == "download":
        known = len(disc.warnings)
        report = go_modcache.download(disc, tc, output_dir)
        printer(f"[Phase 1.6] Downloaded modules for {len(report)} group(s)")
        for warning in disc.warnings[known:]:   # added after units.json was written
            printer(f"[Phase 1.6] warning: {warning}")
    helper = go_toolchain.ensure_helper(tc, output_dir)
    version = helper_version(helper)
    user = go_modcache.user_modcache(tc) if disc.mode == "offline" else None
    engine = {"name": "go/types", "toolchain": tc.version, "x_tools": version.get("x_tools", ""),
              "helper": "sha256:" + version.get("revision", ""), "entities_version": 1,
              "mode": disc.mode, "builds": [b.id for b in disc.builds]}

    def env_for(group, build):
        return go_toolchain.env_for(group, build, mode=disc.mode, output_dir=output_dir,
                                    user_modcache=user, toolchain=tc)

    def runner(argv, env, cwd, out_path):
        return run_helper(argv, env, cwd, out_path, timeout=float(config.GO_HELPER_TIMEOUT))

    kwargs = dict(helper=helper, runner=runner, env_for=env_for, goroot=tc.goroot,
                  out_root=out_root, engine=engine, printer=printer)
    try:
        artifact = run_resolution(data.extracted_entities, disc, file_hashes=data.file_hashes, **kwargs)
    except StaleEntitiesError as exc:
        printer(f"[Phase 1.6] {exc}; extracting them again once")
        refresh_entities(data, out_root=out_root, output_dir=output_dir, printer=printer)
        artifact = run_resolution(data.extracted_entities, disc, file_hashes=data.file_hashes, **kwargs)
    write_json_atomic(artifact, os.path.join(out_root, "resolutions.json"))
    data.resolutions = artifact
    elapsed = time.perf_counter() - t0
    data.timings["phase1c_resolve"] = elapsed
    stats = artifact["stats"]
    groups = ", ".join(f"{gid} {g['status']}" for gid, g in artifact["groups"].items()) or "none"
    printer(f"[Phase 1.6] Resolved {stats['sites_total']} sites ({disc.mode}; groups: {groups}; "
            f"builds: {', '.join(engine['builds'])}) in {elapsed:.1f}s: "
            + ", ".join(f"{k} {v}" for k, v in stats["by_status"].items()))
    printer(f"[Phase 1.6] Dropped {stats['conversions']} conversions and "
            f"{stats['notcalls'] + stats['range_dropped']} range candidates that are not calls")
    return artifact
