"""Build constraints: go_units.match_file against go/build semantics (go-modules §5)."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from graph_generator import go_units
from graph_generator.go_model import BuildConfig
from graph_generator.go_units import match_file, tool_tags

import go_worlds

LINUX = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)
LINUX_CGO = BuildConfig("linux-amd64-cgo", "linux", "amd64", (), True, True)
WINDOWS = BuildConfig("windows-amd64", "windows", "amd64", (), False, False)
DARWIN = BuildConfig("darwin-arm64", "darwin", "arm64", (), False, False)
ANDROID = BuildConfig("android-arm64", "android", "arm64", (), False, False)
TAGGED = BuildConfig("linux-amd64+integration", "linux", "amd64", ("integration",), False, False)

PKG = b"package p\n"


def head(constraint: str, body: bytes = PKG) -> bytes:
    return constraint.encode() + b"\n\n" + body


@pytest.mark.parametrize("name,build,want", [
    ("x.go", LINUX, True),
    ("x_linux.go", LINUX, True),
    ("x_linux.go", WINDOWS, False),
    ("x_windows_amd64.go", WINDOWS, True),
    ("x_windows_arm64.go", WINDOWS, False),
    ("x_amd64.go", DARWIN, False),
    ("x_unix.go", WINDOWS, True),              # unix is not a known OS name
    ("x_linux_test.go", LINUX, True),
    ("x_linux_test.go", WINDOWS, False),
    ("linux.go", WINDOWS, True),               # no underscore: no name constraint
    ("x_test.go", WINDOWS, True),
    ("x_linux.go", ANDROID, True),             # linux is true under android
    ("x_android.go", LINUX, False),
    ("x_nacl.go", LINUX, False),               # nacl is still a known OS
    ("_x.go", LINUX, False),
    (".x.go", LINUX, False),
    ("x.txt", LINUX, False),
    ("dir/sub/x_linux.go", LINUX, True),
    ("x_linux.pb.go", WINDOWS, False),         # only the part before the first dot counts
])
def test_file_name_rules(name, build, want):
    assert match_file(name, PKG, build) is want


@pytest.mark.parametrize("constraint,build,want", [
    ("//go:build linux", LINUX, True),
    ("//go:build linux", WINDOWS, False),
    ("//go:build linux && !cgo", LINUX, True),
    ("//go:build linux && !cgo", LINUX_CGO, False),
    ("//go:build cgo", LINUX_CGO, True),
    ("//go:build (linux || darwin) && amd64", LINUX, True),
    ("//go:build (linux || darwin) && amd64", DARWIN, False),
    ("//go:build unix", DARWIN, True),
    ("//go:build unix", WINDOWS, False),
    ("//go:build ignore", LINUX, False),
    ("//go:build integration", TAGGED, True),
    ("//go:build integration", LINUX, False),
    ("//go:build go1.27", LINUX, True),
    ("//go:build go1.28", LINUX, False),
    ("//go:build gc", LINUX, True),
    ("//go:build gccgo", LINUX, False),
    ("//go:build goexperiment.jsonv2", LINUX, True),
    ("//go:build amd64.v1 && !amd64.v2", LINUX, True),
    ("//go:build boringcrypto", LINUX, False),
    ("//go:build !!linux", LINUX, False),      # syntax error: file is invalid, not built
    ("//go:build linux &&", LINUX, False),
    ("//go:build linux // comment", LINUX, False),
    ("// +build linux", LINUX, True),
    ("// +build linux", WINDOWS, False),
    ("// +build linux darwin", DARWIN, True),  # space = OR
    ("// +build linux,amd64", LINUX, True),    # comma = AND
    ("// +build linux,arm64", LINUX, False),
    ("// +build !windows", LINUX, True),
    ("// +build linux\n// +build arm64", LINUX, False),   # lines are ANDed
    ("// +build !!linux", LINUX, False),       # invalid literal becomes "ignore"
    ("//go:build windows\n// +build linux", LINUX, False),   # //go:build wins
    ("//go:build linux\n//go:build linux", LINUX, False),    # two //go:build lines: invalid
    ("//go:buildlinux", WINDOWS, True),        # not a constraint line
    ("/* //go:build windows */", LINUX, True),  # inside a block comment
])
def test_constraints(constraint, build, want):
    assert match_file("x.go", head(constraint), build) is want


def test_plus_build_needs_a_blank_line_before_package():
    assert match_file("x.go", b"// +build windows\npackage p\n", LINUX) is True
    assert match_file("x.go", b"// +build windows\n\npackage p\n", LINUX) is False


def test_constraint_after_package_clause_is_ignored():
    assert match_file("x.go", b"package p\n\n//go:build windows\n", LINUX) is True


def test_bom_and_crlf_and_non_utf8():
    assert match_file("x.go", b"\xef\xbb\xbf//go:build windows\r\n\r\npackage p\r\n", LINUX) is False
    assert match_file("x.go", b"//go:build linux\n\n// \xff\xfe\npackage p\n", LINUX) is True


def test_import_c_follows_cgo():
    src = b'package p\n\n// #include <stdio.h>\nimport "C"\n\nfunc F() {}\n'
    assert match_file("x.go", src, LINUX) is False
    assert match_file("x.go", src, LINUX_CGO) is True
    assert match_file("x_test.go", src, LINUX_CGO) is False      # cgo in a test file is an error
    grouped = b'package p\n\nimport (\n\t"fmt"\n\t"C"\n)\n'
    assert match_file("x.go", grouped, LINUX) is False


def test_package_documentation_is_ignored():
    assert match_file("doc.go", b"// Package docs.\npackage documentation\n", LINUX) is False


def test_tags_that_only_name_files():
    # GOOS/GOARCH suffixes in a +build line still go through matchTag.
    assert match_file("x.go", head("//go:build linux && amd64 && !android"), ANDROID) is False


# Output of `GOOS=$os GOARCH=$arch go list -f '{{join context.ToolTags ","}}' unsafe` for every
# go1.27.2 port (`go tool dist list`), recorded on 2026-10-13.
_EXP = "goexperiment."
_COMMON = ["jsonv2", "greenteagc", "randomizedheapbase64", "sizespecializedmalloc"]
TOOL_TAGS_1272 = """
aix/ppc64 R C ppc64.power8
android/386 D C 386.sse2
android/amd64 R D C amd64.v1
android/arm D C arm.5,arm.6,arm.7
android/arm64 R D C arm64.v8.0
darwin/amd64 R C amd64.v1
darwin/arm64 R C arm64.v8.0
dragonfly/amd64 R D C amd64.v1
freebsd/386 D C 386.sse2
freebsd/amd64 R D C amd64.v1
freebsd/arm D C arm.5,arm.6,arm.7
freebsd/arm64 R D C arm64.v8.0
illumos/amd64 R D C amd64.v1
ios/amd64 R C amd64.v1
ios/arm64 R C arm64.v8.0
js/wasm D C wasm.satconv,wasm.signext
linux/386 D C 386.sse2
linux/amd64 R D C amd64.v1
linux/arm D C arm.5,arm.6,arm.7
linux/arm64 R D C arm64.v8.0
linux/loong64 R D C -
linux/mips D C mips.hardfloat
linux/mips64 D C mips64.hardfloat
linux/mips64le D C mips64le.hardfloat
linux/mipsle D C mipsle.hardfloat
linux/ppc64 R D C ppc64.power8
linux/ppc64le R D C ppc64le.power8
linux/riscv64 R D C riscv64.rva20u64
linux/s390x R D C -
netbsd/386 D C 386.sse2
netbsd/amd64 R D C amd64.v1
netbsd/arm D C arm.5,arm.6,arm.7
netbsd/arm64 R D C arm64.v8.0
openbsd/386 D C 386.sse2
openbsd/amd64 R D C amd64.v1
openbsd/arm D C arm.5,arm.6,arm.7
openbsd/arm64 R D C arm64.v8.0
openbsd/ppc64 R D C ppc64.power8
openbsd/riscv64 R D C riscv64.rva20u64
plan9/386 D C 386.sse2
plan9/amd64 R D C amd64.v1
plan9/arm D C arm.5,arm.6,arm.7
solaris/amd64 R D C amd64.v1
wasip1/wasm D C wasm.satconv,wasm.signext
windows/386 D C 386.sse2
windows/amd64 R D C amd64.v1
windows/arm64 R D C arm64.v8.0
"""


def _expand(row: str) -> tuple[str, str, set[str]]:
    target, *marks = row.split()
    goos, goarch = target.split("/")
    tags: set[str] = set()
    for mark in marks:
        if mark == "R":
            tags |= {_EXP + "regabiwrappers", _EXP + "regabiargs"}
        elif mark == "D":
            tags.add(_EXP + "dwarf5")
        elif mark == "C":
            tags |= {_EXP + t for t in _COMMON}
        elif mark != "-":
            tags |= set(mark.split(","))
    return goos, goarch, tags


@pytest.mark.parametrize("row", [r for r in TOOL_TAGS_1272.strip().splitlines()])
def test_tool_tags_match_go_1_27_2(row):
    goos, goarch, want = _expand(row)
    assert set(tool_tags(goos, goarch)) == want


# ---------------------------------------------------------------- against go list

CORPUS = {
    "go.mod": "module example.com/bc\n\ngo 1.26\n",
    "a/a.go": "package a\n",
    "a/a_linux.go": "package a\n",
    "a/a_windows_amd64.go": "package a\n",
    "a/a_darwin.go": "package a\n",
    "a/a_unix.go": "//go:build unix\n\npackage a\n",
    "a/a_cgo.go": "//go:build cgo\n\npackage a\n",
    "a/a_nocgo.go": "//go:build !cgo\n\npackage a\n",
    "a/a_tag.go": "//go:build integration\n\npackage a\n",
    "a/a_old.go": "// +build linux,!arm64 darwin\n\npackage a\n",
    "a/a_both.go": "//go:build windows\n// +build linux\n\npackage a\n",
    "a/a_ign.go": "//go:build ignore\n\npackage main\n",
    "a/a_exp.go": "//go:build goexperiment.jsonv2 && go1.27 && !go1.28\n\npackage a\n",
    "a/a_amd64v2.go": "//go:build amd64.v2\n\npackage a\n",
    "a/a_wasm.go": "//go:build wasm.signext\n\npackage a\n",
    "a/cgo_import.go": 'package a\n\nimport "C"\n',
    "a/a_test.go": "package a\n",
    "a/x_test.go": "package a_test\n",
    "a/bad.go": "//go:build linux &&\n\npackage a\n",
    "a/doc.go": "package documentation\n",
    "b/b_linux_test.go": "package b\n",
    "b/b.go": "//go:build linux || windows\n\npackage b\n",
}

BUILDS = [LINUX, LINUX_CGO, WINDOWS, DARWIN, ANDROID, TAGGED,
          BuildConfig("js-wasm", "js", "wasm", (), False, False),
          BuildConfig("linux-arm64", "linux", "arm64", (), False, False)]

_CATS = ("GoFiles", "CgoFiles", "TestGoFiles", "XTestGoFiles")


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
def test_match_file_agrees_with_go_list(tmp_path):
    root = go_worlds.write_tree(tmp_path / "m", CORPUS)
    os.makedirs(tmp_path / "xdg" / "go" / "telemetry")
    (tmp_path / "xdg" / "go" / "telemetry" / "mode").write_text("off 2026-10-13")
    mismatches = []
    for build in BUILDS:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GOENV": "off",
               "GOTOOLCHAIN": "local", "GOWORK": "off", "GOPROXY": "off", "GOSUMDB": "off",
               "GOFLAGS": "-mod=readonly -buildvcs=false", "GOOS": build.goos,
               "GOARCH": build.goarch, "CGO_ENABLED": "1" if build.cgo else "0",
               "GOCACHE": str(tmp_path / "gocache"), "GOPATH": str(tmp_path / "gopath"),
               "GOMODCACHE": str(tmp_path / "modcache"), "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
               "TMPDIR": str(tmp_path)}
        cmd = [go_worlds.GO, "list", "-e", "-json=Dir," + ",".join(_CATS)]
        if build.tags:
            cmd.append("-tags=" + ",".join(build.tags))
        out = subprocess.run(cmd + ["./..."], cwd=root, env=env, capture_output=True,
                             text=True).stdout
        compiled = set()
        dec, i = json.JSONDecoder(), 0
        while i < len(out):
            while i < len(out) and out[i].isspace():
                i += 1
            if i >= len(out):
                break
            pkg, i = dec.raw_decode(out, i)
            rel = os.path.relpath(pkg["Dir"], root)
            for cat in _CATS:
                compiled |= {f"{rel}/{f}" for f in pkg.get(cat) or []}
        for rel, content in CORPUS.items():
            if not rel.endswith(".go"):
                continue
            ours = match_file(rel, content.encode(), build)
            if ours != (rel in compiled):
                mismatches.append((build.id, rel, ours))
    assert mismatches == []


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
@pytest.mark.parametrize("goos,goarch", [("linux", "amd64"), ("darwin", "arm64"),
                                         ("js", "wasm"), ("windows", "386")])
def test_tool_tags_match_live_toolchain(tmp_path, goos, goarch):
    os.makedirs(tmp_path / "xdg" / "go" / "telemetry")
    (tmp_path / "xdg" / "go" / "telemetry" / "mode").write_text("off 2026-10-13")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GOENV": "off", "GOTOOLCHAIN": "local",
           "GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0", "GOFLAGS": "",
           "GOCACHE": str(tmp_path / "c"), "GOPATH": str(tmp_path / "p"),
           "XDG_CONFIG_HOME": str(tmp_path / "xdg"), "GOWORK": "off"}
    out = subprocess.run([go_worlds.GO, "list", "-f", "{{join context.ToolTags \",\"}}", "unsafe"],
                         cwd=tmp_path, env=env, capture_output=True, text=True, check=True).stdout
    assert set(out.strip().split(",")) == set(tool_tags(goos, goarch))
    assert go_units.RELEASE_TAGS_MAX == 27
