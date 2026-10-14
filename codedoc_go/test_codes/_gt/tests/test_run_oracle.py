import json
import os
import shutil

import pytest

import generate_manifest as gm
import run_oracle as ro
from conftest import GT, PROXY, write
from test_generate_manifest import replace_in_cases

CONSTRUCTS = os.path.join(GT, "oracle", "testdata", "constructs")


def test_find_go_precedence(tmp_path, monkeypatch):
    fake = tmp_path / "root" / "bin" / "go"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("GORACLE_GO", str(tmp_path / "root"))
    monkeypatch.setenv("GO_TOOLCHAIN", "/nonexistent")
    assert ro.find_go() == str(fake)
    assert ro.find_go(str(fake)) == str(fake)
    monkeypatch.delenv("GORACLE_GO")
    with pytest.raises(ro.OracleError, match="no go binary at /nonexistent"):
        ro.find_go()


def test_entries_are_validated(tmp_path):
    def entries(obj):
        write(str(tmp_path), {"entries.json": json.dumps(obj)})
        return ro.load_entries(str(tmp_path))
    assert entries({"a": {"dir": ".", "cmd": ["go", "run", "."]}})["a"]["cmd"][1] == "run"
    for bad, msg in [({"a": {"dir": ".", "cmd": ["go", "run", "-mod=mod", "."]}}, "-mod flags"),
                     ({"a": {"dir": ".", "test": ["./..."], "env": {"GOFLAGS": "-mod=vendor"}}}, "-mod flags"),
                     ({"a": {"dir": ".", "test": ["./..."], "env": {"GOPROXY": "direct"}}}, "may not set GOPROXY"),
                     ({"a": {"dir": ".", "cmd": ["go", "build", "."]}}, "must start with go run"),
                     ({"a": {"dir": "."}}, "exactly one of cmd and test"),
                     ([], "must map entry names")]:
        with pytest.raises(ro.OracleError, match=msg):
            entries(bad)


def test_argv_and_environment():
    run = ro.entry_argv("/go/bin/go", {"cmd": ["go", "run", "./cmd/x", "--demo"]})
    assert run == ["/go/bin/go", "run", "-gcflags=all=-l", "-ldflags=-checklinkname=0", "-buildvcs=false",
                   "./cmd/x", "--demo"]
    test = ro.entry_argv("/go/bin/go", {"test": ["./a/...", "./b"], "test_args": ["-run", "TestX"]})
    assert test == ["/go/bin/go", "test", "-count=1", "-vet=off", "-gcflags=all=-l", "-ldflags=-checklinkname=0",
                    "-buildvcs=false", "-run", "TestX", "./a/...", "./b"]
    env = ro.entry_env("/go/bin/go", "/s/fx", {"env": {"GOWORK": "off", "IRYO_DEMO": "1"}}, "/s/fx/hits/e")
    assert env["GOPROXY"] == "file://" + PROXY and env["GOSUMDB"] == "off" and env["GOFLAGS"] == "-modcacherw"
    assert (env["GOOS"], env["GOARCH"], env["CGO_ENABLED"], env["GOTOOLCHAIN"], env["GOENV"]) == \
        ("linux", "amd64", "0", "local", "off")
    assert env["GOWORK"] == "off" and env["IRYO_DEMO"] == "1" and env["GORACLE_OUT"] == "/s/fx/hits/e"
    assert env["GOMODCACHE"] == "/s/fx/modcache" and env["PATH"].startswith("/go/bin:")
    assert env["GOPROXY"].startswith("file:///") and "-mod=" not in env["GOFLAGS"]  # spec R22; vendor mode stays the default
    assert "GOWORK" not in ro.entry_env("/go/bin/go", "/s/fx", {}, "/h")


def test_missing_manifest_is_reported(tiny, tmp_path):
    with pytest.raises(ro.OracleError, match="run generate_manifest.py first"):
        ro.Runner(tiny, "/bin/true", str(tmp_path), None, 60).run()


