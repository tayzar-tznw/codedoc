"""Builders for the evaluate and crossref tests: C5 resolution records, C6 rows,
and "perfect" runs made from a fixture manifest's expectations.

A perfect run answers every case exactly as its expectation says, so scoring
it must give 0 wrong; a test then plants one mistake and checks the verdict.
"""
from __future__ import annotations

import json
import posixpath
from pathlib import Path

from graph_generator import evaluate, pipeline

ROOT = Path(__file__).resolve().parents[1]
TEST_CODES = ROOT / "test_codes"


def manifest(fixture: str) -> dict:
    return json.loads((TEST_CODES / fixture / "ground_truth" / "manifest.json").read_text(encoding="utf-8"))


def target(relpath: str, qualname: str, *, kind: str = "method", unit: str = "example.com/fx",
           repo: str | None = None, line: int = 1) -> dict:
    """A C5 target object."""
    out = {"unit": unit, "package": unit, "relpath": relpath, "qualname": qualname,
           "line": line, "kind": kind}
    if repo is not None:
        out["repo"] = repo
    return out


def record(line: int, col: int, status: str, *, kind: str = "call", name: str = "f",
           caller: str = "main", target: dict | None = None, static: dict | None = None,
           candidates: list[dict] = (), external: dict | None = None, reason: str | None = None,
           open_world: bool = False, via: str | None = None) -> dict:
    """A C5 resolution record with every field present."""
    if via is None:
        via = "types" if status in ("resolved", "ambiguous") else "none"
    return {"site": {"caller": caller, "kind": kind, "name": name, "line": line, "col": col},
            "status": status, "via": via, "target": target, "static_target": static,
            "external": external, "candidates": [dict(c, test_only=False, via_embedded_iface=False)
                                                  for c in candidates],
            "open_world": open_world, "build_variants": [], "reason": reason,
            "build": "linux-amd64"}


def external(module: str, *, peer: bool = False, qualname: str = "", version: str = "") -> dict:
    return {"module": module, "version": version, "package": module, "qualname": qualname,
            "peer": peer}


def import_record(line: int, col: int, import_path: str, *, status: str = "resolved",
                  directory: str = "", clause: str | None = None, name: str | None = None,
                  repo: str | None = None, external: dict | None = None) -> dict:
    """A C5 imports record. A resolved or peer target names its package by
    `clause` (C5); `name` is what helper output carried before the fix."""
    tgt = None
    if status == "resolved" or repo is not None:
        tgt = {"unit": "example.com/fx", "package": import_path, "dir": directory}
        for key, value in (("clause", clause), ("name", name), ("repo", repo)):
            if value is not None:
                tgt[key] = value
    return {"line": line, "col": col, "import_path": import_path, "status": status, "target": tgt,
            "external": external, "reason": None}


def resolutions(files: dict[str, list[dict]], repo: str = "fx", groups: dict | None = None,
                imports: dict[str, list[dict]] | None = None) -> dict:
    """A resolutions.json artifact: relpath -> call records (and import records)."""
    imports = imports or {}
    return {"version": 1, "repo": repo, "groups": groups or {},
            "files": {rel: {"sha256": "x", "calls": files.get(rel, []), "imports": imports.get(rel, []),
                            "embeds": [], "implements": []} for rel in dict.fromkeys([*files, *imports])},
            "stats": {}}


def entities_from(files: dict[str, list[dict]]) -> dict[str, dict]:
    """C3 FileRecords whose sites are exactly the records' sites (one function
    per file), so the edge rate's denominator matches the records."""
    return {rel: {"types": [], "funcs": [{"qualname": "<sites>", "sites": [
        {**r["site"], "offset": i} for i, r in enumerate(recs)]}]} for rel, recs in files.items()}


def run(name: str = "fx", directory: str = ".", *, files: dict[str, list[dict]] | None = None,
        nodes: dict | None = None, edges: dict | None = None, entities: dict | None = None,
        discovery=None, scanned: list[str] = (), groups: dict | None = None,
        imports: dict[str, list[dict]] | None = None) -> evaluate.RepoRun:
    """A RepoRun; without `entities` its sites are the call records' sites, and
    without `groups` one group that loaded ok."""
    files = files or {}
    groups = {"m0": {"status": "ok"}} if groups is None else groups
    return evaluate.RepoRun(name=name, dir=directory,
                            entities=entities_from(files) if entities is None else entities,
                            resolutions=resolutions(files, name, groups, imports),
                            nodes=nodes or {}, edges=edges or {}, discovery=discovery,
                            scanned=list(scanned))


# ═══════════════════════════════════════════════════════ perfect runs from a manifest


