"""No-GCP, no-network regression gate for codedoc_go (spec §9.5).

For each fixture under test_codes/ and each mode, `evaluate`:

1. recreates OUTPUT_DIR/eval_<fixture>[_env] (spec §2.6 rule 5); its go/
   directory is the run's isolated module cache;
2. ingests every repo of the fixture in-process through Phases 1, 1.5 and
   1.6 plus the node and edge builders, with the build pinned to
   linux/amd64 without cgo or tags and no Spanner. go_layouts analyzes each
   layout directory as its own repo; in multi_repo every repo is a peer of
   the others and crossref runs on the results;
3. gives each manifest case one verdict (correct, documented alternate,
   wrong, unanswered) and checks the gates of spec §9.5 with amendment R50:
   the loads (`load_check`: groups ok on iryo-kanri-system and multi_repo,
   no join misses, every entity site with its record), the imports
   (`imports_check`, on iryo-kanri-system and multi_repo), and goracle
   (`oracle_check`: every record against goracle's site table; on
   iryo-kanri-system and multi_repo every resolved record judged over the
   observations of the fixture's entries, 0 WRONG-*). An empty denominator
   fails.

Offline mode (default): an empty isolated module cache, GOPROXY=off and no
user cache. Env mode (`--env`): download mode from the committed proxy
GOPROXY=file:///<abs>/test_codes/_proxy with GO_SUMDB=off (spec R22). Neither
needs the network or GCP.

The scorer and the metrics are pure functions of `RepoRun`s (entities,
resolutions, node and edge rows, discovery); `evaluate_fixture` takes an
`ingest` callable so they can be checked without a Go toolchain.
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import shutil
import stat
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import config, pipeline

FIXTURES: tuple[str, ...] = ("iryo-kanri-system", "go_layouts", "multi_repo")
METRICS: dict[str, tuple[str, ...]] = {        # gated metrics besides 0 wrong + completeness
    "iryo-kanri-system": ("node_coverage", "edge_rate", "qa", "imports", "judge", "class_edges"),
    "go_layouts": ("discovery",),
    "multi_repo": ("cross_repo", "imports", "judge"),
}
VERDICTS: tuple[str, ...] = ("correct", "alternate", "wrong", "unanswered")
ROW_TABLES: tuple[str, ...] = ("PackageImports", "ClassEmbeds", "ClassImplements")   # counted per report
PINNED_BUILD: dict[str, Any] = {"GO_BUILD_GOOS": "linux", "GO_BUILD_GOARCH": "amd64",
                                "GO_BUILD_TAGS": "", "GO_BUILD_CGO": False, "GO_EXTRA_BUILDS": ""}
_TARGET_SLOTS = ("target", "static_target")


@dataclass
class RepoRun:
    """One ingested repo of a fixture."""
    name: str
    dir: str                                   # fixture-relative directory ("." = the fixture)
    entities: dict[str, dict]                  # relpath -> C3 FileRecord
    resolutions: dict                          # resolutions.json (C5)
    nodes: dict[str, list[dict]] = field(default_factory=dict)   # build_node_rows
    edges: dict[str, list[dict]] = field(default_factory=dict)   # derive_edge_rows
    discovery: Any = None                      # go_model.Discovery
    scanned: list[str] = field(default_factory=list)             # relpaths Phase 1 scanned
    timings: dict[str, float] = field(default_factory=dict)      # seconds per phase


# ═══════════════════════════════════════════════════════════════════
# Records at a case's site
# ═══════════════════════════════════════════════════════════════════


def _join(directory: str, rel: str) -> str:
    return posixpath.normpath(posixpath.join(directory or ".", rel))


def repo_dirs(manifest: dict) -> dict[str, str]:
    """Repo name -> fixture-relative directory ('.' for a single-repo fixture)."""
    return {r["name"]: posixpath.normpath(r.get("dir") or ".") for r in manifest["repos"]}


def _located(tgt: dict, repo: str, dirs: dict[str, str]) -> dict:
    """A target with its fixture-relative "file" (a peer target lives under its own repo)."""
    directory = dirs.get(tgt.get("repo") or repo)
    return {**tgt, "file": _join(directory, tgt["relpath"]) if directory is not None else None}


def fixture_records(runs: dict[str, RepoRun]) -> dict[str, list[dict]]:
    """Every call record of every repo, keyed by fixture-relative file. Records
    are copies; each target, static target and candidate gains its "file"."""
    dirs = {name: r.dir for name, r in runs.items()}
    out: dict[str, list[dict]] = {}
    for name, r in runs.items():
        for rel, frec in (r.resolutions or {}).get("files", {}).items():
            recs = out.setdefault(_join(r.dir, rel), [])
            for rec in frec.get("calls", []):
                rec = dict(rec)
                for slot in _TARGET_SLOTS:
                    if rec.get(slot):
                        rec[slot] = _located(rec[slot], name, dirs)
                rec["candidates"] = [_located(c, name, dirs) for c in rec.get("candidates") or []]
                rec["_repo"] = name
                recs.append(rec)
    return out


def records_at(case: dict, records: dict[str, list[dict]]) -> list[dict]:
    """The records at the case's (file, line, column, site kind)."""
    want = (case["line"], case["col"], case["site_kind"])
    return [r for r in records.get(case["file"], [])
            if (r["site"]["line"], r["site"]["col"], r["site"]["kind"]) == want]


# ═══════════════════════════════════════════════════════════════════
# Verdicts
# ═══════════════════════════════════════════════════════════════════


def case_expect(case: dict, mode: str) -> dict:
    """The expectation a run is scored against: `expect_offline` for an
    offline run when the manifest gives one, else `expect` (master C10)."""
    if mode == "offline" and case.get("expect_offline"):
        return case["expect_offline"]
    return case["expect"]


def _key(t: dict) -> tuple:
    """Identity of a declaration: fixture-relative file, qualname, kind."""
    return (t.get("file"), t["qualname"], t.get("kind", "method"))


def _keys(targets) -> set[tuple]:
    return {_key(t) for t in targets or []}


def _named(rec: dict) -> list[dict]:
    """Every target a record names; build variants are informational and never count."""
    return [t for t in (rec.get("target"), rec.get("static_target")) if t] + list(rec.get("candidates") or [])


def _names_distractor(expect: dict, rec: dict) -> bool:
    return bool(_keys(expect.get("distractors")) & {_key(t) for t in _named(rec)})


def _is_peer(rec: dict) -> bool:
    return rec["status"] == "external" and bool((rec.get("target") or {}).get("repo"))


def _claims(rec: dict) -> list[dict]:
    """Targets a record turns into call edges: MethodCalls for an in-repo method
    target of a resolved record, CrossRepoCalls for a peer target (spec §6.3, §8.3)."""
    tgt = rec.get("target")
    if not tgt:
        return []
    if rec["status"] == "resolved" and not tgt.get("repo") and tgt.get("kind", "method") == "method":
        return [tgt]
    return [tgt] if _is_peer(rec) else []


def _possibly(rec: dict) -> list[dict]:
    """Candidates that become PossiblyCalls rows, as Phase 9 writes them (spec
    §5.4, §6.3): those of resolved, external and ambiguous records, none when
    a site has more than POSSIBLY_CALLS_MAX_CANDIDATES."""
    cands = list(rec.get("candidates") or [])
    if rec["status"] not in ("resolved", "external", "ambiguous") or not cands:
        return []
    return [] if len(_keys(cands)) > config.POSSIBLY_CALLS_MAX_CANDIDATES else cands


def interface_methods(runs: dict[str, RepoRun]) -> frozenset[tuple[str, str]]:
    """(fixture-relative file, qualname) of every interface method: C3 lists an
    interface's method specs under its type; concrete methods are funcs."""
    return frozenset((_join(r.dir, rel), m["qualname"]) for r in runs.values()
                     for rel, rec in (r.entities or {}).items()
                     for t in rec.get("types") or [] for m in t.get("methods") or [])


def _interface_case(expect: dict, iface) -> bool:
    """A resolved expectation whose target is an interface method (spec §5.4):
    the manifest gives open_world only for interface calls; the entities know
    the interface methods."""
    return expect["status"] == "resolved" and (
        expect.get("open_world") is not None
        or any((t.get("file"), t["qualname"]) in iface for t in expect.get("targets") or []))


def _acceptable(expect: dict, interface: bool = False) -> set[tuple]:
    """Call-edge claims a case allows: its targets and static target, and its
    runtime targets unless the target is an interface method (R50: an edge to
    an implementer is wrong there; implementers are PossiblyCalls)."""
    if expect["status"] not in ("resolved", "external"):
        return set()
    ok = _keys(expect.get("targets"))
    if not interface:
        ok |= _keys(expect.get("runtime_targets"))
    if expect.get("static_target"):
        ok.add(_key(expect["static_target"]))
    return ok


def _module(expect: dict) -> str | None:
    return (expect.get("external") or {}).get("module")