@pytest.mark.go
def test_tiny_fixture_passes_end_to_end(tiny, go_bin, tmp_path):
    assert gm.main(["generate_manifest.py", tiny]) == 0
    lines = []
    rc = ro.Runner(tiny, go_bin, str(tmp_path / "scratch"), None, 600, printer=lines.append).run()
    out = "\n".join(lines)
    assert rc == 0, out
    assert "entry tinyapp: ok" in out and "summary: 8 cases: 8 pass, 0 fail, 0 say nothing" in out
    work = tmp_path / "scratch" / "tiny"
    verdicts = json.loads((work / "verdicts.json").read_text())
    assert {c["id"]: c["verdict"] for c in verdicts["cases"]}["IFC-001"] == "IFACE-CONSISTENT"
    assert (work / "copy" / "_goracle_rt" / "exit.go").is_file()
    assert "replace codedoc.local/goracle/rt => " in (work / "copy" / "lib" / "go.mod").read_text()
    assert "goracle" not in open(os.path.join(tiny, "lib", "go.mod")).read()  # the fixture is untouched


@pytest.mark.go
def test_wrong_expectations_fail(tiny, go_bin, tmp_path):
    # candidates are allowed implementers too (spec R6): drop CAT from both lists
    replace_in_cases(tiny, "runtime=[DOG, CAT], candidates=[DOG, CAT]", "runtime=[DOG], candidates=[DOG]")
    replace_in_cases(tiny, 'resolved(GREET), "pointer receiver method"', 'resolved(SPEAK), "pointer receiver method"')
    replace_in_cases(tiny, 'external("example.com/thirdparty/uuidlite")', 'resolved(DOG)')
    assert gm.main(["generate_manifest.py", tiny]) == 0
    lines = []
    rc = ro.Runner(tiny, go_bin, str(tmp_path / "scratch"), None, 600, printer=lines.append).run()
    out = "\n".join(lines)
    assert rc == 1
    assert "IFACE-OTHER            IFC-001    undocumented implementer example.com/tiny/lib/core.(*Cat).Speak" in out
    assert "IFACE-OTHER            CALL-001" in out  # Greet is not a method Speak
    assert "MISS                   EXT-002    expected Dog.Speak (lib/core/core.go), observed -" in out


@pytest.mark.go
def test_failed_entry_and_entry_subset(tiny, go_bin, tmp_path):
    write(tiny, {"entries.json": json.dumps({"tinyapp": {"dir": "app", "cmd": ["go", "run", "./cmd/tinyapp"]},
                                             "broken": {"dir": "app", "cmd": ["go", "run", "./cmd/nope"]}})})
    replace_in_cases(tiny, 'plain("builtin"), "builtin", entry=RUN', 'plain("builtin"), "builtin", entry="broken"')
    assert gm.main(["generate_manifest.py", tiny]) == 0
    lines = []
    rc = ro.Runner(tiny, go_bin, str(tmp_path / "scratch"), None, 600, printer=lines.append).run()
    out = "\n".join(lines)
    assert rc == 1 and "entry broken: FAILED" in out
    assert "CONSISTENT-NOEXEC      BLT-001" in out  # tinyapp ran the defer statement (reach counter)
    lines.clear()
    rc = ro.Runner(tiny, go_bin, str(tmp_path / "scratch"), ["broken"], 600, printer=lines.append).run()
    out = "\n".join(lines)
    assert rc == 1 and "SKIPPED                CALL-001" in out and "ENTRY-FAILED           BLT-001" in out


@pytest.mark.go
def test_constructs_verdicts_match_want(go_bin, tmp_path):
    fixture = tmp_path / "constructs"
    shutil.copytree(CONSTRUCTS, fixture)
    lines = []
    rc = ro.Runner(str(fixture), go_bin, str(tmp_path / "scratch"), None, 600, printer=lines.append).run()
    assert rc == 1  # the fixture holds deliberately wrong cases
    verdicts = json.loads((tmp_path / "scratch" / "constructs" / "verdicts.json").read_text())
    want = json.loads((fixture / "ground_truth" / "want.json").read_text())
    assert {c["id"]: c["verdict"] for c in verdicts["cases"]} == want


def test_constructs_want_matches_cases():
    import importlib.util
    spec = importlib.util.spec_from_file_location("constructs_cases", os.path.join(CONSTRUCTS, "ground_truth", "cases.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with open(os.path.join(CONSTRUCTS, "ground_truth", "want.json"), encoding="utf-8") as handle:
        assert json.load(handle) == module.WANT
    with open(os.path.join(CONSTRUCTS, "ground_truth", "manifest.json"), encoding="utf-8") as handle:
        assert {c["id"] for c in json.load(handle)["cases"]} == set(module.WANT)
