"""go.mod, go.work and vendor/modules.txt parsing (spec §3.1, go-modules §1–§3).

The lexical vectors are the 31 cases of the go-modules research probe
(p01-modfile/lex.out, expected output from `go mod edit -json` 1.27.2); the
extra vectors were checked against the same command on 2026-10-13
(`test_parsers_agree_with_go_command` re-checks all of them with -m go).
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from graph_generator import go_units
from graph_generator.go_units import ModError, parse_go_mod, parse_go_work, parse_modules_txt

import go_worlds

M = {"Module": {"Path": "example.com/m"}}

# (name, go.mod text, expected dict | error substring)
LEX_VECTORS = [
    ("arrow_nospace", "module example.com/m\nreplace example.com/a=>./a\n",
     "usage: replace module/path [v1.2.3] => other/module v1.4"),
    ("bad_go", "module example.com/m\ngo 1.21.0.1\n",
     "invalid go version '1.21.0.1': must match format 1.23.0"),
    ("bom", "\ufeffmodule example.com/m\n", "unexpected input character '\\ufeff'"),
    ("cblock", "module example.com/m\n/* c */\n",
     "mod files must use // comments (not /* */ comments)"),
    ("crlf", "module example.com/m\r\ngo 1.22\r\nrequire example.com/a v1.0.0\r\n",
     {**M, "Go": "1.22", "Require": [{"Path": "example.com/a", "Version": "v1.0.0"}]}),
    ("dup_go", "module example.com/m\ngo 1.21\ngo 1.22\n", "repeated go statement"),
    ("empty_block", "module example.com/m\nrequire ()\n", M),
    ("escape", 'module "example.com/a\\x62"\n', {"Module": {"Path": "example.com/ab"}}),
    ("go_noncanon", "module example.com/m\ngo 1.21rc1\n", {**M, "Go": "1.21rc1"}),
    ("godebug_unknown", "module example.com/m\ngodebug nosuchkey=1\n",
     {**M, "GoDebug": [{"Key": "nosuchkey", "Value": "1"}]}),
    ("ignore_abs", "module example.com/m\nignore /abs\n", {**M, "Ignore": [{"Path": "/abs"}]}),
    ("module_block", "module (\n example.com/blk\n)\n", {"Module": {"Path": "example.com/blk"}}),
    ("multitok_block", "module example.com/m\nrequire example.com/a (\n v1.0.0\n)\n",
     "unknown block type: require example.com/a"),
    ("no_module", "go 1.22\n", {"Module": {"Path": ""}, "Go": "1.22"}),
    ("paren_junk", "module example.com/m\nrequire (\n example.com/a v1.0.0\n) x\n",
     "syntax error (expected newline after closing paren)"),
    ("paren_trailing", "module example.com/m\nrequire (\n example.com/a v1.0.0\n) // c\n",
     {**M, "Require": [{"Path": "example.com/a", "Version": "v1.0.0"}]}),
    ("raw", "module `example.com/raw`\n",
     "invalid quoted string: unquoted string cannot contain quote"),
    ("replace_abs_ver", "module example.com/m\nreplace example.com/a => /abs v1.0.0\n",
     'replacement module directory path "/abs" cannot have version'),
    ("replace_at", "module example.com/m\nreplace example.com/a => example.com/b@v1.0.0\n",
     "replacement module must match format 'path version', not 'path@version'"),
    ("replace_dot", "module example.com/m\nreplace example.com/a => .\n",
     {**M, "Replace": [{"Old": {"Path": "example.com/a"}, "New": {"Path": "."}}]}),
    ("replace_win", "module example.com/m\nreplace example.com/a => C:\\x\\y\n",
     "replacement directory appears to be Windows path (on a non-windows system)"),
    ("require_noncanon", "module example.com/m\nrequire example.com/a master\n",
     'version "master" invalid: must be of the form v1.2.3'),
    ("single_quote", "module 'example.com/m'\n",
     "invalid quoted string: unquoted string cannot contain quote"),
    ("slashslash_ident", "module example.com/m//comment\n", M),
    ("tab_in_path", 'module example.com/m\nrequire "example.com/a b" v1.0.0\n',
     {**M, "Require": [{"Path": "example.com/a b", "Version": "v1.0.0"}]}),
    ("tool_block", "module example.com/m\ntool (\n example.com/m/cmd/x\n)\n",
     {**M, "Tool": [{"Path": "example.com/m/cmd/x"}]}),
    ("toolchain_bad", "module example.com/m\ngo 1.22\ntoolchain 1.22.1\n",
     "invalid toolchain version '1.22.1': must match format go1.23.0 or default"),
    ("toolchain_default", "module example.com/m\ngo 1.22\ntoolchain default\n",
     {**M, "Go": "1.22", "Toolchain": "default"}),
    ("two_modules", "module example.com/m\nmodule example.com/n\n", "repeated module statement"),
    ("unknown", "module example.com/m\nfrobnicate x\n", "unknown directive: frobnicate"),
    ("unknown_block", "module example.com/m\nfrobnicate (\n x\n)\n", "unknown block type: frobnicate"),
]

# Cases from the go 1.27.2 modfile source (rule.go) that the seed parser did not cover.
EXTRA_VECTORS = [
    ("canonical_versions",
     "module example.com/m\n\ngo 1.22\n\nrequire (\n\texample.com/a v1.2\n\texample.com/b v1.2.3+meta\n"
     "\texample.com/c v2.0.0+incompatible\n\texample.com/d v1\n)\n",
     {**M, "Go": "1.22", "Require": [
         {"Path": "example.com/a", "Version": "v1.2.0"},
         {"Path": "example.com/b", "Version": "v1.2.3"},
         {"Path": "example.com/c", "Version": "v2.0.0+incompatible"},
         {"Path": "example.com/d", "Version": "v1.0.0"}]}),
    ("path_major_mismatch", "module example.com/m\nrequire example.com/a/v2 v1.0.0\n",
     'version "v1.0.0" invalid: should be v2, not v1'),
    ("gopkg_major_mismatch", "module example.com/m\nrequire gopkg.in/yaml.v3 v2.0.0\n",
     'version "v2.0.0" invalid: should be v3, not v2'),
    ("gopkg_ok", "module example.com/m\nrequire gopkg.in/check.v1 v0.0.0-20161208181325-20d25e280405\n",
     {**M, "Require": [{"Path": "gopkg.in/check.v1",
                        "Version": "v0.0.0-20161208181325-20d25e280405"}]}),
    ("v1_suffix_invalid", "module example.com/m\nrequire example.com/a/v1 v1.0.0\n",
     "invalid module path"),
    ("godebug_quote", 'module example.com/m\ngodebug "a=b"\n', "usage: godebug key=value"),
    ("godebug_no_eq", "module example.com/m\ngodebug ab\n", "usage: godebug key=value"),
    ("retract_extra", "module example.com/m\nretract v1.0.0 extra\n",
     'unexpected token after version: "extra"'),
    ("retract_range_tight", "module example.com/m\nretract [v1.0.0,v1.1.0]\n",
     {**M, "Retract": [{"Low": "v1.0.0", "High": "v1.1.0"}]}),
    ("retract_open", "module example.com/m\nretract [v1.0.0, v1.1.0\n", "expected ']' after version"),
    ("replace_versioned_old", "module example.com/m\nreplace example.com/a v1.0 => ../a\n",
     {**M, "Replace": [{"Old": {"Path": "example.com/a", "Version": "v1.0.0"},
                        "New": {"Path": "../a"}}]}),
    ("bad_escape_squote", 'module "example.com/a\\\'"\n', "invalid quoted string"),
    ("repeated_toolchain", "module example.com/m\ntoolchain go1.22.0\ntoolchain go1.22.1\n",
     "repeated toolchain statement"),
    ("exclude", "module example.com/m\nexclude example.com/a v1.0.0\n",
     {**M, "Exclude": [{"Path": "example.com/a", "Version": "v1.0.0"}]}),
    ("indirect_block_and_line",
     "module example.com/m\nrequire example.com/a v1.0.0 // indirect\nrequire (\n"
     "\texample.com/b v1.0.0 // indirect; with note\n\texample.com/c v1.0.0 // not indirect\n)\n",
     {**M, "Require": [{"Path": "example.com/a", "Version": "v1.0.0", "Indirect": True},
                       {"Path": "example.com/b", "Version": "v1.0.0", "Indirect": True},
                       {"Path": "example.com/c", "Version": "v1.0.0"}]}),
]

ALL_MOD = """// Deprecated: use example.com/all/v2 instead.
module example.com/all // trailing comment