def _open_world_ok(expect: dict, recs: list[dict]) -> bool:
    return expect.get("open_world") is None or all(
        bool(r.get("open_world")) == bool(expect["open_world"]) for r in recs)


def _is_correct(expect: dict, recs: list[dict]) -> bool:
    want = expect["status"]
    if {r["status"] for r in recs} != {want}:
        return False
    targets = _keys(expect.get("targets"))
    if want == "resolved":
        if not all(r.get("target") for r in recs) or {_key(r["target"]) for r in recs} != targets:
            return False
        static = _key(expect["static_target"]) if expect.get("static_target") else None
        if any((_key(r["static_target"]) if r.get("static_target") else None) != static for r in recs):
            return False
        return _open_world_ok(expect, recs) and _candidates_ok(expect, recs)
    if want == "external":
        if any((r.get("external") or {}).get("module") != _module(expect) for r in recs):
            return False
        if not (_open_world_ok(expect, recs) and _candidates_ok(expect, recs)):
            return False
        if targets:
            return all(r.get("target") for r in recs) and {_key(r["target"]) for r in recs} == targets
        return not any(_is_peer(r) for r in recs)
    if want == "ambiguous":
        return _reason_ok(expect, recs) and _candidates_ok(expect, recs)
    if want == "unresolved":
        return _reason_ok(expect, recs)
    return want in ("dynamic", "builtin")


def _candidates_ok(expect: dict, recs: list[dict]) -> bool:
    """Subset rule: every returned candidate is an expected candidate."""
    allowed = _keys(expect.get("candidates"))
    return all(_key(c) in allowed for r in recs for c in r.get("candidates") or [])


def _reason_ok(expect: dict, recs: list[dict]) -> bool:
    return expect.get("reason") is None or all(r.get("reason") == expect["reason"] for r in recs)


def _is_alternate(expect: dict, recs: list[dict]) -> bool:
    """Documented alternates of a resolved case that is not an interface call:
    every record resolved to a runtime target or the static target, with the
    candidate rule kept and no other edge claim."""
    if expect["status"] != "resolved" or not _candidates_ok(expect, recs):
        return False
    alternates = _keys(expect.get("runtime_targets"))
    if expect.get("static_target"):
        alternates.add(_key(expect["static_target"]))
    ok = _acceptable(expect)
    return all(r["status"] == "resolved" and r.get("target") and _key(r["target"]) in alternates
               and all(_key(t) in ok for t in _claims(r)) for r in recs)


def _is_wrong(expect: dict, rec: dict, interface: bool = False) -> bool:
    """A definite answer that contradicts the expectation (spec §9.5 "Wrong",
    R50): an answer at a conversion, a call edge the case does not allow, a
    PossiblyCalls row outside the expected candidates (or where none are
    expected), an external answer where an in-repo target is expected, another
    module. Distractors are checked before (score_case)."""
    if expect["status"] == "conversion":
        return bool(_named(rec)) or (rec["status"] == "external"
                                     and bool((rec.get("external") or {}).get("qualname")))
    ok = _acceptable(expect, interface)
    if any(_key(t) not in ok for t in _claims(rec)):
        return True
    allowed = _keys(expect.get("candidates"))
    if any(_key(c) not in allowed for c in _possibly(rec)):
        return True
    if rec["status"] == "external":
        if expect["status"] == "resolved":
            return True
        if expect["status"] == "external":
            return (rec.get("external") or {}).get("module") != _module(expect)
    return False


def score_case(case: dict, records: dict[str, list[dict]], *, mode: str = "env",
               iface=frozenset()) -> str:
    """One verdict per case: correct | alternate | wrong | unanswered.

    `records` is `fixture_records(...)`; `iface` the interface methods
    (`interface_methods`). A record naming a distractor is wrong whatever else
    it says (R50). A `conversion` case is correct when the site has no record
    at all; any other case without a record is unanswered. An interface case
    has no alternates (spec §5.4).
    """
    expect = case_expect(case, mode)
    recs = records_at(case, records)
    if not recs:
        return "correct" if expect["status"] == "conversion" else "unanswered"
    if any(_names_distractor(expect, r) for r in recs):
        return "wrong"
    interface = _interface_case(expect, iface)
    if expect["status"] != "conversion" and _is_correct(expect, recs):
        return "correct"
    if not interface and _is_alternate(expect, recs):
        return "alternate"
    if any(_is_wrong(expect, r, interface) for r in recs):
        return "wrong"
    return "unanswered"


def _edge_counts(expect: dict, recs: list[dict], interface: bool = False) -> dict[str, int]:
    """Edge claims at one site for precision and recall: claims, good claims,
    expected targets, expected targets claimed."""
    want = _keys(expect.get("targets")) if expect["status"] in ("resolved", "external") else set()
    claims = [t for r in recs for t in _claims(r)]
    ok = _acceptable(expect, interface)
    return {"claims": len(claims), "good": sum(1 for t in claims if _key(t) in ok),
            "expected": len(want), "found": len({_key(t) for t in claims} & want)}


# ═══════════════════════════════════════════════════════════════════
# Metrics
# ═══════════════════════════════════════════════════════════════════


def completeness_min(n: int) -> int:
    """floor(0.85 * n) + 1, in integers."""
    return (85 * n) // 100 + 1


def meets_rate(num: int, den: int) -> bool:
    """num / den >= 0.85 without float rounding; an empty denominator fails (R50)."""
    return den > 0 and num * 100 >= 85 * den


def _rate(num: int, den: int) -> dict:
    return {"value": num / den if den else 0.0, "pass": meets_rate(num, den)}


def per_category(results: list[dict]) -> dict[str, dict]:
    """Per category: total, complete (correct + alternate) and sound (not wrong) counts."""
    out: dict[str, dict] = {}
    for r in results:
        row = out.setdefault(r["category"], {"total": 0, "complete": 0, "sound": 0})
        row["total"] += 1
        row["complete"] += r["verdict"] in ("correct", "alternate")
        row["sound"] += r["verdict"] != "wrong"
    return out


def edge_precision_recall(results: list[dict]) -> dict[str, float]:
    """Precision: edge claims that are an expected target or a documented
    alternate. Recall: expected targets that were claimed."""
    claims = sum(r["edges"]["claims"] for r in results)
    good = sum(r["edges"]["good"] for r in results)
    expected = sum(r["edges"]["expected"] for r in results)
    found = sum(r["edges"]["found"] for r in results)
    return {"precision": good / claims if claims else 1.0,
            "recall": found / expected if expected else 1.0}


def edge_rate(runs: dict[str, RepoRun]) -> tuple[int, int]:
    """(sites answered `resolved` or `external`, sites that are not `builtin`).
    The denominator comes from the entity sites minus the conversions, the
    notcalls and the dropped range candidates (resolutions.json stats), so a
    site that lost its record counts as unanswered (R50)."""
    answered = builtin = sites = 0
    for r in runs.values():
        res = r.resolutions or {}
        sites += _site_count(r)
        for frec in res.get("files", {}).values():
            for rec in frec.get("calls", []):
                builtin += rec["status"] == "builtin"
                answered += rec["status"] in ("resolved", "external")
    return answered, max(sites - builtin, 0)


def _site_count(r: RepoRun) -> int:
    """Entity sites that must have a call record: all of them but the
    conversions, notcalls and dropped range candidates (resolutions.json stats)."""
    stats = (r.resolutions or {}).get("stats") or {}
    sites = sum(len(fn.get("sites") or []) for rec in (r.entities or {}).values() for fn in rec.get("funcs") or [])
    # calls of a type literal are no sites but count with the conversions (R53)
    sites += sum(len(rec.get("conversions") or []) for rec in (r.entities or {}).values())
    return sites - sum(int(stats.get(k) or 0) for k in ("conversions", "notcalls", "range_dropped"))


def _off_site_records(name: str, r: RepoRun) -> list[str]:
    """Per file: every call record sits on an entity site of its kind, and no
    site has two records. With the repo's record count equal to its sites less
    the counted drops, every site that must have a record has exactly one."""
    out = []
    for rel, frec in sorted((r.resolutions or {}).get("files", {}).items()):
        sites = {(s["line"], s["col"], s["kind"]) for fn in ((r.entities or {}).get(rel) or {}).get("funcs") or []
                 for s in fn.get("sites") or []}
        seen: set[tuple] = set()
        for rec in frec.get("calls") or []:
            key = (rec["site"]["line"], rec["site"]["col"], rec["site"]["kind"])
            if key not in sites:
                out.append(f"{name}: {rel}:{key[0]}:{key[1]} {key[2]} record without an entity site")
            elif key in seen:
                out.append(f"{name}: {rel}:{key[0]}:{key[1]} {key[2]} has two records")
            seen.add(key)
    return out


