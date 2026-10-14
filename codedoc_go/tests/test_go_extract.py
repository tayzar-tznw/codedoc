"""go_extract (Phase 1.5, spec §2.6 and §4): the reuse stamp, one helper call
over every scanned file, the entities store and the freshness rules."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

import pytest

from graph_generator import go_extract
from graph_generator.go_extract import ExtractError, StaleEntitiesError
from graph_generator.go_model import BuildConfig, Discovery, GoUnit

PRIMARY = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def unit(key: str, root: str) -> GoUnit:
    return GoUnit(key=key, root=root, go="1.26", toolchain=None, requires=(), replaces=(),
                  vendor=False, in_repo_deps=())


def discovery(root: str, files: dict[str, str], units=(), builds=(PRIMARY,)) -> Discovery:
    return Discovery(repo="r", repo_root=root, units=list(units), unit_of_file=dict(files),
                     groups=[], builds=list(builds), toolchain="go1.27.2", mode="offline",
                     module_source="none", peers=[], skipped={}, warnings=[])


def record(text: str, **extra) -> dict:
    out = {"sha256": sha(text), "module": "example.com/m", "package": "example.com/m",
           "package_name": "m", "origin": "package", "generated": False, "build_constraint": "",
           "build_included": True, "syntax_errors": [], "imports": [], "types": [], "funcs": []}
    out.update(extra)
    return out


class FakeExtract:
    """Stands in for run_extract: records each call, answers from the texts."""

    def __init__(self, texts: dict[str, str], errors=()):
        self.texts, self.errors, self.calls = texts, set(errors), []

    def __call__(self, relpaths):
        self.calls.append(list(relpaths))
        records, errors = {}, {}
        for rel in relpaths:
            if rel in self.errors:
                errors[rel] = f"{rel}:1:1: expected 'package', found 'func'"
            else:
                records[rel] = record(self.texts[rel])
        return records, errors, {"kind": "summary", "files": len(relpaths), "errors": len(errors)}


# ---------------------------------------------------------------- the stamp (R29)

def test_stamp_changes_with_each_input():
    base = dict(helper_revision="h1", build=PRIMARY, gomod="go.mod", module="example.com/m")
    stamp = go_extract.entity_stamp(**base)
    assert stamp == go_extract.entity_stamp(**base)
    variants = [
        dict(base, helper_revision="h2"),
        dict(base, build=BuildConfig("linux-arm64", "linux", "arm64", (), False, True)),
        dict(base, build=BuildConfig("linux-amd64+x", "linux", "amd64", ("x",), False, True)),
        dict(base, build=BuildConfig("linux-amd64-cgo", "linux", "amd64", (), True, True)),
        dict(base, gomod="sub/go.mod"),
        dict(base, module="example.com/other"),
        dict(base, gomod=None, module=None),
    ]
    stamps = {go_extract.entity_stamp(**v) for v in variants}
    assert stamp not in stamps and len(stamps) == len(variants)


def test_stamp_includes_the_extractor_revision(monkeypatch):
    args = dict(helper_revision="h", build=PRIMARY, gomod="go.mod", module="m")
    before = go_extract.entity_stamp(**args)
    monkeypatch.setattr(go_extract, "EXTRACTOR_REVISION", go_extract.EXTRACTOR_REVISION + 1)
    assert go_extract.entity_stamp(**args) != before


def test_file_stamps_follow_module_ownership():
    disc = discovery("/r", {"a.go": "example.com/m", "sub/b.go": "example.com/m/sub"},
                     units=[unit("example.com/m", ""), unit("example.com/m/sub", "sub")])
    stamps = go_extract.file_stamps(["a.go", "sub/b.go", "loose/c.go"], disc, PRIMARY, "h")
    assert stamps["a.go"] == go_extract.entity_stamp(helper_revision="h", build=PRIMARY,
                                                     gomod="go.mod", module="example.com/m")
    assert stamps["sub/b.go"] == go_extract.entity_stamp(helper_revision="h", build=PRIMARY,
                                                         gomod="sub/go.mod", module="example.com/m/sub")
    assert stamps["loose/c.go"] == go_extract.entity_stamp(helper_revision="h", build=PRIMARY,
                                                           gomod=None, module=None)


# ---------------------------------------------------------------- reuse or extract

def stamped(texts: dict[str, str], stamps: dict[str, str]) -> dict[str, dict]:
    return {rel: record(t, stamp=stamps[rel]) for rel, t in texts.items()}


def test_fresh_records_are_reused_without_a_helper_run():
    texts = {"a.go": "package m\n", "b.go": "package m\n\nfunc B() {}\n"}
    stamps = {rel: "s" for rel in texts}
    fake = FakeExtract(texts)
    store, report = go_extract.update_entities(
        stamped(texts, stamps), file_hashes={r: sha(t) for r, t in texts.items()},
        stamps=stamps, extract=fake)
    assert fake.calls == []
    assert sorted(store) == ["a.go", "b.go"]
    assert report == {"extracted": 0, "reused": 2, "failed": {}, "changed": {}}


@pytest.mark.parametrize("why", ["changed", "new", "stamp", "deleted"])
def test_any_change_reextracts_every_file_in_one_call(why):
    texts = {"a.go": "package m\n", "b.go": "package m\n\nfunc B() {}\n", "c.go": "package m\n"}
    stamps = {rel: "s" for rel in texts}
    old = stamped(texts, stamps)
    hashes = {r: sha(t) for r, t in texts.items()}
    if why == "changed":
        texts["b.go"] = "package m\n\nfunc B2() {}\n"
        hashes["b.go"] = sha(texts["b.go"])
    elif why == "new":
        texts["d.go"] = "package m\n"
        hashes["d.go"], stamps["d.go"] = sha(texts["d.go"]), "s"
    elif why == "stamp":
        stamps = {rel: "s2" for rel in texts}
    else:
        del texts["c.go"], hashes["c.go"], stamps["c.go"]
    fake = FakeExtract(texts)
    store, report = go_extract.update_entities(old, file_hashes=hashes, stamps=stamps, extract=fake)
    # Receiver owners and twin choices depend on sibling files, so a deleted
    # file re-extracts the others too.
    assert fake.calls == [sorted(hashes)]
    assert sorted(store) == sorted(hashes)
    assert all(store[r]["stamp"] == stamps[r] and store[r]["sha256"] == hashes[r] for r in store)
    assert report["extracted"] == len(hashes) and report["reused"] == 0


def test_files_the_helper_cannot_parse_get_no_record():
    texts = {"a.go": "package m\n", "bad.go": "func F() {}\n"}
    fake = FakeExtract(texts, errors={"bad.go"})
    store, report = go_extract.update_entities(
        {}, file_hashes={r: sha(t) for r, t in texts.items()}, stamps={r: "s" for r in texts},
        extract=fake)
    assert sorted(store) == ["a.go"]
    assert list(report["failed"]) == ["bad.go"] and "expected 'package'" in report["failed"]["bad.go"]


def test_a_file_changed_after_the_scan_keeps_the_helpers_hash():
    texts = {"a.go": "package m\n\nfunc New() {}\n"}
    fake = FakeExtract(texts)
    store, report = go_extract.update_entities(
        {}, file_hashes={"a.go": sha("package m\n")}, stamps={"a.go": "s"}, extract=fake)
    assert store["a.go"]["sha256"] == sha(texts["a.go"])
    assert report["changed"] == {"a.go": sha(texts["a.go"])}


# ---------------------------------------------------------------- the store

def test_entities_store_round_trip(tmp_path):
    path = str(tmp_path / "entities.json")
    entities = {"a.go": record("package m\n", stamp="s")}
    go_extract.save_entities(path, "iryo", entities, helper_revision="h")
    with open(path) as f:
        doc = json.load(f)
    assert set(doc) == {"version", "repo", "extractor_revision", "helper_revision", "entities"}
    assert doc["version"] == go_extract.ENTITIES_VERSION and doc["repo"] == "iryo"
    assert doc["extractor_revision"] == go_extract.EXTRACTOR_REVISION and doc["helper_revision"] == "h"
    assert go_extract.load_entities(path, "iryo") == entities
    assert go_extract.load_entities(path, "other") == {}
    assert go_extract.load_entities(str(tmp_path / "missing.json"), "iryo") == {}
    doc["version"] = 99
    with open(path, "w") as f:
        json.dump(doc, f)
    assert go_extract.load_entities(path, "iryo") == {}
    with open(path, "w") as f:
        f.write("{not json")
    assert go_extract.load_entities(path, "iryo") == {}


def test_save_entities_is_atomic(tmp_path, monkeypatch):
    path = str(tmp_path / "entities.json")
    go_extract.save_entities(path, "r", {"a.go": record("x", stamp="s")}, helper_revision="h")

    def full_disk(*args, **kwargs):     # the temp file opens, but writing to it fails
        f = open(*args, **kwargs)
        f.write = lambda _text: (_ for _ in ()).throw(OSError("disk full"))
        return f

    monkeypatch.setattr(go_extract, "open", full_disk, raising=False)
    with pytest.raises(OSError, match="disk full"):
        go_extract.save_entities(path, "r", {}, helper_revision="h")
    assert go_extract.load_entities(path, "r") != {}
    assert os.listdir(tmp_path) == ["entities.json"]


# ---------------------------------------------------------------- declaration spans

def test_declaration_spans_are_the_top_level_declarations():
    rec = record("x", types=[
        {"name": "<package>", "qualname": "<package>", "kind": "package", "start_line": 1, "end_line": 40},
        {"name": "T", "qualname": "T", "kind": "struct", "start_line": 3, "end_line": 6,
         "methods": []},
        {"name": "I", "qualname": "I", "kind": "interface", "start_line": 8, "end_line": 10,
         "methods": [{"name": "M", "qualname": "I.M", "start_line": 9, "end_line": 9}]},
    ], funcs=[
        {"qualname": "<init>", "member_kind": "var_init", "start_line": 12, "end_line": 14},
        {"qualname": "F", "member_kind": "func", "start_line": 16, "end_line": 22},
        {"qualname": "F.func1", "member_kind": "closure", "start_line": 17, "end_line": 19},
        {"qualname": "T.M", "member_kind": "method", "start_line": 24, "end_line": 26},
        {"qualname": "init#1", "member_kind": "init", "start_line": 28, "end_line": 30},
    ])
    assert go_extract.declaration_spans(rec) == [
        {"qualname": "T", "kind": "struct", "start_line": 3, "end_line": 6},
        {"qualname": "I", "kind": "interface", "start_line": 8, "end_line": 10},
        {"qualname": "F", "kind": "func", "start_line": 16, "end_line": 22},
        {"qualname": "T.M", "kind": "method", "start_line": 24, "end_line": 26},
        {"qualname": "init#1", "kind": "init", "start_line": 28, "end_line": 30},
    ]


# ---------------------------------------------------------------- the helper process

FAKE = '''#!{python}
import hashlib, json, os, sys, time
behavior = {behavior!r}
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps({{"args": args, "env": {{k: os.environ.get(k) for k in ("GOOS", "GOARCH", "CGO_ENABLED", "GOFLAGS")}}}}) + "\\n")
if behavior.get("sleep"):
    time.sleep(behavior["sleep"])
if behavior.get("exit"):
    sys.stderr.write("gohelper extract: --root is required\\n")
    sys.exit(behavior["exit"])
opts = dict(zip(args[1::2], args[2::2]))
rels = [l for l in open(opts["--files-from"]).read().split("\\n") if l]
for rel in rels:
    data = open(os.path.join(opts["--root"], rel), "rb").read()
    if data.startswith(b"func"):
        print(json.dumps({{"kind": "file", "relpath": rel, "error": rel + ":1:1: expected 'package', found 'func'"}}))
        continue
    rec = {{"sha256": hashlib.sha256(data).hexdigest(), "module": "", "package": "", "package_name": "m",
           "origin": "package", "generated": False, "build_constraint": "", "build_included": True,
           "syntax_errors": [], "imports": [], "types": [], "funcs": []}}
    print(json.dumps({{"kind": "file", "relpath": rel, "record": rec}}))
if behavior.get("garbage"):
    print("{{not json")
if not behavior.get("no_summary"):
    print(json.dumps({{"kind": "summary", "files": len(rels), "errors": 0, "seconds": 0.01}}))
'''


def fake_helper(tmp_path, **behavior) -> tuple[str, str]:
    path = str(tmp_path / "fake-gohelper")
    log = str(tmp_path / "fake-gohelper.log")
    with open(path, "w") as f:
        f.write(FAKE.format(python=sys.executable, behavior=behavior, log=log))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path, log


def calls(log: str) -> list[dict]:
    if not os.path.exists(log):
        return []
    with open(log) as f:
        return [json.loads(line) for line in f]


def write(root, files: dict[str, str]) -> str:
    for rel, text in files.items():
        path = os.path.join(str(root), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    return str(root)


def test_run_extract_passes_every_file_and_the_build(tmp_path):
    root = write(tmp_path / "repo", {"a.go": "package m\n", "x/b.go": "package x\n", "bad.go": "func F()\n"})
    helper, log = fake_helper(tmp_path)
    build = BuildConfig("linux-arm64+a+b", "linux", "arm64", ("a", "b"), True, True)
    records, errors, summary = go_extract.run_extract(
        helper, root, ["a.go", "bad.go", "x/b.go"], build, env={"PATH": "/usr/bin:/bin"},
        timeout=30, scratch=str(tmp_path / "scratch"))
    [call] = calls(log)
    assert call["args"][0] == "extract"
    opts = dict(zip(call["args"][1::2], call["args"][2::2]))
    assert opts["--root"] == root and opts["--goos"] == "linux" and opts["--goarch"] == "arm64"
    assert opts["--tags"] == "a,b" and opts["--cgo"] == "1"
    assert sorted(records) == ["a.go", "x/b.go"] and records["a.go"]["sha256"] == sha("package m\n")
    assert list(errors) == ["bad.go"]
    assert summary["kind"] == "summary" and summary["files"] == 3
    assert os.listdir(tmp_path / "scratch") == []        # the file list is removed


@pytest.mark.parametrize("behavior,message", [
    ({"exit": 2}, "--root is required"),
    ({"no_summary": True}, "no summary"),
    ({"garbage": True}, "not JSON"),
    ({"sleep": 5}, "timed out"),
])
def test_run_extract_failures_raise(tmp_path, behavior, message):
    root = write(tmp_path / "repo", {"a.go": "package m\n"})
    helper, _log = fake_helper(tmp_path, **behavior)
    with pytest.raises(ExtractError, match=message):
        go_extract.run_extract(helper, root, ["a.go"], PRIMARY, env={"PATH": "/usr/bin:/bin"},
                               timeout=1, scratch=str(tmp_path / "scratch"))


# ---------------------------------------------------------------- the phase

class Data:
    """The PipelineData fields the phase reads and writes."""

    def __init__(self, root, texts, disc, toolchain=None):
        self.target_dir = root
        self.repo = "r"
        self.file_list = [os.path.join(root, rel) for rel in sorted(texts)]
        self.file_hashes = {rel: sha(t) for rel, t in texts.items()}
        self.discovery = disc
        self.toolchain = toolchain
        self.extracted_entities = {}
        self.timings = {}


@pytest.fixture
def phase_env(tmp_path, monkeypatch):
    helper, log = fake_helper(tmp_path)
    monkeypatch.setattr(go_extract, "_helper", lambda data, output_dir: helper)
    monkeypatch.setattr(go_extract, "_helper_env", lambda data, output_dir, build: {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(go_extract, "helper_revision", lambda: "rev1")
    return log


def make_data(tmp_path, texts):
    root = write(tmp_path / "repo", texts)
    disc = discovery(root, {rel: "example.com/m" for rel in texts}, units=[unit("example.com/m", "")])
    return Data(root, texts, disc)


def test_extract_phase_writes_the_store_and_reuses_it(tmp_path, phase_env):
    texts = {"a.go": "package m\n", "b.go": "package m\n\nfunc B() {}\n"}
    data = make_data(tmp_path, texts)
    out_root = str(tmp_path / "out" / "repos" / "r")
    lines = []
    stats = go_extract.extract_phase(data, out_root=out_root, output_dir=str(tmp_path / "out"),
                                     printer=lines.append)
    assert stats["extracted"] == 2 and stats["reused"] == 0 and stats["failed"] == {}
    assert sorted(data.extracted_entities) == ["a.go", "b.go"]
    assert all(r["stamp"] for r in data.extracted_entities.values())
    assert "phase1b_extract" in data.timings
    assert any("[Phase 1.5]" in line for line in lines)
    assert go_extract.load_entities(os.path.join(out_root, "entities.json"), "r") == data.extracted_entities
    # A second run in a new process reuses entities.json without the helper.
    data2 = make_data(tmp_path, texts)
    stats = go_extract.extract_phase(data2, out_root=out_root, output_dir=str(tmp_path / "out"),
                                     printer=lines.append)
    assert stats == {**stats, "extracted": 0, "reused": 2}
    assert len(calls(phase_env)) == 1


def test_extract_phase_reports_unparseable_files(tmp_path, phase_env):
    data = make_data(tmp_path, {"a.go": "package m\n", "bad.go": "func F() {}\n"})
    lines = []
    stats = go_extract.extract_phase(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                                     printer=lines.append)
    assert list(stats["failed"]) == ["bad.go"] and "bad.go" not in data.extracted_entities
    assert any("bad.go" in line for line in lines)


def test_extract_phase_takes_the_hash_of_a_file_changed_after_the_scan(tmp_path, phase_env):
    data = make_data(tmp_path, {"a.go": "package m\n"})
    write(data.target_dir, {"a.go": "package m\n\nfunc Later() {}\n"})
    go_extract.extract_phase(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                             printer=lambda _l: None)
    assert data.file_hashes["a.go"] == sha("package m\n\nfunc Later() {}\n")
    assert data.extracted_entities["a.go"]["sha256"] == data.file_hashes["a.go"]


def test_refresh_entities_reextracts_changed_files_and_drops_deleted_ones(tmp_path, phase_env):
    texts = {"a.go": "package m\n", "b.go": "package m\n", "c.go": "package m\n"}
    data = make_data(tmp_path, texts)
    go_extract.extract_phase(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                             printer=lambda _l: None)
    write(data.target_dir, {"b.go": "package m\n\nfunc B() {}\n"})
    os.remove(os.path.join(data.target_dir, "c.go"))
    pending = go_extract.refresh_entities(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                                          printer=lambda _l: None)
    assert pending == ["b.go"]
    assert sorted(data.extracted_entities) == ["a.go", "b.go"]
    assert sorted(data.file_hashes) == ["a.go", "b.go"]
    assert data.file_list == [os.path.join(data.target_dir, r) for r in ("a.go", "b.go")]
    assert data.extracted_entities["b.go"]["sha256"] == sha("package m\n\nfunc B() {}\n")
    assert len(calls(phase_env)) == 2


def test_refresh_entities_without_changes_runs_no_helper(tmp_path, phase_env):
    data = make_data(tmp_path, {"a.go": "package m\n"})
    go_extract.extract_phase(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                             printer=lambda _l: None)
    assert go_extract.refresh_entities(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                                       printer=lambda _l: None) == []
    assert len(calls(phase_env)) == 1


def test_refresh_entities_raises_when_a_file_loses_its_record(tmp_path, phase_env):
    data = make_data(tmp_path, {"a.go": "package m\n", "b.go": "package m\n"})
    go_extract.extract_phase(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                             printer=lambda _l: None)
    write(data.target_dir, {"b.go": "func Broken() {}\n"})
    with pytest.raises(StaleEntitiesError) as exc:
        go_extract.refresh_entities(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path),
                                    printer=lambda _l: None)
    assert exc.value.relpaths == ["b.go"] and "b.go" in str(exc.value)


# ---------------------------------------------------------------- the real helper

@pytest.mark.go
def test_extract_phase_with_the_real_helper(tmp_path):
    from go_worlds import GO, have_go
    from graph_generator import go_toolchain
    if not have_go():
        pytest.skip("no Go toolchain")
    texts = {"go.mod": "module example.com/m\n\ngo 1.26\n",
             "a.go": "package m\n\n// T is a type.\ntype T struct{}\n\nfunc (t *T) M() { helper() }\n",
             "b.go": "package m\n\nfunc helper() { func() {}() }\n"}
    root = write(tmp_path / "repo", texts)
    go_files = {r: t for r, t in texts.items() if r.endswith(".go")}
    disc = discovery(os.path.realpath(root), {r: "example.com/m" for r in go_files},
                     units=[unit("example.com/m", "")])
    data = Data(os.path.realpath(root), go_files, disc, toolchain=go_toolchain.probe_toolchain(GO))
    stats = go_extract.extract_phase(data, out_root=str(tmp_path / "out" / "repos" / "r"),
                                     output_dir=str(tmp_path / "out"), printer=print)
    assert stats["extracted"] == 2 and stats["failed"] == {}
    a, b = data.extracted_entities["a.go"], data.extracted_entities["b.go"]
    assert a["module"] == "example.com/m" and a["sha256"] == sha(texts["a.go"])
    method = [f for f in a["funcs"] if f["qualname"] == "T.M"][0]
    assert method["receiver"]["owner_relpath"] == "a.go" and method["sites"][0]["name"] == "helper"
    assert [f["qualname"] for f in b["funcs"]] == ["helper", "helper.func1"]


def test_helper_env_is_the_allowlist_for_the_primary_build(tmp_path, monkeypatch):
    from graph_generator import go_toolchain
    monkeypatch.setattr(go_toolchain, "base_env", lambda tc, out: {"PATH": "/tc/bin", "GOROOT": "/tc"})

    class D:
        toolchain = object()
    build = BuildConfig("linux-arm64-cgo", "linux", "arm64", (), True, True)
    env = go_extract._helper_env(D(), str(tmp_path), build)
    assert env["GOOS"] == "linux" and env["GOARCH"] == "arm64" and env["CGO_ENABLED"] == "1"
    assert env["GOWORK"] == "off" and env["GOPROXY"] == "off" and env["PATH"] == "/tc/bin"


def test_primary_build_and_toolchain_fallbacks(monkeypatch):
    from graph_generator import go_toolchain

    class D:
        discovery = None
        toolchain = None
    assert go_extract._primary_build(D()).id == "linux-amd64"
    d = D()
    d.discovery = discovery("/r", {}, builds=(BuildConfig("w", "windows", "amd64", (), False, False),))
    assert go_extract._primary_build(d).id == "w"
    monkeypatch.setattr(go_toolchain, "find_toolchain", lambda: "tc")
    assert go_extract._toolchain(d) == "tc" and d.toolchain == "tc"
    assert go_extract.file_stamps(["a.go"], None, PRIMARY, "h") == {
        "a.go": go_extract.entity_stamp(helper_revision="h", build=PRIMARY, gomod=None, module=None)}
    assert go_extract.update_entities({"gone.go": {}}, file_hashes={}, stamps={}, extract=None) == (
        {}, {"extracted": 0, "reused": 0, "failed": {}, "changed": {}})


def test_many_failures_are_summarized(tmp_path, phase_env):
    texts = {f"bad{i:02d}.go": "func F() {}\n" for i in range(25)}
    data = make_data(tmp_path, texts)
    lines = []
    go_extract.extract_phase(data, out_root=str(tmp_path / "o"), output_dir=str(tmp_path), printer=lines.append)
    assert sum("cannot extract" in line for line in lines) == 20
    assert any("and 5 more files without entities" in line for line in lines)
