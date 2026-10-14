"""Scanning with the go command's skip rules, symlink dedupe and ownership (spec §3.1)."""
from __future__ import annotations

import os

import pytest

from graph_generator import config, go_units
from graph_generator.go_units import scan_go_files

import go_worlds

LAYOUT = {
    "go.mod": "module example.com/r\n\ngo 1.26\n\nignore (\n\t./node_modules\n\tstatic\n)\n",
    "main.go": "package main\n",
    "a/a.go": "package a\n",
    "a/a_test.go": "package a\n",
    "a/a_windows.go": "package a\n",               # build-excluded files are still scanned
    "a/_u.go": "package a\n",
    "a/.d.go": "package a\n",
    "a/notes.txt": "x\n",
    "a/testdata/t.go": "package t\n",
    "a/testdata/deep/t2.go": "package t\n",
    "a/_hidden/h.go": "package h\n",
    "a/.dot/d.go": "package d\n",
    "vendor/modules.txt": "# example.com/dep v1.0.0\n## explicit\nexample.com/dep\n",
    "vendor/example.com/dep/dep.go": "package dep\n",
    "vendor/v.go": "package vendor\n",                # the vendor dir itself is a package
    "pkg/vendor/w.go": "package vendor\n",
    "pkg/vendor/inner/i.go": "package inner\n",
    "node_modules/x/x.go": "package x\n",
    "node_modules/m/go.mod": "module example.com/npm\n",
    "static/s.go": "package s\n",
    "web/static/s2.go": "package s\n",
    "staticx/k.go": "package k\n",                   # "static" matches whole elements only
    "nested/go.mod": "module example.com/r/nested\n\ngo 1.26\n",
    "nested/n.go": "package nested\n",
    "nested/static/ns.go": "package ns\n",           # the parent's ignore does not apply
    "nested/testdata/x.go": "package x\n",
}


def _layout(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", LAYOUT)
    os.symlink("a/a.go", os.path.join(root, "link.go"))
    os.symlink(str(tmp_path / "elsewhere.go"), os.path.join(root, "out.go"))
    (tmp_path / "elsewhere.go").write_text("package main\n")
    os.symlink("a", os.path.join(root, "linkdir"))
    os.symlink("a/testdata/t.go", os.path.join(root, "to_testdata.go"))
    os.symlink("missing.go", os.path.join(root, "broken.go"))
    return root


def test_skip_rules_and_counts(tmp_path):
    root = _layout(tmp_path)
    files, skipped = scan_go_files(root)
    assert files == [
        "a/a.go", "a/a_test.go", "a/a_windows.go", "main.go", "nested/n.go",
        "nested/static/ns.go", "pkg/vendor/w.go", "staticx/k.go", "vendor/v.go"]
    assert skipped == {
        "testdata": 3, "underscore": 2, "dot": 1, "vendor": 2, "ignored": 3,
        "symlink": 1, "symlink-unscanned": 3, "symlink-dir": 1}


def test_layout_records_modules_workfiles_and_owners(tmp_path):
    root = _layout(tmp_path)
    lay = go_units._walk(os.path.realpath(root), ())
    assert lay.module_roots == ["", "nested"]
    assert lay.owner["nested/static/ns.go"] == "nested"
    assert lay.owner["a/a.go"] == ""
    assert lay.workfiles == []


def test_files_under_no_module_have_no_owner(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "loose/x.go": "package loose\n",
        "m/go.mod": "module example.com/m\n",
        "m/y.go": "package m\n"})
    lay = go_units._walk(os.path.realpath(root), ())
    assert lay.files == ["loose/x.go", "m/y.go"]
    assert lay.owner == {"m/y.go": "m"}


def test_output_dir_inside_repo_is_pruned(tmp_path, monkeypatch):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/r\n",
        "r.go": "package r\n",
        "out/go/modcache/example.com/dep@v1.0.0/go.mod": "module example.com/dep\n",
        "out/go/modcache/example.com/dep@v1.0.0/dep.go": "package dep\n",
        "extra/e.go": "package e\n"})
    monkeypatch.setattr(config, "OUTPUT_DIR", os.path.join(root, "out"), raising=False)
    files, _ = scan_go_files(root, exclude=[os.path.join(root, "extra")])
    assert files == ["r.go"]
    assert go_units._walk(os.path.realpath(root), ()).module_roots == [""]


def test_go_work_use_dirs_are_scanned_even_under_skip_names(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t.\n\t./_tools\n\t./sub/testdata/mod\n)\n",
        "go.mod": "module example.com/r\n",
        "r.go": "package r\n",
        "_tools/go.mod": "module example.com/tools\n",
        "_tools/t.go": "package tools\n",
        "_tools/_skip/s.go": "package s\n",
        "_other/o.go": "package o\n",
        "sub/testdata/mod/go.mod": "module example.com/tdmod\n",
        "sub/testdata/mod/m.go": "package mod\n",
        "sub/testdata/other.go": "package other\n"})
    lay = go_units._walk(os.path.realpath(root), ())
    assert lay.files == ["_tools/t.go", "r.go", "sub/testdata/mod/m.go"]
    assert lay.module_roots == ["", "_tools", "sub/testdata/mod"]
    assert lay.workfiles == ["go.work"]
    assert lay.skipped == {"underscore": 2, "testdata": 1}


def test_non_utf8_names_are_skipped(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {"go.mod": "module example.com/r\n",
                                                     "ok.go": "package r\n"})
    bad = os.path.join(os.fsencode(root), b"bad\xff.go")
    with open(bad, "wb") as f:
        f.write(b"package r\n")
    files, skipped = scan_go_files(root)
    assert files == ["ok.go"]
    assert skipped == {"non-utf8": 1}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_unreadable_files_are_skipped(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {"go.mod": "module example.com/r\n",
                                                     "ok.go": "package r\n", "no.go": "package r\n"})
    os.chmod(os.path.join(root, "no.go"), 0)
    try:
        files, skipped = scan_go_files(root)
    finally:
        os.chmod(os.path.join(root, "no.go"), 0o644)
    assert files == ["ok.go"]
    assert skipped == {"unreadable": 1}


def test_repo_reached_through_a_symlink(tmp_path):
    root = go_worlds.write_tree(tmp_path / "real", {"go.mod": "module example.com/r\n",
                                                     "r.go": "package r\n"})
    os.symlink(root, tmp_path / "alias")
    assert scan_go_files(str(tmp_path / "alias")) == (["r.go"], {})


def test_r21_go_mod_ignore_dirs_are_not_scanned(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/r\n\ngo 1.26\n\nignore ./generated\n",
        "r.go": "package r\n", "generated/g.go": "package generated\n",
        "generated/deep/d.go": "package deep\n"})
    assert scan_go_files(root) == (["r.go"], {"ignored": 2})