def load_check(runs: dict[str, RepoRun], *, all_ok: bool) -> dict:
    """The loads (R50): every entity site joined a helper record (no
    `join_misses`) and has exactly its call record, at least one group
    loaded, and with `all_ok` (every fixture but go_layouts, whose discovery
    facts give each group's status) every group ended the load `ok`."""
    groups, not_ok, join_misses, mismatch = 0, [], 0, []
    for name, r in sorted(runs.items()):
        res = r.resolutions or {}
        join_misses += int((res.get("stats") or {}).get("join_misses") or 0)
        for gid, g in sorted((res.get("groups") or {}).items()):
            groups += 1
            if (g or {}).get("status") != "ok":
                reason = (g or {}).get("reason")
                not_ok.append(f"{name}:{gid} {(g or {}).get('status')}" + (f" ({reason})" if reason else ""))
        records = sum(len(f.get("calls") or []) for f in res.get("files", {}).values())
        if records != _site_count(r):
            mismatch.append(f"{name}: {records} call records for {_site_count(r)} entity sites")
        mismatch += _off_site_records(name, r)
    ok = groups > 0 and join_misses == 0 and not mismatch and not (all_ok and not_ok)
    return {"groups": groups, "not_ok": not_ok, "join_misses": join_misses, "site_mismatch": mismatch,
            "pass": ok}


def _import_clauses(entities: dict[str, dict]) -> tuple[dict, dict]:
    """The package clauses per (dir, import path): of the files the primary
    build compiles, and of all files. An xtest file has its own import path
    (`<path>_test`), so it never counts."""
    compiled: dict[tuple[str, str], set[str]] = {}
    every: dict[tuple[str, str], set[str]] = {}
    for rel, rec in (entities or {}).items():
        if rec.get("package_name") and rec.get("package"):
            key = (posixpath.dirname(rel), rec["package"])
            every.setdefault(key, set()).add(rec["package_name"])
            if rec.get("build_included"):
                compiled.setdefault(key, set()).add(rec["package_name"])
    return compiled, every


def imports_check(runs: dict[str, RepoRun], limit: int = 20) -> dict:
    """Every resolved in-repo import record against the PackageImports rows:
    each needs a row from its file to the package (dir, clause) it names. A
    record is exempt (ambiguous) when the import can bind more than one clause
    of the target directory: those of the files the primary build compiles
    (of all files when it compiles none), or those of all files for a file
    only an extra build compiles. Peer imports belong to crossref."""
    total = mapped = 0
    unmapped: list[str] = []
    ambiguous: list[str] = []
    for name, r in sorted(runs.items()):
        rows = {(row.get("source_file"), row.get("target_package")) for row in r.edges.get("PackageImports", [])}
        packages = {row.get("package_id") for row in r.nodes.get("Packages", [])}
        compiled, every = _import_clauses(r.entities)
        for rel, frec in sorted((r.resolutions or {}).get("files", {}).items()):
            source = pipeline._make_id("file", name, rel, "")
            included = bool((r.entities.get(rel) or {}).get("build_included", True))
            for rec in frec.get("imports") or []:
                tgt = rec.get("target") or {}
                if rec.get("status") != "resolved" or tgt.get("repo") or (rec.get("external") or {}).get("peer"):
                    continue
                total += 1
                directory, clause = tgt.get("dir"), tgt.get("clause")
                package = pipeline._make_id("package", name, directory, clause) if clause else None
                if package and (source, package) in rows:
                    mapped += 1
                    continue
                key = (directory, rec.get("import_path"))
                bindable = (compiled.get(key) or every.get(key) or set()) if included else every.get(key, set())
                where = f"{_join(r.dir, rel)}:{rec.get('line')}:{rec.get('col')} {rec.get('import_path')}"
                if len(bindable) > 1:
                    ambiguous.append(f"{where}: {directory} has clauses {', '.join(sorted(bindable))}")
                elif not clause:
                    unmapped.append(f"{where}: the record names no clause")
                elif package not in packages:
                    unmapped.append(f"{where}: no Packages node {directory}|{clause}")
                else:
                    unmapped.append(f"{where}: no PackageImports row to {directory}|{clause}")
    return {"records": total, "mapped": mapped, "unmapped": len(unmapped), "ambiguous": len(ambiguous),
            "unmapped_examples": unmapped[:limit], "ambiguous_examples": ambiguous[:limit],
            "pass": total > 0 and not unmapped}


def build_view(runs: dict[str, RepoRun]) -> dict:
    """An in-memory graph of the fixture from the builders' rows."""
    view: dict[str, Any] = {"files": {}, "classes": {}, "methods": {}, "files_by_path": {},
                            "classes_by_fq": {}, "methods_by_fq": {},
                            "calls": [], "possibly": [], "embeds": [], "implements": []}
    for r in runs.values():
        for row in r.nodes.get("Files", []):
            path = _join(r.dir, row["path"])
            view["files"][row["file_id"]] = path
            view["files_by_path"].setdefault(path, []).append(row["file_id"])
        for row in r.nodes.get("Classes", []):
            view["classes"][row["class_id"]] = row
            view["classes_by_fq"].setdefault(row["fqcn"], []).append(row["class_id"])
        for row in r.nodes.get("Methods", []):
            view["methods"][row["method_id"]] = row
            view["methods_by_fq"].setdefault(row["fqmn"], []).append(row["method_id"])
        for table, key, src, dst in (("MethodCalls", "calls", "caller_method", "callee_method"),
                                     ("PossiblyCalls", "possibly", "caller_method", "callee_method"),
                                     ("ClassEmbeds", "embeds", "child_class", "embedded_class"),
                                     ("ClassImplements", "implements", "class_id", "interface_id")):
            view[key] += [(row[src], row[dst]) for row in r.edges.get(table, [])]
    return view


def node_coverage(pins: list[dict], view: dict) -> tuple[int, int]:
    """(declaration pins that exist as nodes, pins). A pin matches kind,
    qualname, module and fixture-relative file."""
    have = {("method", m["qualname"], m.get("module"), view["files"].get(m["file_id"]))
            for m in view["methods"].values()}
    have |= {("class", c["qualname"], c.get("module"), view["files"].get(c["file_id"]))
             for c in view["classes"].values()}
    return sum(1 for p in pins if (p["kind"], p["qualname"], p["module"], p["file"]) in have), len(pins)


def answer_question(q: dict, view: dict) -> tuple[bool, str]:
    """Answer one qa_questions.json question from the rows. Subjects are
    `<import path>.<qualname>` (fqmn / fqcn)."""
    check = q.get("check", {})
    ctype, subject = check.get("type", ""), check.get("subject", "")
    methods, classes, files = view["methods"], view["classes"], view["files"]
    mids = set(view["methods_by_fq"].get(subject, []))
    cids = set(view["classes_by_fq"].get(subject, []))
    if ctype == "node_exists":
        found = bool(mids or cids or subject in view["files_by_path"])
        return _match(q, ["exists" if found else "missing"])
    if ctype == "class_file":
        actual = [files.get(classes[c]["file_id"]) for c in cids]
    elif ctype == "methods_of":
        actual = [m["name"] for m in methods.values()
                  if m["class_id"] in cids and m.get("member_kind") != "closure"]
    elif ctype == "callees_of":
        actual = [methods[t]["fqmn"] for s, t in view["calls"] if s in mids]
    elif ctype == "callers_of":
        actual = [methods[s]["fqmn"] for s, t in view["calls"] if t in mids]
    elif ctype == "possibly_callees_of":
        actual = [methods[t]["fqmn"] for s, t in view["possibly"] if s in mids]
    elif ctype == "cross_module_callees_of":
        actual = [methods[t]["fqmn"] for s, t in view["calls"]
                  if s in mids and methods[t].get("module") != methods[s].get("module")]
    elif ctype == "implementers_of":
        actual = [classes[s]["fqcn"] for s, t in view["implements"] if t in cids]
    elif ctype == "embeds_of":
        actual = [classes[t]["fqcn"] for s, t in view["embeds"] if s in cids]
    else:
        return False, f"(unknown check type {ctype})"
    return _match(q, sorted(set(a for a in actual if a)))


def _match(q: dict, actual: list[str]) -> tuple[bool, str]:
    expected = q.get("expected", [])
    match = q.get("match", "contains_all")
    if match == "count_at_least":
        need = expected if isinstance(expected, int) else (expected[0] if expected else 1)
        ok = len(actual) >= int(need)
    else:
        want = set(expected if isinstance(expected, list) else [expected])
        ok = set(actual) == want if match in ("exact_set", "equals") else want <= set(actual)
    return ok, ", ".join(actual[:8]) or "(empty)"


def qa_rate(fixture_dir: str, view: dict) -> dict:
    """QA rate over ground_truth/qa_questions.json; a missing file fails the gate."""
    path = os.path.join(fixture_dir, "ground_truth", "qa_questions.json")
    if not os.path.isfile(path):
        return {"value": 0.0, "pass": False, "misses": []}
    with open(path, "r", encoding="utf-8") as f:
        questions = json.load(f).get("questions", [])
    misses = []
    for q in questions:
        ok, actual = answer_question(q, view)
        if not ok:
            misses.append({"id": q.get("id"), "question": q.get("question"), "actual": actual})
    return {**_rate(len(questions) - len(misses), len(questions)), "misses": misses}


