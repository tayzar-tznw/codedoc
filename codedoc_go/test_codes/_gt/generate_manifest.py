#!/usr/bin/env python3
"""Generate <fixture>/ground_truth/manifest.json from ground_truth/cases.py.

Usage: python3 generate_manifest.py <fixture-dir>

Each case points at a call site by file + a snippet that occurs once in that
file + the callee token inside the snippet (`occurrence` counts token matches
inside the snippet). This script derives the 1-based line, the 1-based byte
column of the token and its 0-based byte offset, and fills the anchor (line
and column of the declared name; the `func` keyword for closures) into every
target, candidate, runtime target, static target, distractor and declaration
pin. A case's optional offline expectation (case(..., offline=...), master
plan C10) is anchored the same way into "expect_offline" (null when absent).
cases.py CROSS_REPO_EDGES is the only source of record of a multi-repo
fixture's cross-repo edges: it goes into the manifest's "cross_repo_edges"
and, when non-empty, into ground_truth/expected_cross_repo_edges.json, the
file evaluate reads. cases.py CLASS_EDGES (spec R50) is a fixture's exact
ClassImplements / ClassEmbeds set; it goes into the manifest's "class_edges"
only when the fixture defines it (an absent key pins nothing).
Stdlib only; imports nothing from graph_generator. Case positions are
computed here on their own; check_ground_truth.py re-derives them with
different code.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_lib  # noqa: E402  (declaration anchors only)

VERSION = 1
GO_VERSION = "go1.27.2"
BUILD = {"goos": "linux", "goarch": "amd64", "cgo": False, "tags": []}
TARGET_LISTS = ("targets", "candidates", "runtime_targets", "distractors")
REQUIRED = ("id", "category", "file", "snippet", "token", "site_kind", "expect", "why")
EDGES_FILE = "expected_cross_repo_edges.json"
EDGE_LABELS = {  # master plan C10
    "file": "<repo>:<repo-relative path>",
    "package": "<repo>:<repo-relative directory>|<package clause name>",
    "class": "<repo>:<repo-relative path of the declaration>:<qualname>",
    "method": "<repo>:<repo-relative path of the declaration>:<qualname>",
}


class ManifestError(Exception):
    pass


def load_cases(fixture_dir: str):
    path = os.path.join(fixture_dir, "ground_truth", "cases.py")
    spec = importlib.util.spec_from_file_location("gt_cases_generate", path)
    if spec is None or spec.loader is None:
        raise ManifestError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def token_offset(snippet: str, token: str, occurrence: int) -> int | None:
    """Character offset of the occurrence-th token inside snippet (1-based)."""
    if re.fullmatch(r"\w+", token):
        pattern = r"(?<!\w)" + re.escape(token) + r"(?!\w)"
    else:
        pattern = re.escape(token)
    hits = [m.start() for m in re.finditer(pattern, snippet)]
    if occurrence < 1 or len(hits) < occurrence:
        return None
    return hits[occurrence - 1]


def count_overlapping(text: str, part: str) -> int:
    count, at = 0, text.find(part)
    while at >= 0:
        count += 1
        at = text.find(part, at + 1)
    return count


def locate(case: dict, fixture_dir: str) -> tuple[int, int, int]:
    """(line, 1-based byte column, 0-based byte offset) of the case token."""
    cid, rel = case["id"], case["file"]
    path = os.path.join(fixture_dir, rel)
    if not rel.endswith(".go") or not os.path.isfile(path):
        raise ManifestError(f"{cid}: Go file not found: {rel}")
    snippet = case["snippet"]
    if not snippet or "\n" in snippet:
        raise ManifestError(f"{cid}: snippet must be one non-empty line: {snippet!r}")
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    total = count_overlapping(text, snippet)
    if total != 1:
        raise ManifestError(f"{cid}: snippet occurs {total} times in {rel}: {snippet!r}")
    start = text.index(snippet)
    offset = token_offset(snippet, case["token"], case.get("occurrence", 1))
    if offset is None:
        raise ManifestError(f"{cid}: token {case['token']!r} occurrence {case.get('occurrence', 1)} "
                            f"not in snippet {snippet!r}")
    index = start + offset
    line_start = text.rfind("\n", 0, index) + 1
    line = text.count("\n", 0, index) + 1
    col = len(text[line_start:index].encode("utf-8")) + 1
    return line, col, len(text[:index].encode("utf-8"))


def resolve_target(owner: str, target: dict, fixture_dir: str) -> dict:
    if not isinstance(target, dict):
        raise ManifestError(f"{owner}: target is not a dict: {target!r}")
    for key in ("qualname", "module", "kind", "file"):
        if not target.get(key):
            raise ManifestError(f"{owner}: target lacks {key!r}: {target}")
    path = os.path.join(fixture_dir, target["file"])
    if not target["file"].endswith(".go") or not os.path.isfile(path):
        raise ManifestError(f"{owner}: target file not found: {target['file']}")
    decl = gt_lib.parse_file(path).decl(target["qualname"])
    if decl is None:
        raise ManifestError(f"{owner}: {target['qualname']} is not declared in {target['file']}")
    if decl.kind != target["kind"]:
        raise ManifestError(f"{owner}: {target['qualname']} is a {decl.kind} ({decl.member}), "
                            f"target says {target['kind']}")
    return {**target, "line": decl.line, "col": decl.col}


def anchor_expect(owner: str, expect: dict, fixture_dir: str, problems: list[str]) -> dict:
    """A copy of an expectation with anchors filled into every target; problems collect errors."""
    expect = dict(expect)
    for key in TARGET_LISTS:
        resolved = []
        for item in expect.get(key) or []:
            try:
                resolved.append(resolve_target(f"{owner} {key}", item, fixture_dir))
            except ManifestError as exc:
                problems.append(str(exc))
        expect[key] = resolved
    static = expect.get("static_target")
    expect["static_target"] = None
    if static:
        try:
            expect["static_target"] = resolve_target(f"{owner} static_target", static, fixture_dir)
        except ManifestError as exc:
            problems.append(str(exc))
    for key in ("open_world", "external", "reason"):
        expect.setdefault(key, None)
    return expect


def build_case(case: dict, fixture_dir: str) -> dict:
    missing = [k for k in REQUIRED if k not in case]
    if missing:
        raise ManifestError(f"{case.get('id', '?')}: missing keys {missing}")
    line, col, offset = locate(case, fixture_dir)
    problems: list[str] = []
    expect = anchor_expect(case["id"], case["expect"], fixture_dir, problems)
    offline = case.get("expect_offline")
    if offline is not None:
        offline = anchor_expect(f"{case['id']} offline", offline, fixture_dir, problems)
    if problems:
        raise ManifestError("\n".join(problems))
    out = {key: case[key] for key in REQUIRED}
    out["occurrence"] = case.get("occurrence", 1)
    out["entry"] = case.get("entry")
    out["expect"] = expect
    out["expect_offline"] = offline
    out["line"], out["col"], out["offset"] = line, col, offset
    return out


def build_manifest(fixture_dir: str) -> dict:
    module = load_cases(fixture_dir)
    fixture = os.path.basename(os.path.abspath(fixture_dir))
    cases = list(getattr(module, "CASES"))
    ids = [c.get("id") for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ManifestError(f"duplicate case ids: {dupes}")
    problems: list[str] = []
    built = []
    for case in sorted(cases, key=lambda c: c["id"]):
        try:
            built.append(build_case(case, fixture_dir))
        except ManifestError as exc:
            problems.append(str(exc))
    pins = []
    for pin in getattr(module, "DECL_PINS", []):
        try:
            pins.append(resolve_target(f"pin {pin.get('qualname')}", pin, fixture_dir))
        except ManifestError as exc:
            problems.append(str(exc))
    if problems:
        raise ManifestError("\n".join(problems))
    repos = getattr(module, "REPOS", None) or [{"name": fixture, "dir": "."}]
    manifest = {
        "version": VERSION,
        "fixture": fixture,
        "go": GO_VERSION,
        "build": dict(BUILD),
        "repos": [dict(r) for r in repos],
        "cases": built,
        "decl_pins": pins,
        "cross_repo_edges": [dict(e) for e in getattr(module, "CROSS_REPO_EDGES", [])],
        "xpr_pairs": [dict(p) for p in getattr(module, "XPR_PAIRS", [])],
    }
    if hasattr(module, "CLASS_EDGES"):
        manifest["class_edges"] = [dict(e) for e in module.CLASS_EDGES]
    return manifest


def edges_document(manifest: dict) -> dict:
    """ground_truth/expected_cross_repo_edges.json: the exact cross-repo edge set that evaluate
    reads, written from cases.py CROSS_REPO_EDGES (the only source of record, master plan C10)."""
    edges = manifest["cross_repo_edges"]
    return {
        "version": VERSION,
        "fixture": manifest["fixture"],
        "source": "ground_truth/cases.py CROSS_REPO_EDGES, written by test_codes/_gt/generate_manifest.py; "
                  "do not edit",
        "labels": dict(EDGE_LABELS),
        "counts": {table: sum(1 for e in edges if e.get("table") == table)
                   for table in sorted({e.get("table") for e in edges})},
        "edges": edges,
    }


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: generate_manifest.py <fixture-dir>", file=sys.stderr)
        return 2
    fixture_dir = argv[1]
    try:
        manifest = build_manifest(fixture_dir)
    except ManifestError as exc:
        for line in str(exc).splitlines():
            print("ERROR:", line)
        return 1
    out = os.path.join(fixture_dir, "ground_truth", "manifest.json")
    write_json(out, manifest)
    print(f"wrote {out}: {len(manifest['cases'])} cases, {len(manifest['decl_pins'])} pins, "
          f"{len(manifest['cross_repo_edges'])} cross-repo edges"
          + (f", {len(manifest['class_edges'])} class edges" if "class_edges" in manifest else ""))
    if manifest["cross_repo_edges"]:
        edges_out = os.path.join(fixture_dir, "ground_truth", EDGES_FILE)
        write_json(edges_out, edges_document(manifest))
        print(f"wrote {edges_out}: {len(manifest['cross_repo_edges'])} edges")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