go 1.25.0

toolchain go1.25.3

godebug default=go1.21
godebug (
\tpanicnil=1
\tasynctimerchan=0
)

require (
\tgolang.org/x/text v0.38.0 // indirect
\t"example.com/quoted" v1.0.0
\tgopkg.in/yaml.v3 v3.0.1
)

require example.com/dep/v2 v2.3.4

exclude example.com/dep/v2 v2.0.0

replace example.com/dep/v2 v2.3.4 => example.com/fork/dep/v2 v2.3.5
replace (
\texample.com/local => ./local
\texample.com/abs => /abs/path/outside
\texample.com/up => ../outside
)

retract v1.0.1
retract [v1.1.0, v1.2.0] // range

tool golang.org/x/tools/cmd/stringer

ignore (
\t./node_modules
\tstatic
)
"""

# all.json from the probe (go mod edit -json), minus the comment-derived
# Module.Deprecated and Retract.Rationale that the parser leaves out.
ALL_JSON = {
    "Module": {"Path": "example.com/all"},
    "Go": "1.25.0",
    "Toolchain": "go1.25.3",
    "GoDebug": [{"Key": "default", "Value": "go1.21"}, {"Key": "panicnil", "Value": "1"},
                {"Key": "asynctimerchan", "Value": "0"}],
    "Require": [{"Path": "golang.org/x/text", "Version": "v0.38.0", "Indirect": True},
                {"Path": "example.com/quoted", "Version": "v1.0.0"},
                {"Path": "gopkg.in/yaml.v3", "Version": "v3.0.1"},
                {"Path": "example.com/dep/v2", "Version": "v2.3.4"}],
    "Exclude": [{"Path": "example.com/dep/v2", "Version": "v2.0.0"}],
    "Replace": [
        {"Old": {"Path": "example.com/dep/v2", "Version": "v2.3.4"},
         "New": {"Path": "example.com/fork/dep/v2", "Version": "v2.3.5"}},
        {"Old": {"Path": "example.com/local"}, "New": {"Path": "./local"}},
        {"Old": {"Path": "example.com/abs"}, "New": {"Path": "/abs/path/outside"}},
        {"Old": {"Path": "example.com/up"}, "New": {"Path": "../outside"}}],
    "Retract": [{"Low": "v1.0.1", "High": "v1.0.1"}, {"Low": "v1.1.0", "High": "v1.2.0"}],
    "Tool": [{"Path": "golang.org/x/tools/cmd/stringer"}],
    "Ignore": [{"Path": "./node_modules"}, {"Path": "static"}],
}

SYNTH_WORK = """go 1.24.0