# ── F3: the exact cross-repo edge set (master C10 labels) ──────────────


def label_id(table: str, side: str, label: str) -> str:
    """The node ID of an F3 label: files "<repo>:<relpath>", packages
    "<repo>:<dir>|<clause>", classes and methods "<repo>:<relpath>:<qualname>"."""
    if table == "CrossRepoImports":
        repo, rest = label.split(":", 1)
        if side == "source":
            return pipeline._make_id("file", repo, rest, "")
        directory, clause = rest.rsplit("|", 1)
        return pipeline._make_id("package", repo, directory, clause)
    repo, rel, qualname = label.split(":", 2)
    return pipeline._make_id("method" if table == "CrossRepoCalls" else "class", repo, rel, qualname)


def node_labels(entities_by_repo: dict[str, dict]) -> dict[str, str]:
    """Node ID -> F3 label for every file, package, type and function of the repos."""
    out: dict[str, str] = {}
    for repo, entities in entities_by_repo.items():
        for rel, rec in entities.items():
            out[pipeline._make_id("file", repo, rel, "")] = f"{repo}:{rel}"
            if rec.get("package_name"):
                directory = posixpath.dirname(rel)
                out[pipeline._make_id("package", repo, directory, rec["package_name"])] = \
                    f"{repo}:{directory}|{rec['package_name']}"
            for t in rec.get("types", []):
                out[pipeline._make_id("class", repo, rel, t["qualname"])] = f"{repo}:{rel}:{t['qualname']}"
                for m in t.get("methods", []):
                    out[pipeline._make_id("method", repo, rel, m["qualname"])] = f"{repo}:{rel}:{m['qualname']}"
            for fn in rec.get("funcs", []):
                out[pipeline._make_id("method", repo, rel, fn["qualname"])] = f"{repo}:{rel}:{fn['qualname']}"
    return out


def _xrepo_key(table: str, kind: str | None, sid: str, did: str, import_path: str | None) -> tuple:
    return (table, kind if table == "CrossRepoRef" else None, sid, did,
            import_path if table == "CrossRepoImports" else None)


def _xrepo_label(key: tuple, labels: dict[str, str]) -> str:
    table, kind, sid, did, import_path = key
    head = f"{table}[{kind}]" if kind else table
    text = f"{head} {labels.get(sid, sid)} -> {labels.get(did, did)}"
    return f"{text} ({import_path})" if import_path else text


def cross_repo_check(expected: list[dict], derived: dict, labels: dict[str, str]) -> dict:
    """Compare crossref's rows with the manifest's cross_repo_edges as an exact set."""
    want: dict[tuple, str] = {}
    for e in expected:
        key = _xrepo_key(e["table"], e.get("kind"), label_id(e["table"], "source", e["source"]),
                         label_id(e["table"], "target", e["target"]), e.get("import_path"))
        head = f"{e['table']}[{e['kind']}]" if e["table"] == "CrossRepoRef" else e["table"]
        tail = f" ({e['import_path']})" if e["table"] == "CrossRepoImports" else ""
        want[key] = f"{head} {e['source']} -> {e['target']}{tail}"
    got: set[tuple] = set()
    for r in derived.get("CrossRepoCalls", []):
        got.add(_xrepo_key("CrossRepoCalls", None, r["caller_method"], r["callee_method"], None))
    for r in derived.get("CrossRepoImports", []):
        got.add(_xrepo_key("CrossRepoImports", None, r["source_file"], r["target_package"],
                           r.get("import_path")))
    for r in derived.get("CrossRepoRef", []):
        got.add(_xrepo_key("CrossRepoRef", r.get("kind"), r["source_class"], r["target_class"], None))
    return {"expected": len(want), "actual": len(got),
            "missing": sorted(want[k] for k in set(want) - got),
            "extra": sorted(_xrepo_label(k, labels) for k in got - set(want)),
            "pass": got == set(want)}


# ── F1: the exact ClassImplements and ClassEmbeds sets (R50 d) ─────────

CLASS_TABLES: dict[str, tuple[str, str, str]] = {          # table -> (source, target, flag) columns
    "ClassImplements": ("class_id", "interface_id", "pointer_receiver"),
    "ClassEmbeds": ("child_class", "embedded_class", "pointer"),
}
MODES: tuple[str, ...] = ("offline", "env")


def class_edge_check(expected: list[dict], runs: dict[str, RepoRun], labels: dict[str, str],
                     mode: str) -> dict:
    """The derived ClassImplements and ClassEmbeds rows against the manifest's
    class_edges of this mode (an entry without "modes" applies to both),
    compared as exact sets of (table, source, target, flag); endpoints are F3
    class labels "<repo>:<relpath>:<qualname>"."""
    def text(table: str, source: str, target: str, flag: bool) -> str:
        return f"{table} {source} -> {target} ({CLASS_TABLES[table][2]}={str(flag).lower()})"

    want: dict[tuple, str] = {}
    for e in expected:
        if mode not in e.get("modes", MODES):
            continue
        table, flag = e["table"], bool(e.get(CLASS_TABLES[e["table"]][2]))
        key = (table, label_id("CrossRepoRef", "source", e["source"]),
               label_id("CrossRepoRef", "target", e["target"]), flag)
        want[key] = text(table, e["source"], e["target"], flag)
    got = {(table, row[src], row[dst], bool(row.get(flag)))
           for r in runs.values() for table, (src, dst, flag) in CLASS_TABLES.items()
           for row in r.edges.get(table, [])}
    return {"expected": len(want), "actual": len(got),
            "missing": sorted(want[k] for k in set(want) - got),
            "extra": sorted(text(t, labels.get(s, s), labels.get(d, d), f) for t, s, d, f in got - set(want)),
            "pass": got == set(want)}


# ── F2: discovery facts against expected_units.json ────────────────────


def group_key(group, repo_root: str) -> str:
    """'<kind>:<dir>' of a group: the dir of its repo go.work (workspace) or
    go.mod (module) relative to the repo root, '.' for the root."""
    source = getattr(group, "source", None)
    base = os.path.dirname(source) if source else group.dir
    rel = os.path.relpath(base, repo_root).replace(os.sep, "/")
    return f"{group.kind}:{rel}"


def _rel_replace(r, repo_root: str) -> dict:
    new = os.path.relpath(r.new, repo_root).replace(os.sep, "/") if r.local else r.new
    return {"old": r.old, "old_version": r.old_version, "new": new, "new_version": r.new_version,
            "local": r.local}


