#!/usr/bin/env python3
"""Standalone gate for a Go fixture's ground truth (stdlib only).

Usage: python3 check_ground_truth.py <fixture-dir>

Checks, without importing graph_generator and without sharing position code
with generate_manifest.py:
  1. ID bijection between ground_truth/cases.py and manifest.json, and the
     manifest header (version, fixture, go, build).
  2. Snippet round trip: the snippet occurs once in the file and lies on the
     manifest line; the token occurrence sits at the manifest byte column;
     the byte offset agrees with line and column.
  3. The (line, col) is a call site of the case's kind (call, go, defer,
     range) under the conventions of spec §4.3, and no two cases share a
     (file, line, col, kind) site.
  4. Anchors: every target, candidate, runtime target, static target,
     distractor and declaration pin names a declaration of its file (C3
     qualname, right kind) whose name sits at the anchor line and column; the
     anchor line itself reads as that declaration (a func or type line, an
     interface method, the func keyword of a closure).
  5. Module truthfulness: go.mod and go.work parse; module paths are unique
     per repo; go.work `use` directories hold a go.mod; every requirement is a
     workspace module, a local replace or a version in test_codes/_proxy;
     every import of a fixture package names an existing package directory of
     a module the importing module can reach; every target's module is the
     module of its file. Layout problems a fixture has on purpose are listed
     in cases.py LAYOUT_EXCEPTIONS (substrings); an exception that matches
     nothing is a problem itself.
  6. Expectation schema per status, for the env expectation and for the
     optional offline one ("expect_offline", master plan C10); distractors
     never overlap the expected targets; an external case names "std", "C"
     or a module path; peer targets name their repo, and the candidates of a
     peer call lie in the case's own repo; a conversion names nothing.
  7. XPR pairs: line pairs are byte-identical lines with diverging
     expectations; file pairs are byte-identical files.
  8. entries.json schema and every case's entry (a GOWORK other than "off" is
     a fixture-relative work file); qa_questions.json and features.json
     schemas when present.
  9. CROSS_REPO_EDGES endpoints exist (a package target is "<dir>|<clause
     name>"), and the CrossRepoCalls set equals the edges the peer call cases
     imply (from the site's caller to the target); the manifest and
     ground_truth/expected_cross_repo_edges.json equal cases.py
     CROSS_REPO_EDGES, their only source of record.
 10. CLASS_EDGES (spec R50), a fixture's exact ClassImplements / ClassEmbeds
     set: the manifest's "class_edges" equals it (both absent when cases.py
     has none); each edge is ClassImplements with a bool pointer_receiver or
     ClassEmbeds with a bool pointer, optionally limited to "modes" (a
     non-empty subset of offline and env); no (table, source, target) twice;
     both ends are "<repo>:<relpath>:<qualname>" naming a type declared in
     that file.
Exit code 0 means no problem.
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
import gt_lib  # noqa: E402

PROXY = os.path.normpath(os.path.join(HERE, "..", "_proxy"))
GO_VERSION = "go1.27.2"
BUILD = {"goos": "linux", "goarch": "amd64", "cgo": False, "tags": []}
UNRESOLVED_REASONS = ("missing-module", "incomplete-type", "excluded-by-build", "toolchain", "no-declaration",
                      "stale", "syntax-error", "unscanned", "outside-repo", "no-module", "load-error")
QA_TYPES = ("node_exists", "callees_of", "possibly_callees_of", "callers_of", "methods_of", "class_file",
            "imports_of", "implementers_of", "embeds_of", "cross_module_callees_of")
QA_MATCHES = ("equals", "exact_set", "contains_all", "count_at_least")
EDGE_TABLES = ("CrossRepoCalls", "CrossRepoRef", "CrossRepoImports")
REF_KINDS = ("embeds", "implements")
CLASS_EDGE_FLAGS = {"ClassImplements": "pointer_receiver", "ClassEmbeds": "pointer"}
EDGE_MODES = ("offline", "env")
ENTRY_KEYS = {"dir", "cmd", "test", "test_args", "env", "timeout"}
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")
IDENT_BYTE = re.compile(rb"[A-Za-z0-9_\x80-\xff]")


def ident_bounded(snippet: bytes, at: int, token: bytes) -> bool:
    before = snippet[at - 1:at]
    after = snippet[at + len(token):at + len(token) + 1]
    return not (IDENT_BYTE.match(before or b" ") or IDENT_BYTE.match(after or b" "))


def is_std(path: str) -> bool:
    return "." not in path.split("/", 1)[0]


class Checker:
    def __init__(self, fixture_dir: str, proxy_dir: str = PROXY) -> None:
        self.root = os.path.abspath(fixture_dir)
        self.proxy = proxy_dir
        self.problems: list[str] = []
        self.layout_problems: list[str] = []
        self._bytes: dict[str, bytes | None] = {}

    def problem(self, text: str) -> None:
        self.problems.append(text)

    def layout_problem(self, text: str) -> None:
        self.layout_problems.append(text)

    # ------------------------------------------------------------- files
    def data(self, rel: str) -> bytes | None:
        if rel not in self._bytes:
            path = os.path.join(self.root, rel)
            self._bytes[rel] = gt_lib.read_bytes(path) if os.path.isfile(path) else None
        return self._bytes[rel]

    def gofile(self, rel: str) -> gt_lib.GoFile | None:
        if self.data(rel) is None or not rel.endswith(".go"):
            return None
        return gt_lib.parse_file(os.path.join(self.root, rel))

    def repo_of(self, rel: str) -> str | None:
        best = None
        for repo in self.repos:
            base = repo["dir"]
            if base == "" or rel == base or rel.startswith(base + "/"):
                if best is None or len(base) > len(best["dir"]):
                    best = repo
        return best["name"] if best else None

    # ------------------------------------------------------------- layout
    def check_layout(self) -> None:
        lay = self.layout
        for text in lay.errors:
            self.layout_problem(text)
        seen: dict[tuple[str, str], str] = {}
        for d, mod in sorted(lay.modules.items()):
            key = (self.repo_of(d + "/x") or "", mod["module"] or "")
            if key in seen:
                self.layout_problem(f"module path {mod['module']} is declared twice in repo {key[0]}: "
                                    f"{seen[key] or '.'} and {d or '.'}")
            seen.setdefault(key, d)
        self.use_targets: dict[str, list[str]] = {}
        for wdir, work in sorted(lay.workspaces.items()):
            targets = []
            for use in work["use"]:
                target = os.path.normpath(os.path.join(wdir, use)).replace(os.sep, "/")
                target = "" if target == "." else target
                if target.startswith("../") or target == "..":
                    self.layout_problem(f"{wdir or '.'}/go.work: use {use} leaves the fixture")
                    continue
                if target not in lay.modules:
                    self.layout_problem(f"{wdir or '.'}/go.work: use {use} has no go.mod")
                    continue
                targets.append(target)
            self.use_targets[wdir] = targets
            for rep in work["replace"]:
                if rep["local"]:
                    self.check_local_replace(f"{wdir or '.'}/go.work", wdir, rep)
        # spec R4: a module builds in the workspace of the go.work found by walking up from
        # its directory, and only when that go.work uses it; otherwise with GOWORK=off
        self.workspace_of: dict[str, str] = {}
        for d in lay.modules:
            wdir = self.walkup_work(d)
            if wdir is not None and d in self.use_targets.get(wdir, []):
                self.workspace_of[d] = wdir
        for d, mod in sorted(lay.modules.items()):
            for rep in mod["replace"]:
                if rep["local"]:
                    self.check_local_replace(f"{d or '.'}/go.mod", d, rep)
            for path, version, _indirect in mod["require"]:
                if not self.requirement_ok(d, path, version):
                    self.layout_problem(f"{d or '.'}/go.mod: require {path} {version} is neither a workspace module, "
                                        f"a local replace nor a version in _proxy")
        for rel in lay.go_files:
            self.check_imports(rel)

    def check_local_replace(self, where: str, base: str, rep: dict) -> None:
        target = os.path.normpath(os.path.join(self.root, base, rep["new"]))
        gomod = os.path.join(target, "go.mod")
        if not os.path.isfile(gomod):
            self.layout_problem(f"{where}: replace {rep['old']} => {rep['new']} has no go.mod")
            return
        with open(gomod, encoding="utf-8") as handle:
            declared = gt_lib.parse_gomod(handle.read())["module"]
        if declared != rep["old"]:
            self.layout_problem(f"{where}: replace {rep['old']} => {rep['new']} declares module {declared}")

    def walkup_work(self, moddir: str) -> str | None:
        """Directory of the nearest go.work at or above a module directory."""
        parts = moddir.split("/") if moddir else []
        for k in range(len(parts), -1, -1):
            cand = "/".join(parts[:k])
            if cand in self.layout.workspaces:
                return cand
        return None

    def replaces_for(self, moddir: str) -> list[tuple[str, dict]]:
        """(base dir, replace) pairs that apply to a module's build; go.work replaces come first and win."""
        out = []
        wdir = self.workspace_of.get(moddir)
        if wdir is not None:
            out += [(wdir, r) for r in self.layout.workspaces[wdir]["replace"]]
            for d in self.members(moddir):
                out += [(d, r) for r in self.layout.modules[d]["replace"]]
        else:
            out += [(moddir, r) for r in self.layout.modules[moddir]["replace"]]
        return out

    def members(self, moddir: str) -> list[str]:
        """The modules of a module's build: every module its workspace uses, or itself."""
        wdir = self.workspace_of.get(moddir)
        return sorted(self.use_targets[wdir]) if wdir is not None else [moddir]

    def reachable(self, moddir: str) -> dict[str, tuple[str, str]]:
        """Module path -> ("dir", fixture dir) or ("proxy[:module]", version) for a module's build."""
        lay = self.layout
        members = self.members(moddir)
        member_paths = {lay.modules[d]["module"] for d in members}
        out: dict[str, tuple[str, str]] = {}
        for d in members:
            out[lay.modules[d]["module"]] = ("dir", d)
        for d in members:
            for path, version, _ in lay.modules[d]["require"]:
                out.setdefault(path, ("proxy", version))
        for base, rep in reversed(self.replaces_for(moddir)):
            if rep["old"] in member_paths:
                continue
            if rep["local"]:
                target = os.path.relpath(os.path.normpath(os.path.join(self.root, base, rep["new"])), self.root)
                out[rep["old"]] = ("dir", target.replace(os.sep, "/"))
            elif rep["old"] in out:
                out[rep["old"]] = ("proxy:" + rep["new"], rep["new_version"])
        return out

    def requirement_ok(self, moddir: str, path: str, version: str) -> bool:
        lay = self.layout
        if moddir in self.workspace_of and any(lay.modules[d]["module"] == path for d in self.members(moddir)):
            return True
        for base, rep in self.replaces_for(moddir):
            if rep["old"] != path or (rep["old_version"] and rep["old_version"] != version):
                continue
            if rep["local"]:
                return os.path.isfile(os.path.join(self.root, base, rep["new"], "go.mod"))
            return gt_lib.proxy_has(self.proxy, rep["new"], rep["new_version"])
        return gt_lib.proxy_has(self.proxy, path, version)

    def check_imports(self, rel: str) -> None:
        moddir = self.layout.module_dir_of(rel)
        if moddir is None:
            return
        gof = self.gofile(rel)
        if gof is None:
            return
        reach = self.reachable(moddir)
        for imp in gof.imports:
            path = imp["path"]
            if path == "C" or is_std(path):
                continue
            provider = max((m for m in reach if path == m or path.startswith(m + "/")), key=len, default=None)
            if provider is None:
                self.layout_problem(f"{rel}: imports {path}, which no module reachable from "
                                    f"{self.layout.modules[moddir]['module']} provides")
                continue
            where, extra = reach[provider]
            sub = path[len(provider):].lstrip("/")
            if where == "dir":
                pkg_dir = os.path.join(self.root, extra, sub)
                if not os.path.isdir(pkg_dir) or not any(n.endswith(".go") for n in os.listdir(pkg_dir)):
                    self.layout_problem(f"{rel}: imports {path}, but {extra or '.'}/{sub} holds no Go package")
            else:
                module = where.split(":", 1)[1] if ":" in where else provider
                if not self.proxy_package(module, extra, sub):
                    self.layout_problem(f"{rel}: imports {path}, which {module}@{extra} in _proxy does not hold")

    def proxy_package(self, module: str, version: str, sub: str) -> bool:
        import zipfile
        zpath = os.path.join(self.proxy, gt_lib.escape_path(module), "@v", gt_lib.escape_path(version) + ".zip")
        if not os.path.isfile(zpath):
            return False
        prefix = f"{module}@{version}/" + (sub + "/" if sub else "")
        with zipfile.ZipFile(zpath) as zf:
            return any(n.startswith(prefix) and n.endswith(".go") and "/" not in n[len(prefix):]
                       for n in zf.namelist())

    # ------------------------------------------------------------- targets
    def check_target(self, owner: str, target: dict) -> None:
        for key in ("qualname", "module", "kind", "file", "line", "col"):
            if key not in target:
                self.problem(f"{owner}: target lacks {key!r}: {target}")
                return
        if target["kind"] not in gt_lib.TARGET_KINDS:
            self.problem(f"{owner}: bad target kind {target['kind']!r}")
        rel = target["file"]
        gof = self.gofile(rel)
        if gof is None:
            self.problem(f"{owner}: target file missing or not Go: {rel}")
            return
        module = self.layout.module_of(rel)
        if module is None:
            self.problem(f"{owner}: {rel} lies under no go.mod")
        elif module != target["module"]:
            self.problem(f"{owner}: {rel} belongs to module {module}, target says {target['module']}")
        if "repo" in target and target["repo"] != self.repo_of(rel):
            self.problem(f"{owner}: {rel} is in repo {self.repo_of(rel)}, target says {target['repo']}")
        decl = gof.decl(target["qualname"])
        if decl is None:
            self.problem(f"{owner}: {target['qualname']} is not declared in {rel}")
            return
        if decl.kind != target["kind"]:
            self.problem(f"{owner}: {target['qualname']} is a {decl.kind}, target says {target['kind']}")
        if decl.qualname == "<init>":
            return
        if (decl.line, decl.col) != (target["line"], target["col"]):
            self.problem(f"{owner}: anchor {rel}:{target['line']}:{target['col']} but {target['qualname']} "
                         f"is declared at {decl.line}:{decl.col} (regenerate the manifest)")
            return
        self.check_anchor_text(owner, rel, decl, gof)

    def check_anchor_text(self, owner: str, rel: str, decl, gof) -> None:
        """Read the anchor line without the parser: it must spell the declaration."""
        line = gt_lib.split_lines(self.data(rel))[decl.line - 1]
        at = line[decl.col - 1:]
        name = decl.name.encode("utf-8")
        member = decl.member
        if member == "closure":
            ok = at.startswith(b"func") and not IDENT_BYTE.match(at[4:5] or b" ")
        elif member == "package":
            clause = (gof.package or "").encode("utf-8")
            ok = bool(clause) and at.startswith(clause) and re.match(rb"\s*package\s+$", line[:decl.col - 1]) is not None
        elif member == "interface_method":
            ok = re.match(re.escape(name) + rb"\s*\(", at) is not None
        elif decl.kind == "class":
            ok = at.startswith(name) and re.match(rb"\s*(type\s+)?$", line[:decl.col - 1]) is not None
        else:
            head = line[:decl.col - 1]
            ok = at.startswith(name) and re.match(rb"\s*func\s*(\([^)]*\)\s*)?$", head) is not None
        if not ok:
            self.problem(f"{owner}: anchor line {rel}:{decl.line} does not declare {decl.qualname}")

    # ------------------------------------------------------------- cases
    def check_case(self, case: dict) -> None:
        cid = case.get("id", "?")
        for key in ("id", "category", "file", "snippet", "token", "occurrence", "site_kind", "expect", "why",
                    "line", "col", "offset", "entry"):
            if key not in case:
                self.problem(f"{cid}: missing {key!r}")
                return
        if not cid.startswith(case["category"] + "-"):
            self.problem(f"{cid}: id prefix does not match category {case['category']}")
        if case["site_kind"] not in gt_lib.SITE_KINDS:
            self.problem(f"{cid}: bad site_kind {case['site_kind']!r}")
        if not str(case["why"]).strip():
            self.problem(f"{cid}: empty why")
        rel = case["file"]
        data = self.data(rel)
        if data is None or not rel.endswith(".go"):
            self.problem(f"{cid}: Go file missing: {rel}")
            return
        lines = gt_lib.split_lines(data)
        if not 1 <= case["line"] <= len(lines):
            self.problem(f"{cid}: line {case['line']} out of range")
            return
        line = lines[case["line"] - 1]
        snippet = case["snippet"].encode("utf-8")
        count, at = 0, data.find(snippet)
        while at >= 0 and snippet:
            count += 1
            at = data.find(snippet, at + 1)
        if count != 1:
            self.problem(f"{cid}: snippet occurs {count} times: {case['snippet']!r}")
        start = line.find(snippet)
        if start < 0:
            self.problem(f"{cid}: snippet not on line {case['line']}")
            return
        token = case["token"].encode("utf-8")
        word = re.fullmatch(rb"[A-Za-z0-9_\x80-\xff]+", token) is not None
        positions, k = [], snippet.find(token)
        while k >= 0 and token:
            if not word or ident_bounded(snippet, k, token):
                positions.append(k)
            k = snippet.find(token, k + 1)
        occ = case["occurrence"]
        if not isinstance(occ, int) or occ < 1 or len(positions) < occ:
            self.problem(f"{cid}: token {case['token']!r} occurrence {occ} not in snippet")
            return
        col = start + positions[occ - 1] + 1
        if col != case["col"]:
            self.problem(f"{cid}: column {case['col']} but the token is at byte column {col}")
        offset = sum(len(l) + 1 for l in lines[:case["line"] - 1]) + col - 1
        if offset != case["offset"]:
            self.problem(f"{cid}: offset {case['offset']} but line and column give {offset}")
        gof = self.gofile(rel)
        site = gof.site_at(case["line"], case["col"]) if gof else None
        if site is None:
            self.problem(f"{cid}: no call site at {rel}:{case['line']}:{case['col']}")
        elif site.kind != case["site_kind"]:
            self.problem(f"{cid}: the site at {rel}:{case['line']}:{case['col']} is a {site.kind} site, "
                         f"case says {case['site_kind']}")
        elif site.name != case["token"]:
            self.problem(f"{cid}: the site's callee token is {site.name!r}, case says {case['token']!r}")
        self.check_expect(cid, case)

    def check_expect(self, cid: str, case: dict) -> None:
        """The env expectation, and the offline one when the case has it (master plan C10)."""
        self.check_one_expect(cid, case, case["expect"])
        if "expect_offline" not in case:
            self.problem(f"{cid}: case lacks 'expect_offline' (regenerate the manifest)")
        elif case["expect_offline"] is not None:
            self.check_one_expect(f"{cid} offline", case, case["expect_offline"])

    def check_one_expect(self, cid: str, case: dict, expect: dict) -> None:
        if not isinstance(expect, dict):
            self.problem(f"{cid}: expectation is not an object")
            return
        status = expect.get("status")
        if status not in gt_lib.STATUSES:
            self.problem(f"{cid}: bad status {status!r}")
            return
        for key in ("targets", "candidates", "runtime_targets", "distractors", "static_target", "open_world",
                    "external", "reason"):
            if key not in expect:
                self.problem(f"{cid}: expect lacks {key!r} (regenerate the manifest)")
                return
        targets = expect["targets"] or []
        candidates = expect["candidates"] or []
        for slot in ("targets", "candidates", "runtime_targets", "distractors"):
            for item in expect[slot] or []:
                self.check_target(f"{cid} {slot}", item)
        if expect["static_target"]:
            self.check_target(f"{cid} static_target", expect["static_target"])
            if status != "resolved":
                self.problem(f"{cid}: static_target only belongs to resolved cases")
        if expect["open_world"] not in (None, True, False):
            self.problem(f"{cid}: open_world must be true, false or null")
        if status == "resolved" and not targets:
            self.problem(f"{cid}: resolved case without targets")
        if status == "ambiguous" and (targets or not candidates):
            self.problem(f"{cid}: ambiguous case needs candidates and no targets")
        if status == "external":
            module = (expect["external"] or {}).get("module")
            if not isinstance(module, str) or not module or (module not in ("std", "C") and "." not in
                                                                module.split("/")[0]):
                self.problem(f"{cid}: external.module must be 'std', 'C' or a module path, not {module!r}")
            if candidates and not targets:
                self.problem(f"{cid}: external case with candidates (only a call on a peer interface method "
                             f"lists the in-repo implementers)")
            for item in targets:
                if "repo" not in item:
                    self.problem(f"{cid}: peer target {item.get('qualname')} lacks 'repo'")
                elif item["repo"] == self.repo_of(case["file"]):
                    self.problem(f"{cid}: peer target {item.get('qualname')} is in the case's own repo")
            if targets:
                for item in candidates:
                    if "repo" in item or self.repo_of(item.get("file", "")) != self.repo_of(case["file"]):
                        self.problem(f"{cid}: candidate {item.get('qualname')} of a peer call must lie in the "
                                     f"case's own repo")
        elif expect["external"]:
            self.problem(f"{cid}: external block on a {status} case")
        if status in ("builtin", "dynamic", "unresolved", "conversion") and (targets or candidates):
            self.problem(f"{cid}: {status} case must have no targets or candidates")
        if status == "conversion" and expect["runtime_targets"]:
            self.problem(f"{cid}: a conversion runs no function, so it has no runtime targets")
        reason = expect["reason"]
        if reason is not None and (status != "unresolved" or reason not in UNRESOLVED_REASONS):
            self.problem(f"{cid}: reason {reason!r} needs status unresolved and one of {UNRESOLVED_REASONS}")
        named = {(t["file"], t["qualname"]) for t in targets + candidates + (expect["runtime_targets"] or [])}
        if expect["static_target"]:
            named.add((expect["static_target"]["file"], expect["static_target"]["qualname"]))
        for item in expect["distractors"] or []:
            if (item.get("file"), item.get("qualname")) in named:
                self.problem(f"{cid}: distractor {item.get('qualname')} is also an expected target")

    # ------------------------------------------------------------- extras
    def check_xpr(self, pairs: list[dict], cases: dict[str, dict]) -> None:
        for pair in pairs:
            if pair.get("kind") == "line":
                ids = pair.get("cases", [])
                found = [cases.get(i) for i in ids]
                if len(ids) < 2 or not all(found):
                    self.problem(f"XPR pair {ids}: unknown case ids")
                    continue
                texts = {gt_lib.split_lines(self.data(c["file"]) or b"")[c["line"] - 1] for c in found}
                if len(texts) != 1:
                    self.problem(f"XPR pair {ids}: lines are not byte-identical")
                outcomes = {json.dumps([c["expect"]["status"], (c["expect"].get("external") or {}).get("module"),
                                        [(t["file"], t["qualname"]) for t in c["expect"].get("targets", [])]])
                            for c in found}
                if len(outcomes) != len(found):
                    self.problem(f"XPR pair {ids}: expectations do not diverge")
            elif pair.get("kind") == "file":
                files = pair.get("files", [])
                blobs = []
                for rel in files:
                    if self.data(rel) is None:
                        self.problem(f"XPR file pair: missing {rel}")
                        continue
                    blobs.append(self.data(rel))
                if len(files) < 2 or len(set(blobs)) != 1:
                    self.problem(f"XPR file pair {files}: files are not byte-identical")
            else:
                self.problem(f"XPR pair with unknown kind: {pair}")

    def fq_index(self) -> dict[str, list[tuple[str, object]]]:
        """package import path + "." + qualname -> [(file, decl)]."""
        if hasattr(self, "_fq"):
            return self._fq
        index: dict[str, list[tuple[str, object]]] = {}
        for rel in self.layout.go_files:
            gof = self.gofile(rel)
            pkg = self.layout.package_of(rel, gof.package if gof else None)
            if gof is None or pkg is None:
                continue
            for decl in gof.decls:
                if decl.qualname in ("<package>",):
                    continue
                index.setdefault(f"{pkg}.{decl.qualname}", []).append((rel, decl))
        self._fq = index
        return index

    def check_qa(self, path: str, fixture: str) -> int:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self.problem(f"qa_questions.json: {exc}")
            return 0
        if data.get("version") != 1 or data.get("fixture") != fixture or data.get("language") != "ja":
            self.problem("qa_questions.json: version/fixture/language header is wrong")
        index = self.fq_index()
        seen = set()
        questions = data.get("questions", [])
        for q in questions:
            qid = q.get("id", "?")
            if qid in seen:
                self.problem(f"{qid}: duplicate question id")
            seen.add(qid)
            if not JAPANESE.search(q.get("question", "")):
                self.problem(f"{qid}: question is not Japanese")
            check = q.get("check", {})
            ctype, subject = check.get("type"), check.get("subject", "")
            if ctype not in QA_TYPES:
                self.problem(f"{qid}: bad check type {ctype!r}")
                continue
            if q.get("match") not in QA_MATCHES:
                self.problem(f"{qid}: bad match {q.get('match')!r}")
            expected = q.get("expected")
            if not isinstance(expected, list):
                self.problem(f"{qid}: expected must be a list")
                continue
            if q.get("match") == "count_at_least":
                if len(expected) != 1 or not isinstance(expected[0], int):
                    self.problem(f"{qid}: count_at_least expects [<int>]")
                continue
            if ctype == "imports_of":
                if self.data(subject) is None:
                    self.problem(f"{qid}: subject file missing: {subject}")
                continue
            if ctype == "node_exists":
                if expected not in (["exists"], ["missing"]):
                    self.problem(f"{qid}: node_exists expects ['exists'] or ['missing']")
                if expected == ["exists"] and len(index.get(subject, [])) != 1:
                    self.problem(f"{qid}: subject {subject} resolves to {len(index.get(subject, []))} declarations")
                continue
            hits = index.get(subject, [])
            if len(hits) != 1:
                self.problem(f"{qid}: subject {subject} resolves to {len(hits)} declarations")
                continue
            rel, decl = hits[0]
            wants_class = ctype in ("methods_of", "class_file", "implementers_of", "embeds_of")
            if wants_class != (decl.kind == "class"):
                self.problem(f"{qid}: subject {subject} is a {decl.kind}, {ctype} needs a "
                             f"{'class' if wants_class else 'method'}")
            if ctype == "class_file":
                if expected != [rel]:
                    self.problem(f"{qid}: {subject} is declared in {rel}, expected {expected}")
            elif ctype == "methods_of":
                continue
            else:
                for fq in expected:
                    if len(index.get(fq, [])) != 1:
                        self.problem(f"{qid}: expected {fq} resolves to {len(index.get(fq, []))} declarations")
        return len(questions)

    def check_entries(self, path: str, cases: list[dict]) -> int:
        try:
            with open(path, encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self.problem(f"entries.json: {exc}")
            return 0
        if not isinstance(entries, dict):
            self.problem("entries.json: must be an object of entry name -> entry")
            return 0
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                self.problem(f"entry {name}: not an object")
                continue
            unknown = set(entry) - ENTRY_KEYS
            if unknown:
                self.problem(f"entry {name}: unknown keys {sorted(unknown)}")
            if not os.path.isdir(os.path.join(self.root, entry.get("dir", "\0"))):
                self.problem(f"entry {name}: dir missing: {entry.get('dir')}")
            has_cmd, has_test = "cmd" in entry, "test" in entry
            if has_cmd == has_test:
                self.problem(f"entry {name}: needs exactly one of cmd and test")
            if has_cmd:
                cmd = entry["cmd"]
                if not (isinstance(cmd, list) and len(cmd) >= 3 and cmd[:2] == ["go", "run"]
                        and all(isinstance(a, str) for a in cmd)):
                    self.problem(f"entry {name}: cmd must be [\"go\", \"run\", <package>, args...]")
            if has_test:
                pats = entry["test"]
                if not (isinstance(pats, list) and pats and all(isinstance(p, str) for p in pats)):
                    self.problem(f"entry {name}: test must be a non-empty list of package patterns")
            args = entry.get("test_args", [])
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or (args and not has_test):
                self.problem(f"entry {name}: test_args must be a list of strings on a test entry")
            env = entry.get("env", {})
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                self.problem(f"entry {name}: env must map strings to strings")
            else:
                for key in env:
                    if key.startswith("GO") and key != "GOWORK" or key in ("CGO_ENABLED", "PATH", "HOME"):
                        self.problem(f"entry {name}: env may not set {key}")
                gowork = env.get("GOWORK")
                if gowork not in (None, "off") and (os.path.isabs(gowork)
                                                    or not os.path.isfile(os.path.join(self.root, gowork))):
                    self.problem(f"entry {name}: GOWORK must be \"off\" or a fixture-relative work file, "
                                 f"not {gowork!r}")
            words = list(entry.get("cmd", [])) + list(entry.get("test", [])) + list(args) + list(env.values())
            if any("-mod=" in w for w in words if isinstance(w, str)):
                self.problem(f"entry {name}: -mod flags are not allowed")
            timeout = entry.get("timeout", 600)
            if not isinstance(timeout, int) or timeout <= 0:
                self.problem(f"entry {name}: timeout must be a positive integer (seconds)")
        for case in cases:
            if case.get("entry") is not None and case["entry"] not in entries:
                self.problem(f"{case['id']}: unknown entry {case['entry']!r}")
        return len(entries)

    def check_features(self, path: str) -> int:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self.problem(f"features.json: {exc}")
            return 0
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list) or not features:
            self.problem("features.json: needs a non-empty features list")
            return 0
        names, owner = set(), {}
        for feat in features:
            name = feat.get("name") if isinstance(feat, dict) else None
            if not name or name in names:
                self.problem(f"features.json: missing or duplicate name {name!r}")
            names.add(name)
            for rel in feat.get("files", []) if isinstance(feat, dict) else []:
                if self.data(rel) is None:
                    self.problem(f"features.json: {name}: file missing: {rel}")
                if rel in owner:
                    self.problem(f"features.json: {rel} is in {owner[rel]} and {name}")
                owner[rel] = name
        return len(features)

    def derived_edges(self, cases: list[dict]) -> set[tuple]:
        out = set()
        for case in cases:
            expect = case["expect"]
            if expect["status"] != "external" or case["site_kind"] not in gt_lib.SITE_KINDS:
                continue
            src_repo = self.repo_of(case["file"])
            for item in expect.get("targets") or []:
                dst_repo = item.get("repo")
                if not src_repo or not dst_repo or src_repo == dst_repo:
                    continue
                gof = self.gofile(case["file"])
                caller = gof.enclosing(case["line"], case["col"]) if gof else None
                if caller is None:
                    continue
                out.add(("CrossRepoCalls", None, f"{src_repo}:{self.in_repo(case['file'])}:{caller}",
                         f"{dst_repo}:{self.in_repo(item['file'])}:{item['qualname']}"))
        return out

    def in_repo(self, rel: str) -> str:
        repo = self.repo_of(rel)
        base = next(r["dir"] for r in self.repos if r["name"] == repo)
        return rel[len(base) + 1:] if base else rel

    def check_edges(self, edges: list[dict], cases: list[dict]) -> None:
        dirs = {r["name"]: r["dir"] for r in self.repos}
        given = {(e.get("table"), e.get("kind"), e.get("source"), e.get("target")) for e in edges
                 if e.get("table") == "CrossRepoCalls"}
        derived = self.derived_edges(cases)
        for edge in sorted(derived - given, key=str):
            self.problem(f"CROSS_REPO_EDGES lacks an edge implied by the peer cases: {edge}")
        for edge in sorted(given - derived, key=str):
            self.problem(f"CROSS_REPO_EDGES has a CrossRepoCalls edge no peer case implies: {edge}")
        for edge in edges:
            table = edge.get("table")
            if table not in EDGE_TABLES:
                self.problem(f"cross-repo edge with bad table: {edge}")
                continue
            if table == "CrossRepoRef" and edge.get("kind") not in REF_KINDS:
                self.problem(f"CrossRepoRef with bad kind: {edge}")
            for end in ("source", "target"):
                parts = str(edge.get(end, "")).split(":", 2)
                if parts[0] not in dirs or len(parts) < 2:
                    self.problem(f"cross-repo edge {end} is not <repo>:<path>[:<qualname>]: {edge}")
                    continue
                if table == "CrossRepoImports" and end == "target":
                    self.check_package_label(edge, parts[1], dirs[parts[0]])
                    continue
                rel = os.path.normpath(os.path.join(dirs[parts[0]], parts[1])).replace(os.sep, "/")
                if table == "CrossRepoImports":
                    if not os.path.isfile(os.path.join(self.root, rel)):
                        self.problem(f"cross-repo edge {end} missing: {edge[end]}")
                    continue
                gof = self.gofile(rel)
                if gof is None or len(parts) != 3 or gof.decl(parts[2]) is None:
                    self.problem(f"cross-repo edge {end} not declared: {edge[end]}")

    def check_edge_sources(self, edges: list[dict], source: list[dict]) -> None:
        """cases.py CROSS_REPO_EDGES is the only source of record (master plan C10): the manifest
        and ground_truth/expected_cross_repo_edges.json, which evaluate reads, must equal it."""
        if [dict(e) for e in source] != edges:
            self.problem("cross_repo_edges in manifest.json differ from cases.py CROSS_REPO_EDGES (regenerate it)")
        path = os.path.join(self.root, "ground_truth", "expected_cross_repo_edges.json")
        if not os.path.isfile(path):
            if source:
                self.problem("ground_truth/expected_cross_repo_edges.json is missing (regenerate the manifest)")
            return
        if not source:
            self.problem("ground_truth/expected_cross_repo_edges.json exists but cases.py has no CROSS_REPO_EDGES")
            return
        try:
            with open(path, encoding="utf-8") as handle:
                written = json.load(handle).get("edges")
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            self.problem(f"expected_cross_repo_edges.json: {exc}")
            return
        if written != [dict(e) for e in source]:
            self.problem("expected_cross_repo_edges.json differs from cases.py CROSS_REPO_EDGES (regenerate the "
                         "manifest; never edit the file)")

    def check_class_edges(self, manifest: dict, module) -> int:
        """cases.py CLASS_EDGES (spec R50): the exact ClassImplements / ClassEmbeds set a fixture
        pins. The manifest carries it as "class_edges" only when cases.py defines it. Returns the
        number of edges."""
        source = getattr(module, "CLASS_EDGES", None)
        edges = manifest.get("class_edges")
        if (source is None) != (edges is None) or (source is not None and [dict(e) for e in source] != edges):
            self.problem("class_edges in manifest.json differ from cases.py CLASS_EDGES (regenerate it)")
        if not isinstance(edges, list):
            return 0
        dirs = {r["name"]: r["dir"] for r in self.repos}
        seen: set[tuple] = set()
        for edge in edges:
            flag = CLASS_EDGE_FLAGS.get(edge.get("table")) if isinstance(edge, dict) else None
            if flag is None:
                self.problem(f"class edge with bad table: {edge}")
                continue
            other = "pointer" if flag == "pointer_receiver" else "pointer_receiver"
            if not isinstance(edge.get(flag), bool) or other in edge:
                self.problem(f"class edge needs a bool {flag!r} and no {other!r}: {edge}")
            extra = sorted(set(edge) - {"table", "source", "target", flag, other, "modes"})
            if extra:
                self.problem(f"class edge has unknown keys {extra}: {edge}")
            modes = edge.get("modes")
            if modes is not None and (not isinstance(modes, list) or not modes or len(set(modes)) != len(modes)
                                      or not set(modes) <= set(EDGE_MODES)):
                self.problem(f"class edge modes must be a non-empty list of distinct {EDGE_MODES}: {edge}")
            key = (edge.get("table"), edge.get("source"), edge.get("target"))
            if key in seen:
                self.problem(f"duplicate class edge: {edge}")
            seen.add(key)
            if edge.get("source") == edge.get("target"):
                self.problem(f"class edge from a type to itself: {edge}")
            for end in ("source", "target"):
                parts = str(edge.get(end, "")).split(":", 2)
                if len(parts) != 3 or parts[0] not in dirs:
                    self.problem(f"class edge {end} is not <repo>:<relpath>:<qualname>: {edge}")
                    continue
                rel = os.path.normpath(os.path.join(dirs[parts[0]], parts[1])).replace(os.sep, "/")
                gof = self.gofile(rel)
                decl = gof.decl(parts[2]) if gof is not None and parts[2] != "<package>" else None
                if decl is None or decl.kind != "class":
                    self.problem(f"class edge {end} names no type declared in its file: {edge[end]}")
        return len(edges)

    def check_package_label(self, edge: dict, label: str, base: str) -> None:
        """A package endpoint "<dir>|<clause name>" (master plan C10): the directory exists and
        one of its non-test .go files declares that package clause."""
        directory, sep, clause = label.partition("|")
        if not sep or not clause:
            self.problem(f"cross-repo edge target is not <repo>:<dir>|<clause name>: {edge}")
            return
        rel = os.path.normpath(os.path.join(base, directory)).replace(os.sep, "/")
        path = os.path.join(self.root, rel)
        if not os.path.isdir(path):
            self.problem(f"cross-repo edge target missing: {edge['target']}")
            return
        clauses = set()
        for name in sorted(os.listdir(path)):
            if name.endswith(".go") and not name.endswith("_test.go") and os.path.isfile(os.path.join(path, name)):
                gof = self.gofile(f"{rel}/{name}")
                if gof is not None and gof.package:
                    clauses.add(gof.package)
        if clause not in clauses:
            self.problem(f"cross-repo edge target {edge['target']}: {rel} declares package "
                         f"{', '.join(sorted(clauses)) or 'nothing'}, not {clause}")

    def apply_exceptions(self, exceptions: list[str]) -> list[str]:
        used = set()
        for text in self.layout_problems:
            match = next((e for e in exceptions if e in text), None)
            if match is None:
                self.problem(text)
            else:
                used.add(match)
        for e in exceptions:
            if e not in used:
                self.problem(f"LAYOUT_EXCEPTIONS entry matches no layout problem: {e!r}")
        return sorted(used)

    # ------------------------------------------------------------- main
    def run(self) -> int:
        manifest_path = os.path.join(self.root, "ground_truth", "manifest.json")
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"MISMATCH: manifest.json unreadable: {exc}")
            return 1
        fixture = os.path.basename(self.root)
        if manifest.get("version") != 1 or manifest.get("fixture") != fixture:
            self.problem("manifest.json: version or fixture name is wrong")
        if manifest.get("go") != GO_VERSION or manifest.get("build") != BUILD:
            self.problem(f"manifest.json: go must be {GO_VERSION} and build {BUILD}")
        spec = importlib.util.spec_from_file_location(
            "gt_cases_check", os.path.join(self.root, "ground_truth", "cases.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source_ids = [c["id"] for c in module.CASES]
        manifest_ids = [c["id"] for c in manifest.get("cases", [])]
        for cid in sorted({i for i in source_ids if source_ids.count(i) > 1}):
            self.problem(f"cases.py repeats id {cid}")
        for cid in sorted(set(source_ids) - set(manifest_ids)):
            self.problem(f"cases.py case {cid} is not in manifest.json (regenerate it)")
        for cid in sorted(set(manifest_ids) - set(source_ids)):
            self.problem(f"manifest.json case {cid} is not in cases.py")
        by_source = {c["id"]: c for c in module.CASES}
        for case in manifest.get("cases", []):
            src = by_source.get(case.get("id"))
            if src is not None and any(src.get(k) != case.get(k) for k in
                                       ("file", "snippet", "token", "site_kind", "entry", "why")):
                self.problem(f"{case['id']}: manifest.json differs from cases.py (regenerate it)")
        self.repos = []
        for repo in manifest.get("repos") or [{"name": fixture, "dir": "."}]:
            base = os.path.normpath(repo.get("dir") or ".").replace(os.sep, "/")
            self.repos.append({"name": repo["name"], "dir": "" if base == "." else base})
        self.layout = gt_lib.Layout(self.root)
        self.check_layout()
        by_id = {}
        sites: dict[tuple, str] = {}
        for case in manifest.get("cases", []):
            self.check_case(case)
            by_id[case.get("id")] = case
            site = (case.get("file"), case.get("line"), case.get("col"), case.get("site_kind"))
            if site in sites:
                self.problem(f"{case.get('id')}: same site as {sites[site]} ({site[0]}:{site[1]}:{site[2]} {site[3]})")
            sites.setdefault(site, case.get("id"))
        pin_keys = {(p["qualname"], p["file"]) for p in getattr(module, "DECL_PINS", [])}
        if pin_keys != {(p.get("qualname"), p.get("file")) for p in manifest.get("decl_pins", [])}:
            self.problem("decl_pins in manifest.json differ from cases.py (regenerate it)")
        for pin in manifest.get("decl_pins", []):
            self.check_target(f"pin {pin.get('qualname')}", pin)
        self.check_xpr(manifest.get("xpr_pairs", []), by_id)
        self.check_edge_sources(manifest.get("cross_repo_edges", []), list(getattr(module, "CROSS_REPO_EDGES", [])))
        self.check_edges(manifest.get("cross_repo_edges", []), manifest.get("cases", []))
        class_edges = self.check_class_edges(manifest, module)
        qa_path = os.path.join(self.root, "ground_truth", "qa_questions.json")
        questions = self.check_qa(qa_path, fixture) if os.path.isfile(qa_path) else 0
        feat_path = os.path.join(self.root, "ground_truth", "features.json")
        features = self.check_features(feat_path) if os.path.isfile(feat_path) else 0
        entries_path = os.path.join(self.root, "entries.json")
        if os.path.isfile(entries_path):
            entries = self.check_entries(entries_path, manifest.get("cases", []))
        else:
            entries = 0
            self.problem("entries.json is missing")
        if not os.path.isfile(os.path.join(self.root, "README.md")):
            self.problem("README.md is missing")
        expected = self.apply_exceptions(list(getattr(module, "LAYOUT_EXCEPTIONS", [])))
        if self.problems:
            for text in self.problems:
                print("MISMATCH:", text)
            print(f"{len(self.problems)} problem(s)")
            return 1
        print(f"ground truth OK: {fixture}: {len(manifest_ids)} cases, {len(manifest.get('decl_pins', []))} pins, "
              f"{len(self.layout.modules)} modules, {len(self.layout.workspaces)} go.work, {questions} questions, "
              f"{features} features, {entries} entries, {len(manifest.get('cross_repo_edges', []))} cross-repo "
              f"edges, " + (f"{class_edges} class edges, " if "class_edges" in manifest else "")
              + f"{len(expected)} expected layout problems")
        return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_ground_truth.py <fixture-dir>", file=sys.stderr)
        return 2
    return Checker(argv[1]).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