toolchain go1.24.3

godebug (
\tpanicnil=1
)
godebug default=go1.21

use ./a // comment
use (
\t../b
\t"/abs/c"
)

replace example.com/x v1.0.0 => ./fork
replace example.com/y => example.com/z v1.2.3
"""

ETCD_WORK = """// This is a generated file. Do not edit directly.

go 1.26

toolchain go1.26.8

use (
\t.
\t./api
\t./cache
\t./client/pkg
\t./client/v3
\t./etcdctl
\t./etcdutl
\t./pkg
\t./server
\t./tests
\t./tools/mod
\t./tools/rw-heatmaps
\t./tools/testgrid-analysis
)
"""

WORK_VECTORS = [
    ("synth", SYNTH_WORK, {
        "Go": "1.24.0", "Toolchain": "go1.24.3",
        "Godebug": [{"Key": "panicnil", "Value": "1"}, {"Key": "default", "Value": "go1.21"}],
        "Use": [{"DiskPath": "./a"}, {"DiskPath": "../b"}, {"DiskPath": "/abs/c"}],
        "Replace": [
            {"Old": {"Path": "example.com/x", "Version": "v1.0.0"}, "New": {"Path": "./fork"}},
            {"Old": {"Path": "example.com/y"}, "New": {"Path": "example.com/z", "Version": "v1.2.3"}}]}),
    ("etcd", ETCD_WORK, {
        "Go": "1.26", "Toolchain": "go1.26.8",
        "Use": [{"DiskPath": p} for p in (
            ".", "./api", "./cache", "./client/pkg", "./client/v3", "./etcdctl", "./etcdutl",
            "./pkg", "./server", "./tests", "./tools/mod", "./tools/rw-heatmaps",
            "./tools/testgrid-analysis")]}),
    ("prom", "go 1.26.0\n\nuse (\n\t.\n\t./compliance\n\t./documentation/examples/remote_storage\n"
     "\t./internal/tools\n\t./web/ui/mantine-ui/src/promql/tools\n)\n", {
         "Go": "1.26.0",
         "Use": [{"DiskPath": p} for p in (
             ".", "./compliance", "./documentation/examples/remote_storage", "./internal/tools",
             "./web/ui/mantine-ui/src/promql/tools")]}),
    ("bad", "go 1.22\nuse ./a\nfrob x\n", "unknown directive: frob"),
    ("repeated_go", "go 1.22\ngo 1.23\n", "repeated go statement"),
    ("bad_toolchain", "go 1.22\ntoolchain 1.22.1\n", "invalid toolchain version '1.22.1'"),
    ("use_args", "go 1.22\nuse ./a ./b\n", "usage: use local/dir"),
    ("require_in_work", "go 1.22\nrequire example.com/a v1.0.0\n", "unknown directive: require"),
    ("module_block_in_work", "go 1.22\nmodule (\n x\n)\n", "unknown block type: module"),
    ("empty", "", {}),
]


@pytest.mark.parametrize("name,text,expect", LEX_VECTORS + EXTRA_VECTORS,
                         ids=[v[0] for v in LEX_VECTORS + EXTRA_VECTORS])
def test_parse_go_mod_vectors(name, text, expect):
    if isinstance(expect, str):
        with pytest.raises(ModError) as exc:
            parse_go_mod(text)
        assert expect in str(exc.value)
    else:
        assert parse_go_mod(text) == expect


def test_parse_go_mod_every_directive_matches_probe():
    assert parse_go_mod(ALL_MOD) == ALL_JSON


def test_parse_errors_carry_the_line():
    with pytest.raises(ModError) as exc:
        parse_go_mod("module example.com/m\n\ngo 1.21\ngo 1.22\n")
    assert exc.value.line == 4


def test_lax_parse_keeps_only_dependency_directives():
    text = ("module example.com/dep\ngo 1.21-custom\nreplace example.com/a => ./a\n"
            "exclude example.com/b v1.0.0\nfuture directive\nfuture (\n x\n)\n"
            "require example.com/c v1.0.0\nretract [bad\nignore ./x\n")
    assert parse_go_mod(text, strict=False) == {
        "Module": {"Path": "example.com/dep"}, "Go": "1.21",
        "Require": [{"Path": "example.com/c", "Version": "v1.0.0"}],
        "Ignore": [{"Path": "./x"}]}


@pytest.mark.parametrize("name,text,expect", WORK_VECTORS, ids=[v[0] for v in WORK_VECTORS])
def test_parse_go_work_vectors(name, text, expect):
    if isinstance(expect, str):
        with pytest.raises(ModError) as exc:
            parse_go_work(text)
        assert expect in str(exc.value)
    else:
        assert parse_go_work(text) == expect


def test_directive_line_finds_go_and_toolchain():
    text = "// c\nmodule example.com/m\n\n go 1.26 // x\ntoolchain go1.26.8\n"
    assert go_units.directive_line(text, "go") == 4
    assert go_units.directive_line(text, "toolchain") == 5
    assert go_units.directive_line(text, "godebug") is None


# ---------------------------------------------------------------- vendor/modules.txt

MODULE_VENDOR_TXT = """# example.com/Upper v1.0.0
## explicit; go 1.21
example.com/Upper
# example.com/dep v1.1.0
## explicit; go 1.21
example.com/dep
example.com/dep/sub
# example.com/dep/v2 v2.0.0
## explicit; go 1.21
example.com/dep/v2
# example.com/loc v0.0.0 => ./local
## explicit; go 1.22
example.com/loc
# example.com/loc => ./local
"""

MODULE_VENDOR_GOMOD = """module example.com/v1

