"""gt_lib's Go reader against goracle's go/parser table: the same C3 names,
interface methods and call sites (spec §4.3), position for position."""
import json
import os
import subprocess

import pytest

import gt_lib
from conftest import GT

TREES = [os.path.join(GT, "oracle", "testdata", name) for name in ("probe3", "constructs")]


def goracle_bin(go_bin, tmp_path):
    out = tmp_path / "goracle"
    env = {"PATH": os.path.dirname(go_bin) + ":/usr/bin:/bin", "HOME": os.environ.get("HOME", str(tmp_path)),
           "GOENV": "off", "GOTOOLCHAIN": "local", "GOFLAGS": "-modcacherw", "GOWORK": "off", "GOPROXY": "off"}
    if os.environ.get("GOCACHE"):
        env["GOCACHE"] = os.environ["GOCACHE"]
    subprocess.run([go_bin, "build", "-buildvcs=false", "-o", str(out), "."], cwd=os.path.join(GT, "oracle"), env=env,
                   check=True, timeout=600)
    return str(out)


@pytest.mark.go
@pytest.mark.parametrize("tree", TREES, ids=os.path.basename)
def test_python_reader_matches_goracle(go_bin, tmp_path, tree):
    compare(go_bin, tmp_path, tree)


@pytest.mark.go
def test_python_reader_matches_goracle_on_callee_shapes(go_bin, tmp_path):
    from test_gt_lib import SHAPES
    tree = tmp_path / "shapes"
    (tree / "p").mkdir(parents=True)
    (tree / "go.mod").write_text("module example.com/m\n\ngo 1.26\n")
    (tree / "p" / "p.go").write_text(SHAPES)
    compare(go_bin, tmp_path, str(tree))


def compare(go_bin, tmp_path, tree):
    table = tmp_path / "table.jsonl"
    subprocess.run([goracle_bin(go_bin, tmp_path), "scan", "--root", tree, "--out", str(table)], check=True, timeout=600)
    funcs, imethods, sites, files = {}, {}, {}, []
    for line in table.read_text().splitlines():
        rec = json.loads(line)
        if rec["kind"] == "file" and rec["status"] == "instrumented":
            files.append(rec["file"])
        elif rec["kind"] == "func":
            funcs.setdefault(rec["file"], set()).add((rec["qualname"], rec["line"], rec["col"]))
        elif rec["kind"] == "imethod":
            imethods.setdefault(rec["file"], set()).add((rec["qualname"], rec["line"], rec["col"]))
        elif rec["kind"] == "site" and rec["name"] and rec["form"] != "callresult":
            sites.setdefault(rec["file"], set()).add((rec["line"], rec["col"], rec["name"], rec["stmt"]))
    assert files
    for rel in files:
        gof = gt_lib.GoFile(open(os.path.join(tree, rel), "rb").read(), rel)
        py_funcs = {(d.qualname, d.line, d.col) for d in gof.decls
                    if d.kind == "method" and d.member not in ("interface_method", "var_init")}
        py_imethods = {(d.qualname, d.line, d.col) for d in gof.decls if d.member == "interface_method"}
        py_sites = {(s.line, s.col, s.name, s.kind) for s in gof.sites if s.name}
        assert py_funcs == funcs.get(rel, set()), rel
        assert py_imethods == imethods.get(rel, set()), rel
        assert py_sites == sites.get(rel, set()), rel