def discovery_problems(expected: dict, disc, scanned: list[str], groups_after: dict,
                       mode: str) -> list[str]:
    """Differences between one layout's discovery and its expected_units.json
    entry. `groups_after` is resolutions.json "groups" (status after the load);
    `mode` is "offline" or "env"."""
    if disc is None:
        return ["no discovery (the pipeline did not run Phase 1)"]
    problems: list[str] = []
    root = disc.repo_root
    keys = {g.id: group_key(g, root) for g in disc.groups}

    def differ(what: str, got, want) -> None:
        if got != want:
            problems.append(f"{what}: {got}, expected {want}")

    units = {u.key: u for u in disc.units}
    for key in sorted(set(expected.get("units", {})) - set(units)):
        problems.append(f"unit {key}: missing")
    for key in sorted(set(units) - set(expected.get("units", {}))):
        problems.append(f"unit {key}: not expected")
    for key, want in sorted(expected.get("units", {}).items()):
        u = units.get(key)
        if u is None:
            continue
        group = disc.group_of(key)
        got = {"root": u.root, "go": u.go, "toolchain": u.toolchain, "vendor": u.vendor,
               "group": keys.get(group.id) if group else None,
               "requires": sorted([list(req) for req in u.requires]),
               "replaces": sorted((_rel_replace(r, root) for r in u.replaces), key=json.dumps),
               "in_repo_deps": sorted(u.in_repo_deps)}
        exp = {**want, "requires": sorted([list(req) for req in want.get("requires", [])]),
               "replaces": sorted(want.get("replaces", []), key=json.dumps),
               "in_repo_deps": sorted(want.get("in_repo_deps", []))}
        for field_name in ("root", "go", "toolchain", "vendor", "group", "requires", "replaces",
                           "in_repo_deps"):
            differ(f"unit {key} {field_name}", got[field_name], exp.get(field_name))

    groups = {keys[g.id]: g for g in disc.groups}
    for key in sorted(set(expected.get("groups", {})) - set(groups)):
        problems.append(f"group {key}: missing")
    for key in sorted(set(groups) - set(expected.get("groups", {}))):
        problems.append(f"group {key}: not expected")
    for key, want in sorted(expected.get("groups", {}).items()):
        g = groups.get(key)
        if g is None:
            continue
        for attr in ("kind", "vendor", "degraded", "status", "pattern"):
            differ(f"group {key} {attr}", getattr(g, attr), want.get(attr))
        differ(f"group {key} modules", sorted(g.modules), sorted(want.get("modules", [])))
        for part in want.get("warnings_contain", []):
            if not any(part in w for w in g.warnings):
                problems.append(f"group {key} warnings lack {part!r}")

    files = expected.get("files")
    if files is not None:
        for rel, owner in sorted(files.items()):
            got = disc.unit_of_file.get(rel)
            if got != owner:
                problems.append(f"file {rel}: owner {got or 'none'}, expected {owner or 'none'}")
        if sorted(scanned) != sorted(files):
            extra = sorted(set(scanned) - set(files))
            lost = sorted(set(files) - set(scanned))
            problems.append(f"scanned files differ: extra {extra}, missing {lost}")
    if "scanned_files" in expected:
        differ("scanned files", len(scanned), expected["scanned_files"])
    for rel in expected.get("not_scanned", []):
        if rel in scanned:
            problems.append(f"{rel}: scanned, expected not")
    if "skipped" in expected:
        differ("skipped", dict(sorted((k, v) for k, v in disc.skipped.items() if v)),
               dict(sorted(expected["skipped"].items())))
    for part in expected.get("warnings_contain", []):
        if not any(part in w for w in disc.warnings):
            problems.append(f"warnings lack {part!r}")

    facts = (expected.get("modes") or {}).get(mode) or {}
    if "module_source" in facts:
        differ("module source", disc.module_source, facts["module_source"])
    if "missing_modules" in facts:
        got_missing = {keys[g.id]: sorted(g.missing_modules) for g in disc.groups if g.missing_modules}
        for key in sorted(set(got_missing) | set(facts["missing_modules"])):
            differ(f"missing modules of {key}", got_missing.get(key, []),
                   sorted(facts["missing_modules"].get(key, [])))
    # each group keeps its discovery status after the load unless this mode's
    # status_after_load says otherwise (R50)
    by_key = {keys[gid]: (st or {}).get("status") for gid, st in (groups_after or {}).items() if gid in keys}
    overrides = facts.get("status_after_load") or {}
    for key in sorted(overrides):
        if key not in groups:
            problems.append(f"status after load of {key}: no such group")
    for key, g in sorted(groups.items()):
        differ(f"status after load of {key}", by_key.get(key), overrides.get(key, g.status))
    return problems


# ═══════════════════════════════════════════════════════════════════
# goracle: the site table and the runtime oracle (R50 6a-c)
# ═══════════════════════════════════════════════════════════════════

GT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_codes", "_gt")
ORACLE_SRC = os.path.join(GT_DIR, "oracle")
WRONG_VERDICTS: tuple[str, ...] = ("WRONG-OTHER", "WRONG-MULTI", "IFACE-OTHER", "DEFER-GROUP-OTHER")
# a record goracle cannot place on its table: a bug to look at, never neutral (A1)
UNPLACED_VERDICTS: tuple[str, ...] = ("NO-SITE", "NO-TARGET")


class OracleError(RuntimeError):
    """goracle could not be built, scan, observe or match."""


def build_goracle(toolchain, work_dir: str) -> str:
    """goracle (test_codes/_gt/oracle, standard library only) built offline
    into work_dir/bin/goracle."""
    from . import go_toolchain

    out = os.path.join(work_dir, "bin", "goracle")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    env = {**go_toolchain.base_env(toolchain, work_dir), "GOWORK": "off", "GOPROXY": "off",
           "GOSUMDB": "off", "CGO_ENABLED": "0"}
    _run_logged([toolchain.go, "build", "-buildvcs=false", "-o", out, "."], ORACLE_SRC, env,
                os.path.join(work_dir, "build-goracle.log"), "building goracle")
    return out