go 1.22

require (
\texample.com/Upper v1.0.0
\texample.com/dep v1.1.0
\texample.com/dep/v2 v2.0.0
\texample.com/loc v0.0.0
)

replace example.com/loc => ./local
"""

WORKSPACE_VENDOR_TXT = """## workspace
# example.com/dep v1.0.0
## explicit; go 1.21
# example.com/dep v1.2.0
## explicit; go 1.21
example.com/dep
"""


def test_parse_modules_txt_module_vendor():
    got = parse_modules_txt(MODULE_VENDOR_TXT)
    assert got["workspace"] is False
    mods = {(m["path"], m["version"]): m for m in got["modules"]}
    assert mods[("example.com/dep", "v1.1.0")] == {
        "path": "example.com/dep", "version": "v1.1.0", "replace": None, "explicit": True,
        "go": "1.21", "packages": ["example.com/dep", "example.com/dep/sub"]}
    assert mods[("example.com/loc", "v0.0.0")]["replace"] == {"path": "./local", "version": ""}
    assert mods[("example.com/loc", "")] == {
        "path": "example.com/loc", "version": "", "replace": {"path": "./local", "version": ""},
        "explicit": False, "go": None, "packages": []}
    assert [m["path"] for m in got["modules"]][:2] == ["example.com/Upper", "example.com/dep"]


def test_parse_modules_txt_workspace_and_junk():
    got = parse_modules_txt(WORKSPACE_VENDOR_TXT + "junk line without module\n# short\n"
                            "# example.com/r v1.0.0 => example.com/fork v1.0.1\n"
                            "# example.com/x notaversion\nexample.com/x/pkg\n")
    assert got["workspace"] is True
    mods = {(m["path"], m["version"]): m for m in got["modules"]}
    assert mods[("example.com/dep", "v1.0.0")]["packages"] == []
    assert mods[("example.com/dep", "v1.2.0")]["packages"] == ["example.com/dep"]
    assert mods[("example.com/r", "v1.0.0")]["replace"] == {
        "path": "example.com/fork", "version": "v1.0.1"}
    # A header that is neither a version nor "=>" resets the current module.
    assert all(m["path"] != "example.com/x" for m in got["modules"])


def test_vendor_consistent_module():
    gomod = parse_go_mod(MODULE_VENDOR_GOMOD)
    assert go_units.vendor_errors([gomod], parse_modules_txt(MODULE_VENDOR_TXT)) == []


def test_vendor_inconsistencies_are_reported_like_the_go_command():
    gomod = parse_go_mod(MODULE_VENDOR_GOMOD.replace("example.com/dep v1.1.0", "example.com/dep v1.2.0")
                         .replace("replace example.com/loc => ./local", "replace example.com/loc => ./other"))
    errs = go_units.vendor_errors([gomod], parse_modules_txt(MODULE_VENDOR_TXT))
    assert ("example.com/dep@v1.2.0: is explicitly required in go.mod, but not marked as explicit "
            "in vendor/modules.txt") in errs
    assert ("example.com/loc: is replaced by ./other in go.mod, but marked as replaced by ./local "
            "in vendor/modules.txt") in errs
    assert ("example.com/dep@v1.1.0: is marked as explicit in vendor/modules.txt, but not "
            "explicitly required in go.mod") in errs


def test_vendor_replaced_but_not_in_gomod():
    gomod = parse_go_mod(MODULE_VENDOR_GOMOD.replace("replace example.com/loc => ./local", ""))
    errs = go_units.vendor_errors([gomod], parse_modules_txt(MODULE_VENDOR_TXT))
    assert any("is marked as replaced in vendor/modules.txt, but not replaced in go.mod" in e
               for e in errs)


def test_vendor_missing_modules_txt_with_requirements_is_inconsistent():
    gomod = parse_go_mod(MODULE_VENDOR_GOMOD)
    assert go_units.vendor_errors([gomod], parse_modules_txt(""))


def test_vendor_pre_114_only_checks_versions():
    gomod = parse_go_mod("module example.com/old\ngo 1.13\nrequire example.com/dep v1.1.0\n")
    txt = parse_modules_txt("# example.com/dep v1.1.0\nexample.com/dep\n")
    assert go_units.vendor_errors([gomod], txt) == []
    txt2 = parse_modules_txt("# example.com/dep v1.0.0\nexample.com/dep\n")
    assert go_units.vendor_errors([gomod], txt2) == [
        "example.com/dep@v1.1.0: is explicitly required in go.mod, but vendor/modules.txt "
        "indicates example.com/dep@v1.0.0"]


def test_vendor_workspace_check():
    a = parse_go_mod("module example.com/a\ngo 1.22\nrequire example.com/dep v1.0.0\n")
    b = parse_go_mod("module example.com/b\ngo 1.22\nrequire example.com/dep v1.2.0\n")
    txt = parse_modules_txt(WORKSPACE_VENDOR_TXT)
    assert go_units.vendor_errors([a, b], txt, workspace=True) == []
    c = parse_go_mod("module example.com/c\ngo 1.22\nrequire example.com/other v1.0.0\n")
    errs = go_units.vendor_errors([a, b, c], txt, workspace=True)
    assert errs == ["example.com/other@v1.0.0: is explicitly required in go.mod, but not marked "
                    "as explicit in vendor/modules.txt"]


# ---------------------------------------------------------------- against the go command

def _go_env(tmp):
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "GOENV": "off", "GOTOOLCHAIN": "local",
            "GOWORK": "off", "GOPROXY": "off", "GOSUMDB": "off", "GOFLAGS": "",
            "GOCACHE": str(tmp / "gocache"), "GOMODCACHE": str(tmp / "modcache"),
            "GOPATH": str(tmp / "gopath"), "XDG_CONFIG_HOME": str(tmp / "xdg"),
            "TMPDIR": str(tmp)}


def _strip(d):
    d = json.loads(json.dumps(d))
    d.get("Module", {}).pop("Deprecated", None)
    for r in d.get("Retract") or []:
        r.pop("Rationale", None)
    return {k: v for k, v in d.items() if v not in (None, [], "")}


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
def test_parsers_agree_with_go_command(tmp_path):
    os.makedirs(tmp_path / "xdg" / "go" / "telemetry")
    (tmp_path / "xdg" / "go" / "telemetry" / "mode").write_text("off 2026-10-13")
    env = _go_env(tmp_path)
    vectors = LEX_VECTORS + EXTRA_VECTORS + [("all", ALL_MOD, ALL_JSON)]
    disagreements = []
    for name, text, expect in vectors:
        path = tmp_path / f"{name}.mod"
        path.write_bytes(text.encode("utf-8"))
        r = subprocess.run([go_worlds.GO, "mod", "edit", "-json", str(path)], cwd=tmp_path,
                           env=env, capture_output=True, text=True)
        try:
            ours, ok = parse_go_mod(text), True
        except ModError:
            ours, ok = None, False
        if (r.returncode == 0) != ok or (ok and _strip(json.loads(r.stdout)) != _strip(ours)):
            disagreements.append((name, r.returncode, r.stdout[:200], r.stderr[:200], ours))
    for name, text, expect in WORK_VECTORS:
        if not text:
            continue
        path = tmp_path / f"{name}.work"
        path.write_text(text)
        r = subprocess.run([go_worlds.GO, "work", "edit", "-json", str(path)], cwd=tmp_path,
                           env=env, capture_output=True, text=True)
        try:
            ours, ok = parse_go_work(text), True
        except ModError:
            ours, ok = None, False
        if (r.returncode == 0) != ok or (ok and _strip(json.loads(r.stdout)) != _strip(ours)):
            disagreements.append((name, r.returncode, r.stdout[:200], r.stderr[:200], ours))
    assert disagreements == []
