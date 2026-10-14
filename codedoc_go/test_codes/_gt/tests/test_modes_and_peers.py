"""Per-mode expectations, conversions, peer candidates, package labels and work-file entries
(master plan C10 rulings of 2026-10-13/14)."""
import json
import os
import shutil

import check_ground_truth as cg
import generate_manifest as gm
import gt_lib
import run_oracle as ro
from conftest import GT, write
from test_generate_manifest import manifest_of, replace_in_cases


def check(root, capsys):
    rc = cg.main(["check_ground_truth.py", root])
    return rc, capsys.readouterr().out


def generated(root):
    assert gm.main(["generate_manifest.py", root]) == 0
    return root


def test_case_carries_an_offline_expectation_with_the_distractors():
    t = gt_lib.target("F", "example.com/m", "method", "a.go")
    d = gt_lib.target("G", "example.com/m", "method", "a.go")
    c = gt_lib.case("X-001", "a.go", "F()", "F", "call", gt_lib.plain("unresolved", "toolchain"), "why",
                    entry=None, distractors=[d], offline=gt_lib.resolved(t))
    assert c["expect"]["status"] == "unresolved" and c["expect"]["distractors"] == [d]
    assert c["expect_offline"]["status"] == "resolved" and c["expect_offline"]["targets"] == [t]
    assert c["expect_offline"]["distractors"] == [d]
    assert gt_lib.case("X-002", "a.go", "F()", "F", "call", gt_lib.resolved(t), "why", entry=None)["expect_offline"] \
        is None


def test_conversion_status_and_peer_candidates():
    assert "conversion" in gt_lib.STATUSES
    conv = gt_lib.plain("conversion")
    assert (conv["status"], conv["targets"], conv["candidates"], conv["reason"]) == ("conversion", [], [], None)
    method = gt_lib.target("Clock.Now", "example.com/p", "method", "p/clock/clock.go", repo="p")
    impl = gt_lib.target("Sys.Now", "example.com/q", "method", "q/sys.go")
    e = gt_lib.peer(method, module="example.com/p", candidates=[impl], runtime=[impl], open_world=False)
    assert (e["status"], e["targets"], e["candidates"], e["runtime_targets"]) == ("external", [method], [impl], [impl])
    assert e["external"] == {"module": "example.com/p"} and e["open_world"] is False


def test_manifest_anchors_the_offline_expectation(tiny):
    replace_in_cases(tiny, 'resolved(GREET), "pointer receiver method", entry=RUN',
                     'resolved(GREET), "pointer receiver method", entry=RUN, '
                     'offline=plain("unresolved", "missing-module", runtime=[GREET])')
    m = manifest_of(generated(tiny))
    cases = {c["id"]: c for c in m["cases"]}
    off = cases["CALL-001"]["expect_offline"]
    assert off["status"] == "unresolved" and off["reason"] == "missing-module"
    assert [(t["qualname"], t["line"], t["col"]) for t in off["runtime_targets"]] == [("Greeter.Greet", 9, 19)]
    assert off["static_target"] is None and off["external"] is None and off["distractors"] == []
    assert cases["CALL-002"]["expect_offline"] is None


def test_offline_expectation_is_checked_like_expect(tiny, capsys):
    replace_in_cases(tiny, 'resolved(GREET), "pointer receiver method", entry=RUN',
                     'resolved(GREET), "pointer receiver method", entry=RUN, offline=plain("unresolved", "missing-module")')
    rc, out = check(generated(tiny), capsys)
    assert rc == 0, out
    replace_in_cases(tiny, 'offline=plain("unresolved", "missing-module")',
                     'offline=plain("dynamic", "missing-module")')
    replace_in_cases(tiny, 'distractors=[GREET])', 'distractors=[GREET], offline=resolved(GREET))')
    rc, out = check(generated(tiny), capsys)
    assert rc == 1
    assert "CALL-001 offline: reason 'missing-module' needs status unresolved" in out
    assert "IFC-001 offline: distractor Greeter.Greet is also an expected target" in out