def _run_logged(argv: list[str], cwd: str, env: dict | None, log: str, what: str,
                timeout: int = 600) -> None:
    with open(log, "w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(argv) + f"\n# cwd {cwd}\n")
        handle.flush()
        try:
            rc = subprocess.run(argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    if rc != 0:
        raise OracleError(f"{what} failed (exit {rc}); see {log}")


@dataclass
class SiteTable:
    """goracle's site table of a root (`goracle scan`, or COPY/_goracle/sites.jsonl)."""
    callers: dict[tuple[str, int, int, str], str]   # (file, line, column, statement) -> enclosing function
    files: frozenset[str]                            # files goracle tabled
    skipped: dict[str, str]                          # file -> "skipped:<reason>" (cgo, line-directives, ...)


def scan_sites(goracle: str, root: str, out: str) -> SiteTable:
    """`goracle scan --root root --out out` (reads the source, writes nothing
    beside out), read by `site_table`."""
    _run_logged([goracle, "scan", "--root", root, "--out", out], os.path.dirname(out), None,
                out + ".log", "goracle scan")
    return site_table(out)


def site_table(sites_jsonl: str) -> SiteTable:
    """The table's call sites, keyed by (file, line, column, statement), each
    with the C3 qualname of its enclosing function (SiteRec.fid -> FuncRec;
    `<init>` for package-level initializers), and the files goracle tabled
    or skipped. Files are relative to the scanned root."""
    funcs: dict[int, str] = {}
    sites: list[dict] = []
    files: set[str] = set()
    skipped: dict[str, str] = {}
    with open(sites_jsonl, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            kind = rec.get("kind")
            if kind == "func":
                funcs[rec["fid"]] = rec["qualname"]
            elif kind == "site":
                sites.append(rec)
            elif kind == "file":
                if str(rec.get("status", "")).startswith("skipped"):
                    skipped[rec["file"]] = rec["status"]
                else:
                    files.add(rec["file"])
    callers: dict[tuple[str, int, int, str], str] = {}
    for s in sites:
        caller = "<init>" if s["fid"] < 0 else funcs.get(s["fid"], f"?fid{s['fid']}")
        stmt = s.get("stmt") or "call"
        callers[(s["file"], s["line"], s["col"], stmt)] = caller
        if not s.get("name") and s.get("lparen_line"):
            # a callee without a name token, e.g. (*T)(x) or (*fp)(x): C3 puts the
            # site on the call's "(", goracle on the callee and in lparen_line/col
            callers.setdefault((s["file"], s["lparen_line"], s["lparen_col"], stmt), caller)
    return SiteTable(callers=callers, files=frozenset(files), skipped=skipped)


def record_problems(resolutions: dict, table: SiteTable, *, prefix: str = "") -> list[str]:
    """R50 6(b)(c) on every call record: a via=types record whose site has a
    name names targets (target and candidates) whose last qualname segment is
    that name, and each record's caller is the enclosing function of its site
    in goracle's table; a site of a tabled file that the table lacks is a
    problem, and so is a file the table does not know. Files goracle skips for
    a documented reason (cgo, line directives, no module) are not compared.
    `prefix` turns repo-relative paths into the table's ("" when the table's
    root is the repo)."""
    problems: list[str] = []
    for rel, frec in sorted((resolutions or {}).get("files", {}).items()):
        path = prefix + rel
        tabled = path in table.files
        if not tabled and path not in table.skipped and frec.get("calls"):
            problems.append(f"{path}: not in goracle's table")
        for rec in frec.get("calls", []):
            site = rec["site"]
            where = f"{path}:{site['line']}:{site['col']} {site['kind']} {site['name']}"
            if rec.get("via") == "types" and site.get("name"):
                for t in [rec.get("target"), *(rec.get("candidates") or [])]:
                    if t and t["qualname"].rsplit(".", 1)[-1] != site["name"]:
                        problems.append(f"{where}: names {t['qualname']}")
            if not tabled:
                continue
            want = table.callers.get((path, site["line"], site["col"], site["kind"]))
            if want is None:
                problems.append(f"{where}: no such site in goracle's table")
            elif want != site.get("caller"):
                problems.append(f"{where}: caller {site.get('caller')}, goracle's table says {want}")
    return problems


def oracle_edges(resolutions: dict, entities: dict, *, prefix: str = "") -> list[dict]:
    """`goracle match` input: one edge per resolved record whose target is a
    method (every MethodCalls answer), interface methods marked. An interface
    edge always carries the record's candidates, an empty list and the open
    world included: every observed in-repo callee must be among them. Only a
    record whose `candidates_partial` is set (G4 "suppressed", or
    "via_embedded_external") gets no key, so goracle applies its name rule
    (R50 as amended). Other edges carry their candidates when they have some."""
    iface = {(rel, m["qualname"]) for rel, rec in (entities or {}).items()
             for t in rec.get("types") or [] for m in t.get("methods") or []}
    out = []
    for rel, frec in sorted((resolutions or {}).get("files", {}).items()):
        for rec in frec.get("calls", []):
            t = rec.get("target")
            if rec["status"] != "resolved" or not t or t.get("repo") or t.get("kind", "method") != "method":
                continue
            target = {"file": prefix + t["relpath"], "line": t["line"], "qualname": t["qualname"]}
            interface = (t["relpath"], t["qualname"]) in iface
            if interface:
                target["kind"] = "interface_method"
            site = rec["site"]
            edge = {"file": prefix + rel, "line": site["line"], "col": site["col"], "name": site["name"],
                    "target": target, "note": rec.get("via") or ""}
            cands = rec.get("candidates") or []
            if (interface and not rec.get("candidates_partial")) or (cands and not interface):
                edge["candidates"] = [_oracle_candidate(c, prefix) for c in cands]
            out.append(edge)
    return out


def _oracle_candidate(c: dict, prefix: str) -> dict:
    out = {"file": prefix + c["relpath"], "line": c["line"], "qualname": c["qualname"]}
    if c.get("via_embedded_iface"):
        out["via_embedded_iface"] = True       # goracle accepts any same-named method (R50)
    return out


def _a1():
    """test_codes/_gt/run_oracle.py (master plan C11, standalone): its entries
    and environment rules are reused, so evaluate observes what A1 observes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("codedoc_gt_run_oracle", os.path.join(GT_DIR, "run_oracle.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observe(fixture_dir: str, toolchain, goracle: str, work_dir: str) -> tuple[str, list[str]]:
    """Instrument a copy of the fixture and run every entries.json entry as A1
    does (run_oracle.py), then `goracle report`. Returns (obs.jsonl, entries
    that failed)."""
    from . import go_toolchain

    a1 = _a1()
    gocache = os.path.join(go_toolchain.ensure_go_dirs(_base_output()), "gocache")   # shared, warm
    env = {**a1.base_env(toolchain.go, work_dir), "GOCACHE": gocache}
    for sub in ("tmp", "gopath", "xdg"):                # what run_oracle.py's Runner creates
        os.makedirs(os.path.join(work_dir, sub), exist_ok=True)
    copy = os.path.join(work_dir, "copy")
    _rmtree(copy)
    _run_logged([goracle, "instrument", "--src", fixture_dir, "--out", copy], work_dir, env,
                os.path.join(work_dir, "instrument.log"), "goracle instrument")
    failed, hit_dirs = [], []
    for name, entry in a1.load_entries(fixture_dir).items():
        hits = os.path.join(work_dir, "hits", name)
        os.makedirs(hits, exist_ok=True)
        hit_dirs.append(hits)
        try:
            _run_logged(a1.entry_argv(toolchain.go, entry), os.path.join(copy, entry.get("dir", ".")),
                        {**a1.entry_env(toolchain.go, work_dir, entry, hits, copy), "GOCACHE": gocache},
                        os.path.join(work_dir, f"entry-{name}.log"), f"entry {name}",
                        int(entry.get("timeout", 600)))
        except OracleError:
            failed.append(name)
    obs = os.path.join(work_dir, "obs.jsonl")
    _run_logged([goracle, "report", "--copy", copy, "--hits", ",".join(hit_dirs), "--out", obs], work_dir,
                env, os.path.join(work_dir, "report.log"), "goracle report")
    return obs, failed


def match(goracle: str, obs: str, edges: list[dict], work_dir: str) -> dict[str, int]:
    """`goracle match` over the edges: verdict counts."""
    records = os.path.join(work_dir, "edges.jsonl")
    with open(records, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(e) + "\n" for e in edges)
    verdicts = os.path.join(work_dir, "verdicts.jsonl")
    log = os.path.join(work_dir, "match.log")
    with open(log, "w", encoding="utf-8") as handle:
        rc = subprocess.run([goracle, "match", "--obs", obs, "--records", records, "--out", verdicts],
                            cwd=work_dir, stdout=handle, stderr=subprocess.STDOUT, timeout=600).returncode
    if rc not in (0, 1):                        # 1: at least one wrong verdict
        raise OracleError(f"goracle match failed (exit {rc}); see {log}")
    with open(verdicts, encoding="utf-8") as f:
        return dict(Counter(json.loads(line)["verdict"] for line in f if line.strip()))


def oracle_check(fixture_dir: str, runs: dict[str, RepoRun], eval_dir: str, *, judge: bool,
                 printer=print) -> dict:
    """R50 6(a)-(c): every record against goracle's site table (`record_problems`)
    and, with `judge` (iryo-kanri-system and multi_repo), every resolved record
    judged by goracle over the observations of the fixture's entries; any
    WRONG-* verdict, problem, failed entry or oracle error fails, and so do a
    verdict count other than the edge count and a run in which no judged edge
    executed (`executed` counts the edges whose site ran)."""
    from . import go_toolchain

    started = time.perf_counter()
    work = os.path.join(eval_dir, "oracle")
    out: dict[str, Any] = {"problems": 0, "problem_examples": [], "unchecked_files": [], "edges": 0,
                           "executed": 0, "verdicts": {}, "wrong": 0, "unplaced": 0, "failed_entries": [],
                           "error": None}
    try:
        toolchain = go_toolchain.find_toolchain()
        goracle = build_goracle(toolchain, work)
        table = scan_sites(goracle, fixture_dir, os.path.join(work, "sites.jsonl"))
        problems, edges = [], []
        for _name, r in sorted(runs.items()):
            prefix = "" if r.dir in (".", "") else r.dir.rstrip("/") + "/"
            problems += record_problems(r.resolutions, table, prefix=prefix)
            edges += oracle_edges(r.resolutions, r.entities, prefix=prefix)
        out.update(problems=len(problems), problem_examples=problems[:20],
                   unchecked_files=sorted(table.skipped.items()))
        if judge:
            obs, failed = observe(fixture_dir, toolchain, goracle, work)
            verdicts = match(goracle, obs, edges, work)
            unexercised = verdicts.get("UNEXERCISED", 0)
            out.update(edges=len(edges), verdicts=dict(sorted(verdicts.items())), failed_entries=failed,
                       wrong=sum(n for v, n in verdicts.items() if v in WRONG_VERDICTS),
                       unplaced=sum(n for v, n in verdicts.items() if v in UNPLACED_VERDICTS),
                       executed=len(edges) - unexercised)
            if sum(verdicts.values()) != len(edges):
                raise OracleError(f"goracle match returned {sum(verdicts.values())} verdicts for "
                                  f"{len(edges)} edges")
            if unexercised + verdicts.get("UNVERIFIABLE-TARGET", 0) == len(edges):
                # a runtime that silently recorded nothing judges no edge at all
                raise OracleError("goracle judged no executed edge (every verdict UNEXERCISED or "
                                  "UNVERIFIABLE-TARGET)")
    except (OracleError, go_toolchain.ToolchainError, OSError) as exc:
        out["error"] = str(exc)
    out["seconds"] = round(time.perf_counter() - started, 1)
    out["pass"] = (out["error"] is None and out["problems"] == 0 and out["wrong"] == 0
                   and out["unplaced"] == 0 and not out["failed_entries"])
    return out


# ═══════════════════════════════════════════════════════════════════
# Ingest (the pipeline, in-process)
# ═══════════════════════════════════════════════════════════════════


@contextlib.contextmanager
def pinned_settings(eval_dir: str, *, env_mode: bool, repo_root: str) -> Iterator[None]:
    """While a fixture runs: linux/amd64 without cgo, tags or extra builds;
    no PEER_REPOS; no user module cache (GO_USER_MODCACHE is an empty dir), so
    offline loads see only vendor/ and GOPROXY=off; env mode downloads from
    file:///<abs>/test_codes/_proxy with GO_SUMDB=off (spec §9.5, R22)."""
    empty = os.path.join(eval_dir, "no-user-modcache")
    os.makedirs(empty, exist_ok=True)
    pinned = {**PINNED_BUILD, "PEER_REPOS": None, "GO_USER_MODCACHE": empty,
              "GO_MODULES_DOWNLOAD": env_mode}
    if env_mode:
        pinned["GO_PROXY"] = Path(repo_root, "test_codes", "_proxy").resolve().as_uri()
        pinned["GO_SUMDB"] = "off"
    saved = {k: getattr(config, k, None) for k in pinned}
    try:
        for k, v in pinned.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def seed_helper(helper: str, eval_dir: str) -> None:
    """Copy the cached gohelper binary into the eval dir's go/bin, so the run
    does not rebuild it in a cold build cache (ensure_helper finds it there by
    name; under another naming it simply builds)."""
    from . import go_toolchain

    dest = os.path.join(go_toolchain.go_dir(eval_dir), "bin", os.path.basename(helper))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(helper, dest)


def run_graph_track(name: str, directory: str, root: str, out_base: str, *, peers: list,
                    mode: str, toolchain) -> RepoRun:
    """Phases 1, 1.5 and 1.6 and the builders for one repo with --no-spanner,
    in-process, with OUTPUT_DIR at out_base/repos/<name> (so out_base/go is the
    run's module cache); rows go to graph_rows/*.jsonl for inspection. Used by
    evaluate and by the trial (WP-B6)."""
    saved = config.OUTPUT_DIR
    config.OUTPUT_DIR = os.path.join(out_base, "repos", name)
    try:
        data = pipeline.PipelineData(target_dir=root, repo=name, mode=mode, no_spanner=True,
                                     peers=list(peers), toolchain=toolchain)
        pipeline.phase1_scan(data)
        pipeline.phase1b_extract(data)
        pipeline.phase1c_resolve(data)
        t0 = time.perf_counter()
        nodes = pipeline.build_node_rows(data)
        edges = pipeline.derive_edge_rows(data, pipeline._node_index(data))
        data.timings["rows"] = time.perf_counter() - t0
        pipeline._write_rows(data, {**nodes, **edges})
        pipeline._write_graph_meta(data)
    finally:
        config.OUTPUT_DIR = saved
    return RepoRun(name=name, dir=directory, entities=data.extracted_entities,
                   resolutions=data.resolutions, nodes=nodes, edges=edges,
                   discovery=data.discovery, scanned=sorted(data.file_hashes),
                   timings=dict(data.timings))


def ingest_fixture(fixture_dir: str, dirs: dict[str, str], eval_dir: str, env_mode: bool,
                   printer=print) -> dict[str, RepoRun]:
    """Every repo of a fixture through the graph track. In a fixture gated on a
    cross-repo edge set each repo gets the others as peers (spec §8.2); the
    go_layouts repos stay independent."""
    from . import go_toolchain, go_units

    toolchain = go_toolchain.find_toolchain()
    seed_helper(go_toolchain.ensure_helper(toolchain, _base_output()), eval_dir)
    roots = {name: os.path.realpath(os.path.join(fixture_dir, d)) for name, d in dirs.items()}
    linked = "cross_repo" in METRICS.get(os.path.basename(fixture_dir), ())
    peers = {name: go_units.discover_peer(name, root) for name, root in roots.items()} if linked else {}
    runs: dict[str, RepoRun] = {}
    for name, root in roots.items():
        printer(f"  [evaluate] {name}")
        runs[name] = run_graph_track(name, dirs[name], root, eval_dir,
                                     peers=[p for n, p in peers.items() if n != name],
                                     mode="download" if env_mode else "offline", toolchain=toolchain)
    return runs


def _base_output() -> str:
    return os.path.realpath(os.path.abspath(config.OUTPUT_DIR))


def _rmtree(path: str) -> None:
    """shutil.rmtree that also removes read-only module-cache files."""
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(p)
    if os.path.lexists(path):
        shutil.rmtree(path, onexc=retry)


# ═══════════════════════════════════════════════════════════════════
# Fixture runner + report
# ═══════════════════════════════════════════════════════════════════


def _short(t: dict | None) -> dict | None:
    if not t:
        return None
    out = {"file": t.get("file"), "qualname": t["qualname"], "kind": t.get("kind")}
    if t.get("repo"):
        out["repo"] = t["repo"]
    return out


def compact_records(recs: list[dict]) -> dict:
    """The `got` side of a miss: each record at the site, targets in the manifest's shape."""
    return {"records": [{"status": r["status"], "via": r.get("via"), "reason": r.get("reason"),
                         "target": _short(r.get("target")),
                         "static_target": _short(r.get("static_target")),
                         "external": r.get("external"),
                         "candidates": [_short(c) for c in r.get("candidates") or []]}
                        for r in recs]}


def describe_expect(expect: dict) -> str:
    want = expect["status"]
    if expect.get("targets"):
        return f"{want} " + ", ".join(f"{t['qualname']}@{t['file']}" for t in expect["targets"])
    if want == "external":
        return f"external {_module(expect)}"
    if want == "ambiguous":
        return f"ambiguous[{len(expect.get('candidates') or [])}]"
    if want == "unresolved" and expect.get("reason"):
        return f"unresolved ({expect['reason']})"
    return want


def describe_got(got: dict) -> str:
    parts = []
    for r in got["records"]:
        text = r["status"]
        if r.get("external"):
            text += f" {r['external'].get('module')}"
        t = r.get("target")
        if t:
            text += " " + (f"{t['repo']}:" if t.get("repo") else "") + f"{t['file']}:{t['qualname']}"
        if r.get("candidates"):
            text += f" [{len(r['candidates'])} candidates]"
        if r.get("reason"):
            text += f" ({r['reason']})"
        parts.append(text)
    return "; ".join(parts) or "(no record at site)"


def _error(fixture: str, mode: str, error: str, hint: str) -> dict:
    return {"fixture": fixture, "mode": mode, "pass": False, "error": error, "hint": hint}


def evaluate_fixture(fixture: str, repo_root: str, *, env_mode: bool, printer=print,
                     dump_edges: bool = False,
                     ingest: Callable[..., dict[str, RepoRun]] | None = None,
                     oracle: Callable[..., dict] | None = None) -> dict:
    """Ingest and score one fixture in one mode; returns the report (or an error
    report). `ingest` and `oracle` replace the pipeline and `oracle_check` (tests)."""
    from . import crossref, go_toolchain

    started = time.perf_counter()
    mode = "env" if env_mode else "offline"
    fixture_dir = os.path.join(repo_root, "test_codes", fixture)
    manifest_path = os.path.join(fixture_dir, "ground_truth", "manifest.json")
    if not os.path.isfile(manifest_path):
        return _error(fixture, mode, f"manifest.json not found: {manifest_path}",
                      f"python3 test_codes/_gt/generate_manifest.py test_codes/{fixture}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    dirs = repo_dirs(manifest)
    eval_dir = os.path.join(_base_output(), f"eval_{fixture}" + ("_env" if env_mode else ""))
    _rmtree(eval_dir)
    os.makedirs(eval_dir)
    try:
        with pinned_settings(eval_dir, env_mode=env_mode, repo_root=repo_root):
            runs = (ingest or ingest_fixture)(fixture_dir, dirs, eval_dir, env_mode, printer)
    except go_toolchain.ToolchainError as exc:
        return _error(fixture, mode, str(exc), "run `python -m graph_generator setup go`, or set "
                                               "GO_TOOLCHAIN to a go >= 1.26 binary")
    except go_toolchain.HelperBuildError as exc:
        return _error(fixture, mode, str(exc), "the gohelper build failed; it builds offline from "
                                               "gohelper/vendor with the toolchain")
    except NotImplementedError as exc:
        return _error(fixture, mode, str(exc), "evaluate needs the whole graph track")
    cross = None
    if "cross_repo" in METRICS.get(fixture, ()):
        cross = crossref.derive_cross_repo(
            [{"repo": name, "root": (r.discovery.repo_root if r.discovery else ""),
              "entities": r.entities, "resolutions": r.resolutions,
              "modules": [u.key for u in r.discovery.units] if r.discovery else []}
             for name, r in sorted(runs.items())], printer)

    records = fixture_records(runs)
    iface = interface_methods(runs)
    results, misses = [], []
    for case in manifest["cases"]:
        expect = case_expect(case, mode)
        recs = records_at(case, records)
        verdict = score_case(case, records, mode=mode, iface=iface)
        results.append({"category": case["category"], "verdict": verdict,
                        "edges": _edge_counts(expect, recs, _interface_case(expect, iface))})
        if verdict != "correct":
            misses.append({"id": case["id"], "category": case["category"], "verdict": verdict,
                           "expected": expect, "got": compact_records(recs)})
    counts = Counter(r["verdict"] for r in results)
    metrics = METRICS.get(fixture, ())
    view = build_view(runs) if {"node_coverage", "qa"} & set(metrics) else None
    report: dict[str, Any] = {
        "fixture": fixture, "mode": mode, "cases": len(results),
        "verdicts": {v: counts[v] for v in VERDICTS},
        "completeness": {"value": counts["correct"] + counts["alternate"],
                         "required": completeness_min(len(results))},
        "node_coverage": _rate(*node_coverage(manifest.get("decl_pins", []), view))
        if "node_coverage" in metrics else None,
        "edge_rate": _rate(*edge_rate(runs)) if "edge_rate" in metrics else None,
        "qa": qa_rate(fixture_dir, view) if "qa" in metrics else None,
        "cross_repo": None, "discovery": None, "class_edges": None,
        "imports": {**imports_check(runs), "gated": "imports" in metrics},
        "loads": load_check(runs, all_ok="discovery" not in metrics),
        "rows": {table: sum(len(r.edges.get(table, [])) for r in runs.values()) for table in ROW_TABLES},
        "per_category": per_category(results),
        "edges": edge_precision_recall(results),
        "misses": misses, "eval_dir": eval_dir,
    }
    report["completeness"]["pass"] = report["completeness"]["value"] >= report["completeness"]["required"]
    labels = node_labels({name: r.entities for name, r in runs.items()})
    if cross is not None:
        report["cross_repo"] = cross_repo_check(manifest.get("cross_repo_edges", []), cross, labels)
        report["crossref_drops"] = cross["drops"]
    if "class_edges" in manifest:
        report["class_edges"] = class_edge_check(manifest["class_edges"], runs, labels, mode)
        if "class_edges" in metrics and not report["class_edges"]["expected"]:   # required: never vacuous
            report["class_edges"]["missing"] = [f"class_edges pins no edge for {mode}"]
            report["class_edges"]["pass"] = False
    elif "class_edges" in metrics:      # required here: a manifest that lost the key fails
        report["class_edges"] = {"expected": 0, "actual": 0, "missing": ["manifest.json has no class_edges"],
                                 "extra": [], "pass": False}
    if "discovery" in metrics:
        report["discovery"] = _discovery_report(fixture_dir, runs, dirs, mode)
    report["oracle"] = (oracle or oracle_check)(fixture_dir, runs, eval_dir, judge="judge" in metrics,
                                                printer=printer)
    gated = [report[m] for m in ("node_coverage", "edge_rate", "qa", "cross_repo", "class_edges", "discovery",
                                 "loads", "oracle") if report[m]]
    if report["imports"]["gated"]:
        gated.append(report["imports"])
    report["pass"] = (counts["wrong"] == 0 and report["completeness"]["pass"]
                      and all(m["pass"] for m in gated))
    if dump_edges:
        dump: dict[str, Any] = {name: r.edges for name, r in runs.items()}
        if cross is not None:
            dump["crossref"] = cross
        report["edges_path"] = os.path.join(eval_dir, "edges.json")
        with open(report["edges_path"], "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=True, indent=1)
    report["seconds"] = round(time.perf_counter() - started, 1)
    return report


def _discovery_report(fixture_dir: str, runs: dict[str, RepoRun], dirs: dict[str, str],
                      mode: str) -> dict:
    path = os.path.join(fixture_dir, "ground_truth", "expected_units.json")
    if not os.path.isfile(path):
        return {"pass": False, "layouts": 0, "problems": {"*": [f"{path} not found"]}}
    with open(path, "r", encoding="utf-8") as f:
        expected = json.load(f).get("repos", {})
    problems: dict[str, list[str]] = {}
    for name in sorted(set(expected) | set(dirs)):
        if name not in expected:
            found = [f"layout {name} has no expected_units.json entry"]
        elif name not in runs:
            found = [f"layout {name} was not ingested"]
        else:
            r = runs[name]
            found = discovery_problems(expected[name], r.discovery, r.scanned,
                                       (r.resolutions or {}).get("groups", {}), mode)
        if found:
            problems[name] = found
    return {"pass": not problems, "layouts": len(set(expected) | set(dirs)), "problems": problems}


def print_report(report: dict, printer=print, *, quiet_misses: bool = False) -> bool:
    """Print one fixture report; returns report["pass"]."""
    if "error" in report:
        printer(f"  ERROR [{report['fixture']}, {report['mode']}]: {report['error']}")
        if report.get("hint"):
            printer(f"  HINT:  {report['hint']}")
        return False
    mark = lambda ok: "[PASS]" if ok else "[FAIL]"
    v, c = report["verdicts"], report["completeness"]
    printer("=" * 70)
    printer(f"  EVALUATE — {report['fixture']}  (mode: {report['mode']})")
    printer("=" * 70)
    printer(f"  Cases: {report['cases']}  correct {v['correct']}  alternate {v['alternate']}  "
            f"wrong {v['wrong']}  unanswered {v['unanswered']}")
    printer(f"  {mark(v['wrong'] == 0)} Zero wrong     : {v['wrong']} wrong")
    printer(f"  {mark(c['pass'])} Completeness   : {c['value']}/{report['cases']} "
            f"(need >= {c['required']})")
    for key, label in (("node_coverage", "Node coverage  "), ("edge_rate", "Edge rate      "),
                       ("qa", "QA             ")):
        if report.get(key) is not None:
            printer(f"  {mark(report[key]['pass'])} {label}: {report[key]['value']:.3f} "
                    f"(need >= 0.85)")
    if report.get("cross_repo") is not None:
        x = report["cross_repo"]
        printer(f"  {mark(x['pass'])} Cross-repo set : {x['actual']} actual / {x['expected']} expected")
    if report.get("class_edges") is not None:
        c = report["class_edges"]
        printer(f"  {mark(c['pass'])} Class edges    : {c['actual']} actual / {c['expected']} expected")
        for e in c["missing"][:20]:
            printer(f"  missing class edge: {e}")
        for e in c["extra"][:20]:
            printer(f"  extra class edge: {e}")
    if report.get("discovery") is not None:
        d = report["discovery"]
        printer(f"  {mark(d['pass'])} Discovery      : {len(d['problems'])} of {d['layouts']} "
                f"layouts differ")
    oracle = report.get("oracle")
    if oracle is not None:
        judged = (f"; goracle judged {oracle['edges']} edges ({oracle.get('executed', 0)} executed): "
                  f"WRONG {oracle['wrong']}"
                  + "".join(f", {v} {n}" for v, n in oracle["verdicts"].items() if v not in WRONG_VERDICTS)
                  if oracle["edges"] or oracle["verdicts"] else "")
        printer(f"  {mark(oracle['pass'])} Oracle         : {oracle['problems']} record problems{judged}"
                + (f" ({oracle['seconds']} s)" if oracle.get("seconds") is not None else ""))
        if oracle.get("error"):
            printer(f"  oracle error: {oracle['error']}")
        for name in oracle.get("failed_entries") or []:
            printer(f"  oracle entry failed: {name}")
        for line in oracle.get("problem_examples") or []:
            printer(f"  record problem: {line}")
    loads = report.get("loads")
    if loads is not None:
        printer(f"  {mark(loads['pass'])} Loads          : {loads['groups']} groups, "
                f"{len(loads['not_ok'])} not ok, {loads['join_misses']} join misses")
        for line in loads["not_ok"]:
            printer(f"  group not ok: {line}")
        for line in loads.get("site_mismatch") or []:
            printer(f"  sites without their record: {line}")
    imports = report.get("imports") or {}
    if imports:
        tag = mark(imports["pass"]) if imports.get("gated") else "[INFO]"
        printer(f"  {tag} Imports        : {imports['mapped']} of {imports['records']} in-repo import "
                f"records mapped ({imports['unmapped']} unmapped, {imports['ambiguous']} ambiguous)")
    printer(f"  Edges: precision {report['edges']['precision']:.3f}  "
            f"recall {report['edges']['recall']:.3f}")
    if report.get("rows") is not None:
        printer("  Rows: " + "  ".join(f"{table} {n}" for table, n in report["rows"].items()))
    printer("  ── per category ──")
    printer(f"    {'category':<10} {'total':>5} {'complete':>8} {'sound':>5}")
    for cat, row in sorted(report["per_category"].items()):
        printer(f"    {cat:<10} {row['total']:>5} {row['complete']:>8} {row['sound']:>5}")
    for m in report["misses"]:
        if m["verdict"] == "wrong":
            printer(f"  WRONG {m['id']}: expected {describe_expect(m['expected'])}; "
                    f"got {describe_got(m['got'])}")
    for example in imports.get("unmapped_examples", []):
        printer(f"  unmapped import: {example}")
    if not quiet_misses:
        for example in imports.get("ambiguous_examples", []):
            printer(f"  ambiguous import: {example}")
        rest = [m for m in report["misses"] if m["verdict"] != "wrong"]
        if rest:
            printer("  ── misses ──")
            for m in rest:
                printer(f"    {m['id']:<10} {m['verdict']:<10} expected "
                        f"{describe_expect(m['expected'])}; got {describe_got(m['got'])}")
        for q in (report.get("qa") or {}).get("misses", []):
            printer(f"  QA miss {q['id']}: {q['question']} -> {q['actual']}")
        if report.get("cross_repo") is not None:
            for e in report["cross_repo"]["missing"]:
                printer(f"  missing cross-repo edge: {e}")
            for e in report["cross_repo"]["extra"]:
                printer(f"  extra cross-repo edge: {e}")
        for layout, found in sorted(((report.get("discovery") or {}).get("problems") or {}).items()):
            for p in found:
                printer(f"  discovery {layout}: {p}")
    if report.get("edges_path"):
        printer(f"  Edges written to {report['edges_path']}")
    if report.get("eval_dir"):
        printer(f"  Outputs: {report['eval_dir']}")
    if report.get("seconds") is not None:
        printer(f"  Time: {report['seconds']} s")
    printer(f"  RESULT: {'PASS' if report['pass'] else 'FAIL'}")
    return report["pass"]


def run_evaluation(fixtures: list[str], repo_root: str, *, env_mode: bool = False, printer=print,
                   dump_edges: bool = False, quiet_misses: bool = False) -> bool:
    """Evaluate every fixture in one mode; True when all of them pass their gates."""
    ok = True
    for fixture in fixtures:
        report = evaluate_fixture(fixture, repo_root, env_mode=env_mode, printer=printer,
                                  dump_edges=dump_edges)
        ok = print_report(report, printer, quiet_misses=quiet_misses) and ok
    return ok