def _split(file: str, dirs: dict[str, str]) -> tuple[str, str] | None:
    """(repo, repo-relative path) of a fixture-relative file, longest repo dir first."""
    for repo, directory in sorted(dirs.items(), key=lambda kv: -len(kv[1])):
        if directory == ".":
            return repo, file
        if file.startswith(directory + "/"):
            return repo, file[len(directory) + 1:]
    return None


def _target_of(expected: dict, dirs: dict[str, str], *, peer: bool = False) -> dict:
    repo, rel = _split(expected["file"], dirs)
    return target(rel, expected["qualname"], kind=expected["kind"], unit=expected["module"],
                  repo=repo if peer else None, line=expected.get("line", 1))


def perfect_record(case: dict, expect: dict, dirs: dict[str, str]) -> dict | None:
    """The record that answers `expect` exactly; None for a conversion (no record)."""
    status = expect["status"]
    line, col, kind = case["line"], case["col"], case["site_kind"]
    if status == "conversion":
        return None
    cands = [_target_of(c, dirs) for c in expect.get("candidates") or []]
    if status == "resolved":
        static = expect.get("static_target")
        return record(line, col, "resolved", kind=kind, target=_target_of(expect["targets"][0], dirs),
                      static=_target_of(static, dirs) if static else None, candidates=cands,
                      open_world=bool(expect.get("open_world")))
    if status == "external":
        module = expect["external"]["module"]
        targets = expect.get("targets") or []
        tgt = _target_of(targets[0], dirs, peer=True) if targets else None
        return record(line, col, "external", kind=kind, target=tgt,
                      external=external(module, peer=bool(targets)))
    if status == "ambiguous":
        return record(line, col, "ambiguous", kind=kind, candidates=cands,
                      reason=expect.get("reason"))
    return record(line, col, status, kind=kind, reason=expect.get("reason"))


def perfect_runs(fixture: str, *, offline: bool = False) -> tuple[dict, dict[str, evaluate.RepoRun]]:
    """(manifest, repo -> RepoRun) whose records answer every case as expected."""
    m = manifest(fixture)
    dirs = evaluate.repo_dirs(m)
    files: dict[str, dict[str, list[dict]]] = {name: {} for name in dirs}
    for case in m["cases"]:
        expect = evaluate.case_expect(case, "offline" if offline else "env")
        rec = perfect_record(case, expect, dirs)
        if rec is None:
            continue
        repo, rel = _split(case["file"], dirs)
        files[repo].setdefault(rel, []).append(rec)
    return m, {name: run(name, dirs[name], files=files[name]) for name in dirs}


# ═══════════════════════════════════════════════════════ C6 rows


def file_row(repo: str, relpath: str) -> dict:
    return {"file_id": pipeline._make_id("file", repo, relpath, ""), "path": relpath, "repo": repo}


def class_row(repo: str, relpath: str, qualname: str, *, module: str, package: str,
              kind: str = "struct") -> dict:
    return {"class_id": pipeline._make_id("class", repo, relpath, qualname), "qualname": qualname,
            "name": qualname, "fqcn": f"{package}.{qualname}", "kind": kind, "module": module,
            "package": package, "file_id": pipeline._make_id("file", repo, relpath, ""), "repo": repo}


def method_row(repo: str, relpath: str, qualname: str, *, module: str, package: str,
               owner: str = "<package>", member_kind: str = "func") -> dict:
    return {"method_id": pipeline._make_id("method", repo, relpath, qualname), "qualname": qualname,
            "name": qualname.split(".")[-1], "fqmn": f"{package}.{qualname}",
            "member_kind": member_kind, "module": module,
            "class_id": pipeline._make_id("class", repo, relpath, owner),
            "file_id": pipeline._make_id("file", repo, relpath, ""), "repo": repo}


def package_row(repo: str, directory: str, clause: str) -> dict:
    return {"package_id": pipeline._make_id("package", repo, directory, clause), "name": clause,
            "directory": directory, "repo": repo}


def entity(package: str, clause: str, *, origin: str = "package", included: bool = True) -> dict:
    """The C3 FileRecord fields that decide which package an import binds."""
    return {"package": package, "package_name": clause, "origin": origin, "build_included": included,
            "types": [], "funcs": []}


def edge(table: str, source: str, target: str, **extra) -> dict:
    cols = {"MethodCalls": ("caller_method", "callee_method"),
            "PossiblyCalls": ("caller_method", "callee_method"),
            "PackageImports": ("source_file", "target_package"),
            "ClassEmbeds": ("child_class", "embedded_class"),
            "ClassImplements": ("class_id", "interface_id")}[table]
    return {"edge_id": pipeline._edge_id(table, source, target), cols[0]: source, cols[1]: target,
            **extra}


def join(directory: str, rel: str) -> str:
    return posixpath.normpath(posixpath.join(directory, rel))