def test_stale_manifest_without_expect_offline(tiny, capsys):
    generated(tiny)
    path = os.path.join(tiny, "ground_truth", "manifest.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    for c in data["cases"]:
        del c["expect_offline"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    rc, out = check(tiny, capsys)
    assert rc == 1 and "CALL-001: case lacks 'expect_offline' (regenerate the manifest)" in out


CONV_FIXTURE = {
    "go.mod": "module example.com/conv\n\ngo 1.26\n",
    "c.go": "package c\n\ntype Yen int64\n\nfunc F(n int) int64 {\n\treturn int64(Yen(n))\n}\n",
    "README.md": "x\n",
    "entries.json": "{}\n",
    "ground_truth/cases.py": (
        "from gt_lib import case, plain, target\n"
        "M = 'example.com/conv'\n"
        "CASES = [case('CNV-001', 'c.go', 'int64(Yen(n))', 'int64', 'call', plain('conversion'), 'builtin type', entry=None),\n"
        "         case('CNV-002', 'c.go', 'Yen(n)', 'Yen', 'call', plain('conversion'), 'named type', entry=None)]\n"),
}


def test_conversion_cases(tmp_path, capsys):
    root = str(tmp_path / "conv")
    write(root, CONV_FIXTURE)
    shutil.copytree(GT, tmp_path / "_gt", ignore=shutil.ignore_patterns("tests", "oracle", "__pycache__"))
    rc, out = check(generated(root), capsys)
    assert rc == 0, out
    replace_in_cases(root, "plain('conversion'), 'named type'",
                     "{**plain('conversion'), 'targets': [target('Yen', M, 'class', 'c.go')]}, 'named type'")
    rc, out = check(generated(root), capsys)
    assert rc == 1 and "CNV-002: conversion case must have no targets or candidates" in out


PEER_CASES = """\
from gt_lib import case, peer, target

REPOS = [{"name": "p", "dir": "p"}, {"name": "q", "dir": "q"}]
P, Q = "example.com/mr/p", "example.com/mr/q"
NOW = target("Clock.Now", P, "method", "p/clock/clock.go", repo="p")
HELLO = target("Hello", P, "method", "p/clock/clock.go", repo="p")
SYS = target("Sys.Now", Q, "method", "q/sys.go")
CASES = [
    case("PEER-001", "q/sys.go", "clock.Hello()", "Hello", "call", peer(HELLO, module=P), "peer function", entry=None),
    case("PEER-002", "q/sys.go", "c.Now()", "Now", "call", peer(NOW, module=P, candidates=[SYS], runtime=[SYS]),
         "peer interface method with an in-repo implementer", entry=None),
]
CROSS_REPO_EDGES = [
    {"table": "CrossRepoCalls", "kind": None, "source": "q:sys.go:Use", "target": "p:clock/clock.go:Hello"},
    {"table": "CrossRepoCalls", "kind": None, "source": "q:sys.go:Stamp", "target": "p:clock/clock.go:Clock.Now"},
    {"table": "CrossRepoImports", "kind": None, "source": "q:sys.go", "target": "p:clock|clock",
     "import_path": "example.com/mr/p/clock"},
]
"""

PEER_FIXTURE = {
    "p/go.mod": "module example.com/mr/p\n\ngo 1.26\n",
    "p/clock/clock.go": "package clock\n\ntype Clock interface {\n\tNow() int\n}\n\nfunc Hello() string {\n\treturn \"hi\"\n}\n",
    "q/go.mod": "module example.com/mr/q\n\ngo 1.26\n\nrequire example.com/mr/p v0.0.0\n\nreplace example.com/mr/p => ../p\n",
    "q/sys.go": ("package q\n\nimport \"example.com/mr/p/clock\"\n\ntype Sys struct{}\n\nfunc (Sys) Now() int {\n\treturn 1\n}\n\n"
                 "func Use() string {\n\treturn clock.Hello()\n}\n\nfunc Stamp(c clock.Clock) int {\n\treturn c.Now()\n}\n"),
    "q/work/q.work": "go 1.26\n\nuse (\n\t../../p\n\t..\n)\n",
    "README.md": "x\n",
    "entries.json": json.dumps({"q": {"dir": "q", "cmd": ["go", "run", "."], "env": {"GOWORK": "q/work/q.work"}}}),
    "ground_truth/cases.py": PEER_CASES,
}


def peer_fixture(tmp_path):
    root = str(tmp_path / "mr")
    write(root, PEER_FIXTURE)
    shutil.copytree(GT, tmp_path / "_gt", ignore=shutil.ignore_patterns("tests", "oracle", "__pycache__"))
    return root


def test_peer_candidates_package_labels_and_work_files(tmp_path, capsys):
    root = peer_fixture(tmp_path)
    rc, out = check(generated(root), capsys)
    assert rc == 0, out


def test_peer_candidate_and_label_problems(tmp_path, capsys):
    root = peer_fixture(tmp_path)
    replace_in_cases(root, 'SYS = target("Sys.Now", Q, "method", "q/sys.go")',
                     'SYS = target("Sys.Now", Q, "method", "q/sys.go", repo="q")')
    replace_in_cases(root, 'peer(HELLO, module=P)', '{**peer(HELLO, module=P), "targets": [], "candidates": [HELLO]}')
    replace_in_cases(root, '"target": "p:clock|clock"', '"target": "p:clock"')
    write(root, {"entries.json": json.dumps({"q": {"dir": "q", "cmd": ["go", "run", "."],
                                                   "env": {"GOWORK": "q/work/missing.work"}},
                                             "abs": {"dir": "q", "cmd": ["go", "run", "."],
                                                     "env": {"GOWORK": "/etc/go.work"}}})})
    rc, out = check(generated(root), capsys)
    assert rc == 1
    for text in ["PEER-002: candidate Sys.Now of a peer call must lie in the case's own repo",
                 "PEER-001: external case with candidates (only a call on a peer interface method",
                 "cross-repo edge target is not <repo>:<dir>|<clause name>",
                 "entry q: GOWORK must be \"off\" or a fixture-relative work file, not 'q/work/missing.work'",
                 "entry abs: GOWORK must be \"off\" or a fixture-relative work file, not '/etc/go.work'"]:
        assert text in out, text


def test_package_label_names_the_clause(tmp_path, capsys):
    root = peer_fixture(tmp_path)
    replace_in_cases(root, '"target": "p:clock|clock"', '"target": "p:clock|clocks"')
    rc, out = check(generated(root), capsys)
    assert rc == 1 and "cross-repo edge target p:clock|clocks: p/clock declares package clock, not clocks" in out


def test_relative_gowork_is_resolved_in_the_copy():
    entry = {"env": {"GOWORK": "ground_truth/oracle/web.work"}}
    env = ro.entry_env("/go/bin/go", "/s/fx", entry, "/h", "/s/fx/copy")
    assert env["GOWORK"] == "/s/fx/copy/ground_truth/oracle/web.work"
    assert ro.entry_env("/go/bin/go", "/s/fx", {"env": {"GOWORK": "off"}}, "/h", "/s/fx/copy")["GOWORK"] == "off"
    assert ro.entry_env("/go/bin/go", "/s/fx", {"env": {"GOWORK": "/abs/w.work"}}, "/h", "/c")["GOWORK"] == "/abs/w.work"
    assert ro.entry_env("/go/bin/go", "/s/fx", entry, "/h")["GOWORK"] == "ground_truth/oracle/web.work"


def test_edges_file_is_written_from_cross_repo_edges(tmp_path, capsys):
    root = peer_fixture(tmp_path)
    assert gm.main(["generate_manifest.py", root]) == 0
    assert "expected_cross_repo_edges.json: 3 edges" in capsys.readouterr().out
    with open(os.path.join(root, "ground_truth", "expected_cross_repo_edges.json"), encoding="utf-8") as handle:
        doc = json.load(handle)
    assert doc["edges"] == manifest_of(root)["cross_repo_edges"] and len(doc["edges"]) == 3
    assert doc["counts"] == {"CrossRepoCalls": 2, "CrossRepoImports": 1}
    assert doc["labels"]["package"] == "<repo>:<repo-relative directory>|<package clause name>"
    assert "CROSS_REPO_EDGES" in doc["source"] and doc["fixture"] == "mr"


def test_edges_file_must_equal_the_source_of_record(tmp_path, capsys):
    root = generated(peer_fixture(tmp_path))
    path = os.path.join(root, "ground_truth", "expected_cross_repo_edges.json")
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    doc["edges"].pop()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    rc, out = check(root, capsys)
    assert rc == 1 and "expected_cross_repo_edges.json differs from cases.py CROSS_REPO_EDGES" in out
    os.remove(path)
    rc, out = check(root, capsys)
    assert rc == 1 and "ground_truth/expected_cross_repo_edges.json is missing (regenerate the manifest)" in out
    replace_in_cases(root, '"target": "p:clock|clock"', '"target": "p:clock|clock", "note": 1')
    rc, out = check(root, capsys)
    assert rc == 1 and "cross_repo_edges in manifest.json differ from cases.py CROSS_REPO_EDGES" in out


def test_stray_edges_file_without_cross_repo_edges(tiny, capsys):
    write(tiny, {"ground_truth/expected_cross_repo_edges.json": json.dumps({"edges": []})})
    rc, out = check(generated(tiny), capsys)
    assert rc == 1 and "expected_cross_repo_edges.json exists but cases.py has no CROSS_REPO_EDGES" in out
