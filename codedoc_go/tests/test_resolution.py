"""resolution (Phase 1.6, spec §5, C2/C5): group loads per build, the
(relpath, offset) join, file-level reasons, freshness and the artifact.

The default run replays the helper outputs recorded in
tests/fixtures/helper/basic/ (helper_fixtures.py) against a copy of that repo."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil

import pytest

from graph_generator import resolution
from graph_generator.go_extract import StaleEntitiesError
from graph_generator.go_model import (POSSIBLY_REASONS, STATUSES, UNRESOLVED_REASONS, VIA,
                                      BuildConfig, Discovery, GoUnit, Group, Replace)
from graph_generator.resolution import HelperRun
from helper_fixtures import replay

CASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "helper", "basic")
GOROOT = "/fake/goroot"
LINUX = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)
WINDOWS = BuildConfig("windows-amd64", "windows", "amd64", (), False, False)


def unit(key, root):
    return GoUnit(key=key, root=root, go="1.26", toolchain=None, requires=(), replaces=(), vendor=False,
                  in_repo_deps=())


class World:
    """A copy of the basic repo, its recorded helper outputs and a discovery."""

    def __init__(self, tmp_path, builds=(LINUX, WINDOWS)):
        self.repo = os.path.realpath(str(tmp_path / "repo"))
        shutil.copytree(os.path.join(CASE, "repo"), self.repo)
        self.out = os.path.realpath(str(tmp_path / "out"))
        extract, self.resolves, self.meta = replay(CASE, self.repo, GOROOT, self.out)
        self.entities = {l["relpath"]: l["record"] for l in extract if "record" in l}
        self.file_hashes = {rel: r["sha256"] for rel, r in self.entities.items()}
        units = [unit("example.com/app", "app"), unit("example.com/lib", "lib"), unit("example.com/solo", "solo")]
        owner = {"app": "example.com/app", "lib": "example.com/lib", "solo": "example.com/solo"}
        ws = Group(id="ws0", kind="workspace", dir=os.path.join(self.out, "repos", "basic", "go", "ws0"),
                   modules=["example.com/app", "example.com/lib"],
                   gowork=os.path.join(self.out, "repos", "basic", "go", "ws0", "go.work"), modfile=None,
                   vendor=False, degraded=False, status="ok", pattern="work")
        solo = Group(id="m0", kind="module", dir=os.path.join(self.repo, "solo"), modules=["example.com/solo"],
                     gowork=None, modfile=os.path.join(self.out, "repos", "basic", "go", "m0", "go.mod"),
                     vendor=False, degraded=False, status="toolchain", pattern="./...",
                     warnings=['solo/go.mod:3: "go 1.99" is newer than the toolchain go1.27.2'])
        self.disc = Discovery(repo="basic", repo_root=self.repo, units=units,
                              unit_of_file={rel: owner[rel.split("/")[0]] for rel in self.entities
                                            if rel.split("/")[0] in owner},
                              groups=[ws, solo], builds=list(builds), toolchain="go1.27.2", mode="offline",
                              module_source="none", peers=[], skipped={}, warnings=[])
        self.calls = []
        self.override = {}

    def runner(self, argv, env, cwd, out_path):
        opts = dict(zip(argv[2::2], argv[3::2]))
        key = (opts["--dir"].rstrip("/").split("/")[-1], opts["--build-id"])
        self.calls.append({"argv": argv, "opts": opts, "env": env, "cwd": cwd, "out": out_path})
        if key in self.override:
            return self.override[key]
        group = "ws0" if opts["--pattern"] == "work" else "m0"
        return HelperRun(records=json.loads(json.dumps(self.resolves[(group, opts["--build-id"])])))

    def env_for(self, group, build):
        return {"GOOS": build.goos, "GOARCH": build.goarch, "GOMODCACHE": os.path.join(self.out, "go", "modcache"),
                "GOCACHE": os.path.join(self.out, "go", "gocache")}

    def run(self, **kw):
        return resolution.run_resolution(self.entities, self.disc, file_hashes=self.file_hashes,
                                         helper="/bin/gohelper", runner=kw.get("runner", self.runner),
                                         env_for=self.env_for, goroot=GOROOT,
                                         out_root=os.path.join(self.out, "repos", "basic"),
                                         engine={"name": "go/types"}, printer=kw.get("printer", lambda _l: None))


def calls_by(artifact, rel):
    return {(c["site"]["name"], c["site"]["line"]): c for c in artifact["files"][rel]["calls"]}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_recorded_fixture_matches_the_repo(world):
    assert set(world.entities) == {"app/main.go", "lib/lib.go", "lib/twin_linux.go", "lib/twin_windows.go",
                                   "lib/broken/broken.go", "loose/loose.go", "solo/solo.go"}
    assert world.meta["groups"] == {"ws0": "ok", "m0": "toolchain"}


def test_the_basic_repo_end_to_end(world):
    art = world.run()
    main = calls_by(art, "app/main.go")
    lib = "lib/lib.go"

    def tgt(c):
        return (c["target"]["relpath"], c["target"]["qualname"]) if c["target"] else None

    assert (main["Hello", 11]["status"], tgt(main["Hello", 11])) == ("resolved", (lib, "Base.Hello"))
    assert tgt(main["Say", 12]) == (lib, "Say")
    assert (main["ToUpper", 13]["status"], main["ToUpper", 13]["external"]["module"]) == ("external", "std")
    assert (main["f", 15]["via"], tgt(main["f", 15])) == ("convention:func-alias", (lib, "Format"))
    assert (main["func", 16]["via"], tgt(main["func", 16])) == ("convention:funclit", ("app/main.go", "main.func1"))
    assert (main["Seq", 17]["site"]["kind"], main["Seq", 17]["via"], tgt(main["Seq", 17])) == \
        ("range", "convention:range", (lib, "Seq"))
    assert main["len", 21]["status"] == "builtin"
    native = main["Native", 22]
    assert tgt(native) == ("lib/twin_linux.go", "Native")
    assert [b["relpath"] for b in native["build_variants"]] == ["lib/twin_windows.go"]
    greet = main["Greet", 24]
    assert (greet["status"], tgt(greet)) == ("resolved", (lib, "Greeter.Greet"))
    assert [c["qualname"] for c in greet["candidates"]] == ["English.Greet"]
    anon = main["Hello", 25]
    recorded = [r for r in world.resolves[("ws0", "linux-amd64")]
                if r["kind"] == "site" and r["line"] == 25 and r["name"] == "Hello"][0]
    if recorded["iface"]["recv_type"] == "":
        assert anon["status"] == "ambiguous" and anon["reason"] == "implementation"
    else:            # an interface literal reported by its type string: no node, G7
        assert (anon["status"], anon["reason"]) == ("unresolved", "no-declaration")
    assert tgt(main["Total", 26]) == (lib, "Total")
    assert ("byte", 20) not in main and ("any", 25) not in main        # conversions are dropped

    lib_calls = calls_by(art, lib)
    assert lib_calls["Greet", 22]["target"]["qualname"] == "Greeter.Greet"
    assert lib_calls["Sprint", 26]["external"]["qualname"] == "Sprint"
    assert lib_calls["yield", 30]["status"] == "dynamic"
    assert not [k for k in lib_calls if k[0] == "xs"]                  # range over a slice: not a call
    win = calls_by(art, "lib/twin_windows.go")["windowsName", 4]
    assert (win["status"], win["build"], tgt(win)) == ("resolved", "windows-amd64",
                                                       ("lib/twin_windows.go", "windowsName"))
    assert calls_by(art, "lib/broken/broken.go")["ToLower", 6]["status"] == "external"
    assert [(c["status"], c["reason"]) for c in art["files"]["loose/loose.go"]["calls"]] == [("unresolved", "no-module")]
    assert [(c["status"], c["reason"]) for c in art["files"]["solo/solo.go"]["calls"]] == [("unresolved", "toolchain")]

    stats = art["stats"]
    assert stats["conversions"] == 2 and stats["notcalls"] == 1 and stats["join_misses"] == 0
    assert stats["sites_total"] == sum(stats["by_status"].values()) == sum(len(f["calls"]) for f in art["files"].values())
    assert art["groups"]["ws0"]["builds"] == ["linux-amd64", "windows-amd64"]
    assert art["groups"]["m0"]["status"] == "toolchain"
    for entry in art["files"].values():
        for c in entry["calls"]:
            assert c["status"] in STATUSES and c["via"] in VIA
            assert c["reason"] is None or c["reason"] in UNRESOLVED_REASONS + POSSIBLY_REASONS
            assert set(c["site"]) == {"caller", "kind", "name", "line", "col"}


def test_each_group_load_gets_its_command_line(world):
    world.run()
    assert [(c["opts"]["--build-id"], c["opts"]["--pattern"]) for c in world.calls] == \
        [("linux-amd64", "work"), ("windows-amd64", "work")]                 # m0 is toolchain-gated
    first = world.calls[0]
    assert first["argv"][:2] == ["/bin/gohelper", "resolve"]
    assert first["opts"]["--repo-root"] == world.repo
    assert first["opts"]["--gowork"].endswith("ws0/go.work") and first["opts"]["--modfile"] == ""
    assert first["opts"]["--main-modules"] == "example.com/app,example.com/lib"
    assert first["opts"]["--goos"] == "linux" and first["opts"]["--cgo"] == "0"
    assert first["cwd"] == world.disc.groups[0].dir
    assert first["out"].endswith(os.path.join("go", "ws0", "resolve-linux-amd64.jsonl"))


def test_an_extra_build_runs_only_while_files_are_uncovered(tmp_path):
    w = World(tmp_path, builds=(LINUX, BuildConfig("linux-arm64", "linux", "arm64", (), False, False)))
    w.resolves[("ws0", "linux-arm64")] = w.resolves[("ws0", "linux-amd64")]
    w.run()
    assert len(w.calls) == 2           # twin_windows.go is still uncovered after linux-amd64
    w2 = World(tmp_path / "second", builds=(LINUX,))
    w2.entities.pop("lib/twin_windows.go")
    w2.file_hashes.pop("lib/twin_windows.go")
    del w2.disc.unit_of_file["lib/twin_windows.go"]
    w2.disc.builds.append(BuildConfig("linux-arm64", "linux", "arm64", (), False, False))
    w2.run()
    assert len(w2.calls) == 1          # every ws0 file is covered by the primary build


def test_without_the_extra_build_the_windows_twin_is_excluded(tmp_path):
    w = World(tmp_path, builds=(LINUX,))
    art = w.run()
    assert [(c["status"], c["reason"], c["build"]) for c in art["files"]["lib/twin_windows.go"]["calls"]] == \
        [("unresolved", "excluded-by-build", None)]


@pytest.mark.parametrize("run,reason", [
    (HelperRun(returncode=1, stderr="go: go.work requires go >= 1.29 (running go 1.27.2)"), "toolchain"),
    (HelperRun(returncode=-1, stderr="timed out after 1800s", timed_out=True), "load-error"),
    (HelperRun(records=[{"kind": "error", "pkg": "", "path": "", "line": 0, "msg": "go: malformed go.work"},
                        {"kind": "summary", "packages": 0, "errors": 1}]), "load-error"),
    (HelperRun(records=[{"kind": "file", "path": "/x.go", "sha256": "1", "packages": []}]), "load-error"),
])
def test_r5_a_failed_group_load_leaves_its_sites_unresolved(world, run, reason):
    world.override[("ws0", "linux-amd64")] = run
    lines = []
    art = world.run(printer=lines.append)
    group_files = [rel for rel in art["files"] if world.disc.unit_of_file.get(rel) in world.disc.groups[0].modules]
    statuses = {(c["status"], c["reason"]) for rel in group_files for c in art["files"][rel]["calls"]}
    assert statuses == {("unresolved", reason)}
    assert art["groups"]["ws0"]["status"] == "error" and art["groups"]["ws0"]["reason"] == reason
    assert art["groups"]["ws0"]["message"]
    assert any("group ws0" in line and "failed to load" in line for line in lines)
    assert len(world.calls) == 1        # no extra build after a failure
    assert [(c["status"], c["reason"]) for c in art["files"]["solo/solo.go"]["calls"]] == [("unresolved", "toolchain")]


def test_a_discovery_error_group_is_not_loaded(world):
    world.disc.groups[0].status = "error"
    art = world.run()
    assert world.calls == []
    assert {c["reason"] for c in art["files"]["app/main.go"]["calls"]} == {"load-error"}


def test_entities_that_differ_from_the_disk_are_refused_before_any_load(world):
    with open(os.path.join(world.repo, "app", "main.go"), "a") as f:
        f.write("\n// edited\n")
    with pytest.raises(StaleEntitiesError) as exc:
        world.run()
    assert exc.value.relpaths == ["app/main.go"] and world.calls == []


def test_a_file_changed_during_the_run_is_stale_with_the_records_pointing_into_it(world):
    def runner(argv, env, cwd, out_path):
        run = world.runner(argv, env, cwd, out_path)
        with open(os.path.join(world.repo, "lib", "lib.go"), "a") as f:
            f.write("\n// edited during the load\n")
        return run
    art = world.run(runner=runner)
    assert {(c["status"], c["reason"]) for c in art["files"]["lib/lib.go"]["calls"]} == {("unresolved", "stale")}
    main = calls_by(art, "app/main.go")
    assert (main["Say", 12]["status"], main["Say", 12]["reason"]) == ("unresolved", "stale")
    assert main["ToUpper", 13]["status"] == "external"                    # points nowhere in lib.go
    assert art["stats"]["stale"] > 0
    lib = art["files"]["lib/lib.go"]
    assert lib["sha256"] != world.entities["lib/lib.go"]["sha256"]        # the bytes on disk now
    assert art["files"]["app/main.go"]["sha256"] == world.entities["app/main.go"]["sha256"]
    for key in ("imports", "embeds", "implements"):
        assert lib[key] and {(r["status"], r["reason"]) for r in lib[key]} == {("unresolved", "stale")}


def test_a_load_that_saw_other_bytes_is_stale(world):
    records = json.loads(json.dumps(world.resolves[("ws0", "linux-amd64")]))
    for r in records:
        if r["kind"] == "file" and r["path"].endswith("lib/twin_linux.go"):
            r["sha256"] = "changed-during-load"
    world.override[("ws0", "linux-amd64")] = HelperRun(records=records)
    art = world.run()
    native = calls_by(art, "app/main.go")["Native", 22]
    assert (native["status"], native["reason"]) == ("unresolved", "stale")
    assert art["files"]["lib/twin_linux.go"]["sha256"] == "changed-during-load"


def test_a_load_that_does_not_own_a_file_still_reports_its_bytes(world):
    """The windows load reads lib/lib.go too; if it saw other bytes, every
    position mapped into lib.go is untrusted (C5 ruling 2026-10-14)."""
    records = json.loads(json.dumps(world.resolves[("ws0", "windows-amd64")]))
    for r in records:
        if r["kind"] == "file" and r["path"].endswith("lib/lib.go"):
            r["sha256"] = "0" * 64
    world.override[("ws0", "windows-amd64")] = HelperRun(records=records)
    art = world.run()
    assert art["files"]["lib/lib.go"]["sha256"] == "0" * 64
    assert calls_by(art, "app/main.go")["Say", 12]["reason"] == "stale"


def test_a_covered_file_without_entities_does_not_stop_the_run(world):
    world.entities.pop("lib/twin_linux.go")
    art = world.run()
    assert "lib/twin_linux.go" not in art["files"]
    native = calls_by(art, "app/main.go")["Native", 22]
    assert (native["status"], native["reason"]) == ("unresolved", "no-declaration")   # no node there


def test_a_site_missing_from_a_covered_file_is_a_load_error(world):
    records = [r for r in world.resolves[("ws0", "linux-amd64")]
               if not (r["kind"] == "site" and r["name"] == "Say")]
    world.override[("ws0", "linux-amd64")] = HelperRun(records=records)
    art = world.run()
    assert (calls_by(art, "app/main.go")["Say", 12]["reason"]) == "load-error"
    assert art["stats"]["join_misses"] == 1


def test_symlinked_files_join_through_their_real_path(world):
    real = os.path.join(world.repo, "lib", "twin_linux.go")
    shared = os.path.join(world.repo, "shared")
    os.makedirs(shared)
    shutil.move(real, os.path.join(shared, "twin_linux.go"))
    os.symlink(os.path.join(shared, "twin_linux.go"), real)
    records = json.loads(json.dumps(world.resolves[("ws0", "linux-amd64")]))
    for r in records:
        for key in ("path",):
            if isinstance(r.get(key), str) and r[key].endswith("lib/twin_linux.go"):
                r[key] = os.path.join(shared, "twin_linux.go")
        if r.get("kind") == "site" and r.get("target") and r["target"]["path"].endswith("lib/twin_linux.go"):
            r["target"]["path"] = os.path.join(shared, "twin_linux.go")
    world.override[("ws0", "linux-amd64")] = HelperRun(records=records)
    art = world.run()
    native = calls_by(art, "app/main.go")["Native", 22]
    assert (native["status"], native["target"]["relpath"]) == ("resolved", "lib/twin_linux.go")


def test_imports_embeds_and_implements(world):
    art = world.run()
    imports = {i["import_path"]: i for i in art["files"]["app/main.go"]["imports"]}
    assert imports["strings"]["status"] == "external" and imports["strings"]["external"]["module"] == "std"
    assert imports["example.com/lib"]["status"] == "resolved"
    assert imports["example.com/lib"]["target"] == {"unit": "example.com/lib", "package": "example.com/lib",
                                                    "dir": "lib", "clause": "lib"}
    assert set(imports["example.com/lib"]) == {"line", "col", "import_path", "status", "target", "external",
                                               "reason"}
    embeds = art["files"]["lib/lib.go"]["embeds"]
    assert embeds == [{"type": {"relpath": "lib/lib.go", "qualname": "Child"},
                       "embedded": {"unit": "example.com/lib", "package": "example.com/lib",
                                    "relpath": "lib/lib.go", "qualname": "Base", "line": 13, "kind": "class"},
                       "status": "resolved", "external": None, "pointer": False, "reason": None}]
    impl = {(i["type"]["relpath"], i["type"]["qualname"], i["iface"]["qualname"], i["status"])
            for i in art["files"]["lib/lib.go"]["implements"]}
    assert ("lib/lib.go", "English", "Greeter", "resolved") in impl
    assert all(set(i) == {"type", "iface", "status", "external", "pointer_receiver", "reason"}
               and i["reason"] is None for i in art["files"]["lib/lib.go"]["implements"])


def test_the_artifact(world, tmp_path):
    art = world.run()
    assert art["version"] == resolution.RESOLUTIONS_VERSION and art["repo"] == "basic"
    assert set(art) == {"version", "engine", "repo", "groups", "files", "stats"}
    for entry in art["files"].values():
        assert set(entry) == {"sha256", "calls", "imports", "embeds", "implements"}
    path = str(tmp_path / "x" / "resolutions.json")
    resolution.write_json_atomic(art, path)
    with open(path) as f:
        assert json.load(f) == json.loads(json.dumps(art))
    assert os.listdir(tmp_path / "x") == ["resolutions.json"]


def test_timings_live_in_stats_so_the_artifact_hash_is_stable(world):
    from graph_generator.pipeline import resolutions_hash
    first = world.run()
    second = world.run()
    assert resolutions_hash(first) == resolutions_hash(second)
    assert "seconds" not in json.dumps(first["groups"])


# ---------------------------------------------------------------- the phase

def test_resolve_phase_wires_the_pieces(world, monkeypatch, tmp_path):
    from graph_generator import go_modcache, go_toolchain, go_units
    from graph_generator.go_model import Toolchain
    events = []
    monkeypatch.setattr(go_units, "prepare_control_dirs", lambda disc, out: events.append(("control", out)))
    monkeypatch.setattr(go_toolchain, "ensure_helper", lambda tc, out: "/bin/gohelper")
    monkeypatch.setattr(go_toolchain, "env_for", lambda g, b, **kw: world.env_for(g, b))
    monkeypatch.setattr(go_modcache, "user_modcache", lambda tc: None)
    monkeypatch.setattr(go_modcache, "download", lambda disc, tc, out: events.append(("download", out)) or {})
    monkeypatch.setattr(resolution, "helper_version", lambda helper: {"x_tools": "v0.52.0", "revision": "abc"})
    monkeypatch.setattr(resolution, "run_helper", lambda argv, env, cwd, out, timeout: world.runner(argv, env, cwd, out))
    refreshed = []
    monkeypatch.setattr(resolution, "refresh_entities", lambda data, **kw: refreshed.append(kw) or [])

    class Data:
        pass
    data = Data()
    data.toolchain = Toolchain(go="/fake/go", version="go1.27.2", goroot=GOROOT)
    data.discovery, data.extracted_entities, data.file_hashes = world.disc, world.entities, world.file_hashes
    data.timings, data.resolutions = {}, {}
    out_root = os.path.join(world.out, "repos", "basic")
    lines = []
    art = resolution.resolve_phase(data, out_root=out_root, output_dir=world.out, printer=lines.append)
    assert data.resolutions is art and "phase1c_resolve" in data.timings
    assert art["engine"] == {"name": "go/types", "toolchain": "go1.27.2", "x_tools": "v0.52.0",
                             "helper": "sha256:abc", "entities_version": 1, "mode": "offline",
                             "builds": ["linux-amd64", "windows-amd64"]}
    with open(os.path.join(out_root, "resolutions.json")) as f:
        assert json.load(f)["stats"]["sites_total"] == art["stats"]["sites_total"]
    assert events == [("control", world.out)] and len(refreshed) == 1
    assert any(line.startswith("[Phase 1.6] Resolved") for line in lines)
    world.disc.mode = "download"
    resolution.resolve_phase(data, out_root=out_root, output_dir=world.out, printer=lines.append)
    assert ("download", world.out) in events


def test_resolve_phase_prints_the_download_warnings(world, monkeypatch):
    """go_modcache.download adds its warnings (missing requirements, failed
    modules) after Phase 1 wrote units.json and printed the discovery
    warnings, so Phase 1.6 prints them; the earlier ones are not repeated."""
    from graph_generator import go_modcache, go_toolchain, go_units
    from graph_generator.go_model import Toolchain
    monkeypatch.setattr(go_units, "prepare_control_dirs", lambda disc, out: None)
    monkeypatch.setattr(go_toolchain, "ensure_helper", lambda tc, out: "/bin/gohelper")
    monkeypatch.setattr(go_toolchain, "env_for", lambda g, b, **kw: world.env_for(g, b))
    monkeypatch.setattr(resolution, "helper_version", lambda helper: {"x_tools": "v0.52.0", "revision": "abc"})
    monkeypatch.setattr(resolution, "run_helper", lambda argv, env, cwd, out, timeout: world.runner(argv, env, cwd, out))
    monkeypatch.setattr(resolution, "refresh_entities", lambda data, **kw: [])
    added = ["group ws0: go.mod lacks requirements for: example.com/x@v1.0.0 (imported by example.com/app)",
             "group ws0: 1 module(s) could not be downloaded, so calls into them are unresolved"]

    def download(disc, tc, out):
        disc.warnings.extend(added)
        return {"ws0": {"status": "partial"}}
    monkeypatch.setattr(go_modcache, "download", download)

    class Data:
        pass
    data = Data()
    data.toolchain = Toolchain(go="/fake/go", version="go1.27.2", goroot=GOROOT)
    data.discovery, data.extracted_entities, data.file_hashes = world.disc, world.entities, world.file_hashes
    data.timings, data.resolutions = {}, {}
    world.disc.mode = "download"
    world.disc.warnings.append("printed by Phase 1 already")
    lines = []
    resolution.resolve_phase(data, out_root=os.path.join(world.out, "repos", "basic"),
                             output_dir=world.out, printer=lines.append)
    warned = [line for line in lines if line.startswith("[Phase 1.6] warning:")]
    assert warned == [f"[Phase 1.6] warning: {w}" for w in added]
    assert lines.index("[Phase 1.6] Downloaded modules for 1 group(s)") < lines.index(warned[0])


def test_resolve_phase_retries_stale_entities_once(world, monkeypatch):
    from graph_generator import go_modcache, go_toolchain, go_units
    from graph_generator.go_model import Toolchain
    monkeypatch.setattr(go_units, "prepare_control_dirs", lambda disc, out: None)
    monkeypatch.setattr(go_toolchain, "ensure_helper", lambda tc, out: "/bin/gohelper")
    monkeypatch.setattr(go_toolchain, "env_for", lambda g, b, **kw: world.env_for(g, b))
    monkeypatch.setattr(go_modcache, "user_modcache", lambda tc: None)
    monkeypatch.setattr(resolution, "helper_version", lambda helper: {})
    attempts = []

    def run_resolution(entities, disc, **kw):
        attempts.append(1)
        raise StaleEntitiesError(["app/main.go"])
    monkeypatch.setattr(resolution, "run_resolution", run_resolution)
    monkeypatch.setattr(resolution, "refresh_entities", lambda data, **kw: [])

    class Data:
        pass
    data = Data()
    data.toolchain = Toolchain(go="/fake/go", version="go1.27.2", goroot=GOROOT)
    data.discovery, data.extracted_entities, data.file_hashes = world.disc, world.entities, world.file_hashes
    data.timings = {}
    with pytest.raises(StaleEntitiesError):
        resolution.resolve_phase(data, out_root=world.out, output_dir=world.out, printer=lambda _l: None)
    assert len(attempts) == 2


def test_conversions_by_syntax_are_counted_in_every_file(world):
    """R53: extraction drops a call of a type literal from the sites and lists
    it under the file's conversions; they are counted with the conversions in
    every file, loaded or not, and never twice where resolve has a conversion
    record at the same offset."""
    main = world.entities["app/main.go"]
    assert len(main["conversions"]) == 1  # []byte("x"), a conversion record in the load too
    assert all(s["offset"] not in main["conversions"] for fn in main["funcs"] for s in fn["sites"])
    world.entities["loose/loose.go"]["conversions"] = [7, 31]  # a file no load covers
    art = world.run()
    assert art["stats"]["conversions"] == 2 + 2  # []byte("x") and any(x) in main.go, loose's two
    assert [c["reason"] for c in art["files"]["loose/loose.go"]["calls"]] == ["no-module"]


def test_group_toolchain_gate_marks_sites_unresolved(world):
    """Review focus 1: a group whose go line exceeds the toolchain is not
    loaded; its sites are unresolved (toolchain), other groups resolve."""
    art = world.run()
    assert all(c["opts"]["--pattern"] == "work" for c in world.calls)      # m0 never loaded
    solo = art["files"]["solo/solo.go"]["calls"]
    assert [(c["status"], c["reason"], c["target"], c["external"]) for c in solo] == \
        [("unresolved", "toolchain", None, None)]
    assert art["groups"]["m0"]["status"] == "toolchain" and "go 1.99" in art["groups"]["m0"]["message"]
    assert calls_by(art, "app/main.go")["Say", 12]["status"] == "resolved"


def _replace(world, key, old, new, local=True):
    """Give unit *key* the go.mod replace `old => new` (new: repo-relative, or absolute)."""
    r = Replace(old=old, old_version="", new=os.path.join(world.repo, new) if local else new,
                new_version="" if local else "v1.0.0", local=local)
    world.disc.units = [dataclasses.replace(u, replaces=(r,)) if u.key == key else u
                        for u in world.disc.units]


def test_r54_a_replace_from_another_group_opens_the_interface_calls(world):
    """R54 (probe p17): solo's group m0 builds lib from its in-repo directory,
    so m0's types can reach lib's interface calls, and no load of ws0 knows
    them. Only open_world changes; m0 not loading (toolchain) does not matter."""
    before = world.run()
    _replace(world, "example.com/solo", "example.com/lib", "lib")
    assert resolution._open_world_units(world.disc) == {"example.com/lib": ["m0"]}
    lines = []
    after = world.run(printer=lines.append)
    lib_b, lib_a = calls_by(before, "lib/lib.go"), calls_by(after, "lib/lib.go")
    assert (lib_b["Greet", 22]["open_world"], lib_a["Greet", 22]["open_world"]) == (False, True)
    assert {**lib_a["Greet", 22], "open_world": False} == lib_b["Greet", 22]   # candidates [English.Greet]
    lib_a.pop(("Greet", 22)), lib_b.pop(("Greet", 22))
    assert lib_a == lib_b                                                     # Sprint, yield: no interface
    assert after["files"]["app/main.go"] == before["files"]["app/main.go"]
    assert [l for l in lines if "R54" in l] == [
        "[Phase 1.6] 1 interface call(s) opened: another group builds their module from its in-repo "
        "directory through a local replace (R54): example.com/lib by m0"]


def test_r54_only_the_replaced_module_opens(world):
    before = world.run()
    _replace(world, "example.com/solo", "example.com/app", "app")
    after = world.run()
    assert after["files"]["lib/lib.go"] == before["files"]["lib/lib.go"]
    main = calls_by(after, "app/main.go")
    assert main["Greet", 24]["open_world"] and main["Hello", 25]["open_world"]   # named, anonymous interface
    assert main["Hello", 25]["candidates"] == calls_by(before, "app/main.go")["Hello", 25]["candidates"]
    assert not main["Say", 12]["open_world"]


@pytest.mark.parametrize("key, old, new, local", [
    ("example.com/app", "example.com/other", "lib", True),        # the site module's own group
    ("example.com/solo", "example.com/lib", "/elsewhere/lib", True),   # a directory outside the repo
    ("example.com/solo", "example.com/x", "lib/broken", True),    # a directory that is no module root
    ("example.com/solo", "example.com/lib", "example.com/fork", False),   # a module version
    ("example.com/app", "example.com/lib", "solo", True),         # a main module is never replaced
])
def test_r54_replaces_that_open_nothing(world, key, old, new, local):
    before = world.run()
    _replace(world, key, old, new, local)
    assert resolution._open_world_units(world.disc) == {}
    lines = []
    after = world.run(printer=lines.append)
    assert {rel: f["calls"] for rel, f in after["files"].items()} == \
        {rel: f["calls"] for rel, f in before["files"].items()}
    assert not any("R54" in l for l in lines)


def _work_replace(world, gid, old, new, version=""):
    """Give group *gid* the go.work replace `old [version] => new` (new: repo-relative, or absolute)."""
    g = next(g for g in world.disc.groups if g.id == gid)
    g.replaces = [Replace(old=old, old_version=version, new=os.path.join(world.repo, new), new_version="",
                          local=True)]


def test_r54_a_go_work_replace_opens_another_groups_module(world):
    """R54: solo's group, here a workspace ws1, builds lib through a replace in
    its go.work; lib's interface calls open as with a go.mod replace."""
    before = world.run()
    world.disc.groups[1].id, world.disc.groups[1].kind = "ws1", "workspace"
    _work_replace(world, "ws1", "example.com/lib", "lib")
    assert resolution._open_world_units(world.disc) == {"example.com/lib": ["ws1"]}
    after = world.run()
    lib_b, lib_a = calls_by(before, "lib/lib.go"), calls_by(after, "lib/lib.go")
    assert (lib_b["Greet", 22]["open_world"], lib_a["Greet", 22]["open_world"]) == (False, True)
    assert after["files"]["app/main.go"] == before["files"]["app/main.go"]


def test_r54_a_go_work_replace_outside_the_repo_opens_nothing(world):
    before = world.run()
    world.disc.groups[1].id, world.disc.groups[1].kind = "ws1", "workspace"
    _work_replace(world, "ws1", "example.com/lib", "/elsewhere/lib")
    assert resolution._open_world_units(world.disc) == {}
    after = world.run()
    assert {rel: f["calls"] for rel, f in after["files"].items()} == \
        {rel: f["calls"] for rel, f in before["files"].items()}


def test_r54_a_go_work_replace_overrides_the_members_replaces_of_its_path(world):
    _replace(world, "example.com/app", "example.com/solo", "solo")     # a ws0 member's go.mod
    assert resolution._open_world_units(world.disc) == {"example.com/solo": ["ws0"]}
    _work_replace(world, "ws0", "example.com/solo", "/elsewhere/solo")  # ws0's go.work wins
    assert resolution._open_world_units(world.disc) == {}
    # A go.work replace of one version leaves the member's replace of every other version.
    _work_replace(world, "ws0", "example.com/solo", "/elsewhere/solo", version="v1.0.0")
    assert resolution._open_world_units(world.disc) == {"example.com/solo": ["ws0"]}


# ---------------------------------------------------------------- the real helper

def _project(art):
    out = {}
    for rel, entry in art["files"].items():
        for c in entry["calls"]:
            t = c["target"]
            out[(rel, c["site"]["line"], c["site"]["col"])] = (
                c["status"], c["via"], c["reason"], (t["relpath"], t["qualname"]) if t else None,
                tuple(x["qualname"] for x in c["candidates"]), tuple(x["relpath"] for x in c["build_variants"]))
    return out


@pytest.mark.go
def test_recorded_fixtures_are_current(tmp_path):
    from go_worlds import have_go
    from helper_fixtures import record
    if not have_go():
        pytest.skip("no Go toolchain")
    fresh = record(CASE, str(tmp_path / "rec"))
    for name, text in sorted(fresh.items()):
        if name == "meta.json":
            continue
        with open(os.path.join(CASE, name)) as f:
            assert f.read() == text, (f"{name} no longer matches the helper; re-record with "
                                      f"`.venv/bin/python tests/helper_fixtures.py {CASE}`")


@pytest.mark.go
def test_graph_phases_with_the_real_helper_match_the_replay(tmp_path, monkeypatch):
    from go_worlds import GO, have_go
    from graph_generator import config, go_toolchain, pipeline
    if not have_go():
        pytest.skip("no Go toolchain")
    replayed = World(tmp_path / "replay").run()
    repo = os.path.realpath(str(tmp_path / "real" / "basic"))
    shutil.copytree(os.path.join(CASE, "repo"), repo)
    out = str(tmp_path / "real" / "out")
    monkeypatch.setattr(config, "OUTPUT_DIR", os.path.join(out, "repos", "basic"))
    monkeypatch.setattr(config, "GO_EXTRA_BUILDS", "windows/amd64")
    monkeypatch.setattr(config, "GO_TOOLCHAIN", GO)
    data = pipeline.PipelineData(target_dir=repo)
    data.repo = "basic"
    data.toolchain = go_toolchain.probe_toolchain(GO)
    pipeline.phase1_scan(data)
    pipeline.phase1b_extract(data)
    pipeline.phase1c_resolve(data)
    real = data.resolutions
    assert real["engine"]["builds"] == ["linux-amd64", "windows-amd64"]
    assert real["engine"]["x_tools"] == "v0.52.0" and real["engine"]["helper"].startswith("sha256:")
    assert _project(real) == _project(replayed)
    assert real["stats"]["join_misses"] == 0
    assert os.path.isfile(os.path.join(out, "repos", "basic", "resolutions.json"))
    assert os.path.isfile(os.path.join(out, "repos", "basic", "go", "ws0", "resolve-linux-amd64.jsonl"))


P17 = {
    "go.work": "go 1.26\n\nuse ./ws/lib\n",
    "ws/lib/go.mod": "module example.com/lib\n\ngo 1.26\n",
    "ws/lib/lib.go": "package lib\n\ntype Store interface {\n\tCommit() int\n\tRollback() int\n}\n\n"
                     "func Use(s Store) int {\n\tn := s.Commit()\n\treturn n\n}\n",
    "outside/go.mod": "module example.com/outside\n\ngo 1.26\n\nrequire example.com/lib v0.0.0\n\n"
                      "replace example.com/lib => ../ws/lib\n",
    "outside/main.go": "package main\n\nimport \"example.com/lib\"\n\ntype Impl struct{}\n\n"
                       "func (Impl) Commit() int   { return 1 }\nfunc (Impl) Rollback() int { return 2 }\n\n"
                       "func main() { println(lib.Use(Impl{})) }\n",
}


@pytest.mark.go
def test_r54_probe_p17_with_the_real_helper(tmp_path, monkeypatch):
    """R54 on probe p17's layout: the go.work uses only ws/lib, whose Use(s
    Store) calls s.Commit(); outside/ is no workspace module, replaces
    example.com/lib with ../ws/lib and passes its Impl, which runs there. No
    load of the workspace knows Impl: no candidates, and open-world."""
    from go_worlds import GO, have_go, write_tree
    from graph_generator import config, go_toolchain, pipeline
    if not have_go():
        pytest.skip("no Go toolchain")
    repo = os.path.realpath(write_tree(tmp_path / "p17", P17))
    monkeypatch.setattr(config, "OUTPUT_DIR", os.path.join(str(tmp_path / "out"), "repos", "p17"))
    monkeypatch.setattr(config, "GO_TOOLCHAIN", GO)
    monkeypatch.setattr(config, "GO_USER_MODCACHE", str(tmp_path / "empty-cache"))
    data = pipeline.PipelineData(target_dir=repo)
    data.repo = "p17"
    data.toolchain = go_toolchain.probe_toolchain(GO)
    pipeline.phase1_scan(data)
    pipeline.phase1b_extract(data)
    pipeline.phase1c_resolve(data)
    art = data.resolutions
    assert sorted(g["status"] for g in art["groups"].values()) == ["ok", "ok"]
    commit = calls_by(art, "ws/lib/lib.go")["Commit", 9]
    assert (commit["status"], commit["target"]["qualname"], commit["candidates"], commit["open_world"]) == \
        ("resolved", "Store.Commit", [], True)
    use = calls_by(art, "outside/main.go")["Use", 10]
    assert (use["status"], use["target"]["qualname"], use["open_world"]) == ("resolved", "Use", False)


# ---------------------------------------------------------------- pieces

def _script(tmp_path, name, body):
    import stat
    import sys
    path = str(tmp_path / name)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n{body}")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def test_run_helper_reads_the_out_file(tmp_path):
    helper = _script(tmp_path, "gohelper", """import json, sys
args = sys.argv[1:]
out = args[args.index("--out") + 1]
with open(out, "w") as f:
    f.write(json.dumps({"kind": "summary", "packages": 1}) + "\\n")
sys.stderr.write("gohelper resolve: load 0.1s\\n")
""")
    run = resolution.run_helper([helper, "resolve"], {"PATH": "/usr/bin:/bin"}, str(tmp_path),
                                str(tmp_path / "o" / "r.jsonl"), timeout=30)
    assert (run.returncode, run.records, run.timed_out) == (0, [{"kind": "summary", "packages": 1}], False)
    assert "load 0.1s" in run.stderr and run.seconds > 0


def test_run_helper_failures(tmp_path):
    bad = _script(tmp_path, "bad", """import sys
args = sys.argv[1:]
open(args[args.index("--out") + 1], "w").write("{not json\\n")
""")
    run = resolution.run_helper([bad], {"PATH": "/usr/bin:/bin"}, str(tmp_path), str(tmp_path / "b.jsonl"),
                                timeout=30)
    assert run.returncode != 0 and "not JSON" in run.stderr
    slow = _script(tmp_path, "slow", "import time\ntime.sleep(5)\n")
    run = resolution.run_helper([slow], {"PATH": "/usr/bin:/bin"}, str(tmp_path), str(tmp_path / "s.jsonl"),
                                timeout=1)
    assert run.timed_out and run.returncode == -1 and "timed out" in run.stderr
    gone = _script(tmp_path, "gone", "import sys\nsys.exit(3)\n")
    run = resolution.run_helper([gone], {"PATH": "/usr/bin:/bin"}, str(tmp_path), str(tmp_path / "g.jsonl"),
                                timeout=30)
    assert (run.returncode, run.records) == (3, [])


def test_helper_version(tmp_path):
    helper = _script(tmp_path, "v", 'print(\'{"kind":"version","protocol":1,"x_tools":"v0.52.0","revision":"r"}\')\n')
    assert resolution.helper_version(helper)["x_tools"] == "v0.52.0"
    assert resolution.helper_version(_script(tmp_path, "w", "print('nope')\n")) == {}


def _ctx(world, peers=()):
    from graph_generator.go_classify import ClassifyContext, DeclIndex, Locator
    scanned = {os.path.join(world.repo, rel): rel for rel in world.entities}
    return ClassifyContext(decls=DeclIndex(world.entities),
                           locator=Locator(repo_root=world.repo, scanned=scanned, goroot=GOROOT,
                                           modcaches=[os.path.join(world.out, "go", "modcache")], peers=peers))


def test_import_records_by_location(world, tmp_path):
    from graph_generator.go_model import Peer
    peer_root = os.path.realpath(str(tmp_path / "peer"))
    os.makedirs(os.path.join(peer_root, "money"))
    for name, text in {"money/yen.go": "// Package money.\npackage money\n",
                       "money/yen_test.go": "package money_test\n",
                       "money/gen_windows.go": "package other\n",
                       "money/tool.go": "//go:build ignore\n\npackage main\n"}.items():
        with open(os.path.join(peer_root, name), "w") as f:
            f.write(text)
    peer = Peer(name="shared", root=peer_root, units=(unit("example.com/shared", ""),))
    ctx = _ctx(world, peers=[peer])
    builds = resolution._Builds(world.entities, resolution._Loads(), world.repo)
    mc = os.path.join(world.out, "go", "modcache")

    def rec(**kw):
        base = {"line": 3, "col": 8, "offset": 20, "import_path": "x", "target_dir": "", "module": "",
                "version": "", "std": False, "error": None}
        base.update(kw)
        return resolution._import_record(base, ctx, builds, LINUX, {})

    assert rec(error="package not found")["reason"] == "missing-module"
    assert rec(import_path="C", module="C")["external"]["module"] == "C"
    assert rec(import_path="fmt", std=True)["external"] == {"module": "std", "version": "", "package": "fmt", "peer": False}
    r = rec(import_path="example.com/lib", target_dir=os.path.join(world.repo, "lib"), module="example.com/lib")
    assert (r["status"], r["target"]) == ("resolved", {"unit": "example.com/lib", "package": "example.com/lib",
                                                       "dir": "lib", "clause": "lib"})
    # No scanned file there: nothing a build compiles (never "unscanned", C5 ruling).
    assert rec(import_path="example.com/lib/x", target_dir=os.path.join(world.repo, "lib", "x"))["reason"] == \
        "excluded-by-build"
    r = rec(import_path="example.com/shared/money", target_dir=os.path.join(peer_root, "money"))
    assert (r["status"], r["external"]["peer"], r["target"]) == (
        "external", True, {"repo": "shared", "unit": "example.com/shared", "package": "example.com/shared/money",
                           "dir": "money", "clause": "money"})
    r = rec(import_path="github.com/a/b", target_dir=os.path.join(mc, "github.com/a/b@v1.0.0"),
            module="github.com/a/b", version="v1.0.0")
    assert r["external"] == {"module": "github.com/a/b", "version": "v1.0.0", "package": "github.com/a/b", "peer": False}
    assert rec(import_path="z", target_dir="/elsewhere/z")["reason"] == "outside-repo"
    assert rec(import_path="z")["reason"] == "load-error"


def _import(world, ents, loads, build, rel_dir="lib"):
    ctx = _ctx(world)
    return resolution._import_record({"line": 1, "col": 1, "import_path": "example.com/lib",
                                      "target_dir": os.path.join(world.repo, rel_dir), "module": "example.com/lib"},
                                     ctx, resolution._Builds(ents, loads, world.repo), build, {})


def test_an_import_binds_the_unique_clause_of_the_compiled_files(world):
    ents = dict(world.entities)
    ents["lib/a_gen.go"] = {**ents["lib/lib.go"], "package_name": "main", "build_included": False,
                            "origin": "command"}
    ents["lib/lib_ext_test.go"] = {**ents["lib/lib.go"], "package_name": "lib_test", "origin": "xtest"}
    r = _import(world, ents, resolution._Loads(), LINUX)
    assert (r["status"], r["target"]["clause"]) == ("resolved", "lib")       # never the generator's main
    ents["lib/other.go"] = {**ents["lib/lib.go"], "package_name": "other"}
    r = _import(world, ents, resolution._Loads(), LINUX)
    assert (r["status"], r["reason"], r["target"]) == ("unresolved", "no-declaration", None)


def test_the_clause_follows_the_build_that_answered_the_import(world):
    """fee_linux.go says billing, fee_windows.go winbilling: an importer the
    windows extra build answers binds winbilling (C5 ruling)."""
    lib = world.entities["lib/lib.go"]
    os.makedirs(os.path.join(world.repo, "fee"))
    files = {"fee/fee_linux.go": "package billing\n", "fee/fee_windows.go": "package winbilling\n"}
    for rel, text in files.items():
        with open(os.path.join(world.repo, rel), "w") as f:
            f.write(text)
    ents = {"fee/fee_linux.go": {**lib, "package_name": "billing", "build_included": True},
            "fee/fee_windows.go": {**lib, "package_name": "winbilling", "build_included": False}}
    # From the loads' file records of each build ...
    loads = resolution._Loads(files_by_build={"windows-amd64": {"fee/fee_windows.go"},
                                              "linux-amd64": {"fee/fee_linux.go"}},
                              dirs_by_build={"windows-amd64": {"fee"}, "linux-amd64": {"fee"}})
    assert _import(world, ents, loads, WINDOWS, "fee")["target"]["clause"] == "winbilling"
    assert _import(world, ents, loads, LINUX, "fee")["target"]["clause"] == "billing"
    # ... else the primary build's build_included, or match_file for an extra build.
    assert _import(world, ents, resolution._Loads(), LINUX, "fee")["target"]["clause"] == "billing"
    assert _import(world, ents, resolution._Loads(), WINDOWS, "fee")["target"]["clause"] == "winbilling"
    darwin = BuildConfig("darwin-arm64", "darwin", "arm64", (), False, False)
    r = _import(world, ents, resolution._Loads(), darwin, "fee")
    assert (r["status"], r["reason"]) == ("unresolved", "excluded-by-build")


def test_the_broken_directories_are_those_a_build_compiles(world):
    ents = {"a/x.go": {"syntax_errors": [3], "build_included": True},
            "b/gen.go": {"syntax_errors": [3], "build_included": False},
            "c/x_test.go": {"syntax_errors": [9], "build_included": True},
            "d/ok.go": {"syntax_errors": [], "build_included": True},
            "e/win.go": {"syntax_errors": [2], "build_included": False}}
    loads = resolution._Loads(coverage={"e/win.go": ("ws0", "windows-amd64")})
    builds = resolution._Builds(ents, loads, world.repo)
    assert resolution._broken_dirs(ents, loads, builds, [LINUX]) == frozenset({"a", "c", "e"})


def test_implements_pairs_come_from_every_primary_load_once(world):
    records = json.loads(json.dumps(world.resolves[("ws0", "linux-amd64")]))
    pairs = [r for r in records if r["kind"] == "implements"]
    world.override[("ws0", "linux-amd64")] = HelperRun(records=records + json.loads(json.dumps(pairs)))
    win = json.loads(json.dumps(world.resolves[("ws0", "windows-amd64")]))
    base = {"path": os.path.join(world.repo, "lib", "lib.go"), "line": 13, "col": 6, "name": "Base",
            "pkg": "example.com/lib"}
    extra = {"kind": "implements", "type": base, "iface": {**pairs[0]["iface"]}, "pointer_receiver": False}
    assert resolution._implements_record(extra, _ctx(world)) is not None      # mappable, so the test bites
    world.override[("ws0", "windows-amd64")] = HelperRun(records=win + [extra])
    art = world.run()
    got = [(i["type"]["qualname"], i["iface"]["qualname"]) for i in art["files"]["lib/lib.go"]["implements"]]
    assert len(got) == len(set(got)) == len(pairs)            # duplicates collapse
    assert ("Base", "Greeter") not in got                     # pairs of extra builds are not used


def test_embed_and_implements_ends_by_location(world):
    from graph_generator.go_model import Peer
    peer = Peer(name="shared", root="/peers/shared", units=(unit("example.com/shared", ""),))
    ctx = _ctx(world, peers=[peer])
    child = {"path": os.path.join(world.repo, "lib", "lib.go"), "line": 17, "col": 6, "name": "Child",
             "pkg": "example.com/lib"}
    universe = {"path": "", "line": 0, "col": 0, "name": "error", "pkg": "", "obj": "type"}
    shared = {"path": "/peers/shared/base.go", "line": 3, "col": 6, "name": "Base", "pkg": "example.com/shared",
              "obj": "type"}
    cached = {"path": os.path.join(world.out, "go", "modcache", "github.com/a/b@v1.2.0/b.go"), "line": 1, "col": 6,
              "name": "B", "pkg": "github.com/a/b", "obj": "type"}
    greeter = {"path": os.path.join(world.repo, "lib", "lib.go"), "line": 5, "col": 6, "name": "Greeter",
               "pkg": "example.com/lib"}
    assert resolution._implements_record({"type": child, "iface": child}, ctx) is None    # not an interface
    i = resolution._implements_record({"type": child, "iface": greeter, "pointer_receiver": False}, ctx)
    assert i["type"] == {"relpath": "lib/lib.go", "qualname": "Child"} and i["iface"]["qualname"] == "Greeter"
    assert resolution._implements_record({"type": greeter, "iface": greeter}, ctx) is None  # concrete only
    e = resolution._embed_record({"type": child, "embedded": universe, "pointer": False}, ctx)
    assert (e["status"], e["external"]["module"], e["embedded"]) == ("external", "std", None)
    e = resolution._embed_record({"type": child, "embedded": shared, "pointer": True}, ctx)
    assert (e["status"], e["embedded"]["repo"], e["external"]["peer"], e["pointer"]) == ("external", "shared", True, True)
    e = resolution._embed_record({"type": child, "embedded": cached, "pointer": False}, ctx)
    assert e["external"]["version"] == "v1.2.0" and e["reason"] is None
    assert resolution._embed_record({"type": {**child, "line": 99}, "embedded": universe}, ctx) is None
    e = resolution._embed_record({"type": child, "embedded": {**child, "line": 99}}, ctx)
    assert (e["status"], e["embedded"]) == ("unresolved", None)
    i = resolution._implements_record({"type": child, "iface": shared, "pointer_receiver": True}, ctx)
    assert (i["status"], i["iface"]["repo"], i["pointer_receiver"]) == ("external", "shared", True)
    assert resolution._implements_record({"type": child, "iface": {"path": "/elsewhere/x.go"}}, ctx) is None
    assert resolution._implements_record({"type": None, "iface": shared}, ctx) is None


def test_an_incomplete_implements_pair_is_unresolved(world):
    """R43 (and G3/G4): the helper marks a pair whose method facts are not
    sound; Phase 1.6 keeps it as unresolved (incomplete-type), so neither
    Phase 9 nor crossref edges it."""
    from graph_generator.go_model import Peer
    peer = Peer(name="shared", root="/peers/shared", units=(unit("example.com/shared", ""),))
    ctx = _ctx(world, peers=[peer])
    child = {"path": os.path.join(world.repo, "lib", "lib.go"), "line": 17, "col": 6, "name": "Child",
             "pkg": "example.com/lib"}
    greeter = {"path": os.path.join(world.repo, "lib", "lib.go"), "line": 5, "col": 6, "name": "Greeter",
               "pkg": "example.com/lib"}
    shared = {"path": "/peers/shared/base.go", "line": 3, "col": 6, "name": "Base", "pkg": "example.com/shared",
              "obj": "type"}
    for iface, iface_end in ((greeter, ("lib/lib.go", "Greeter")), (shared, ("base.go", "Base"))):
        i = resolution._implements_record({"type": child, "iface": iface, "pointer_receiver": False,
                                           "incomplete": True}, ctx)
        assert (i["status"], i["reason"], i["external"]) == ("unresolved", "incomplete-type", None)
        assert (i["iface"]["relpath"], i["iface"]["qualname"]) == iface_end
        sound = resolution._implements_record({"type": child, "iface": iface, "incomplete": False}, ctx)
        assert sound["status"] in ("resolved", "external") and sound["reason"] is None


def test_a_pair_one_load_marks_incomplete_stays_incomplete(world):
    """The same pair from two group loads: the most conservative answer wins."""
    records = json.loads(json.dumps(world.resolves[("ws0", "linux-amd64")]))
    pairs = [r for r in records if r["kind"] == "implements"]
    assert pairs and not any(r["incomplete"] for r in pairs)
    world.override[("ws0", "linux-amd64")] = HelperRun(
        records=records + [{**json.loads(json.dumps(r)), "incomplete": True} for r in pairs])
    art = world.run()
    got = art["files"]["lib/lib.go"]["implements"]
    assert len(got) == len(pairs)
    assert all((i["status"], i["reason"]) == ("unresolved", "incomplete-type") for i in got)
    from graph_generator.graph_rows import derive_edge_rows, node_index

    class Data:
        pass
    data = Data()
    data.repo, data.target_dir, data.file_hashes = "basic", world.repo, world.file_hashes
    data.extracted_entities, data.resolutions = world.entities, art
    rows = derive_edge_rows(data, node_index(data))
    assert rows["ClassImplements"] == []


def test_write_json_atomic_leaves_no_temp_file_on_failure(tmp_path, monkeypatch):
    path = str(tmp_path / "r.json")
    resolution.write_json_atomic({"a": 1}, path)

    def full_disk(*args, **kwargs):     # the temp file opens, but writing to it fails
        f = open(*args, **kwargs)
        f.write = lambda _text: (_ for _ in ()).throw(OSError("full"))
        return f

    monkeypatch.setattr(resolution, "open", full_disk, raising=False)
    with pytest.raises(OSError, match="full"):
        resolution.write_json_atomic({"a": 2}, path)
    assert os.listdir(tmp_path) == ["r.json"]
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"a": 1}        # the old file is intact


def test_the_artifact_feeds_the_graph_builders(world):
    """Contract check with B3: the edges of the basic repo come out of
    pipeline.node index + derive_edge_rows built from this artifact."""
    from graph_generator import pipeline
    art = world.run()
    data = pipeline.PipelineData(target_dir=world.repo)
    data.repo = "basic"
    data.extracted_entities = world.entities
    data.file_hashes = dict(world.file_hashes)
    data.resolutions = art
    data.discovery = world.disc
    rows = pipeline.derive_edge_rows(data, pipeline._node_index(data))
    assert len(rows["PackageImports"]) == 1                     # app/main.go -> example.com/lib
    assert len(rows["ClassEmbeds"]) == 1                        # Child embeds Base
    assert len(rows["ClassImplements"]) >= 1                    # English implements Greeter
    resolved = sum(c["status"] == "resolved" for f in art["files"].values() for c in f["calls"])
    assert 0 < len(rows["MethodCalls"]) <= resolved
    assert rows["PossiblyCalls"]                                # Greet candidates, the anonymous Hello
    stats = rows.stats
    for key in ("imports_unmapped", "embeds_unmapped", "implements_unmapped", "files_stale"):
        assert not stats.get(key), (key, stats.get(key))


@pytest.mark.go
def test_f1_with_the_real_helper_has_no_wrong_answer(tmp_path, monkeypatch):
    """Phases 1-1.6 on the F1 corpus offline: every site joins, and no record
    contradicts the manifest (an in-repo target other than the expected one,
    or an edge where none is expected). Scoring itself is evaluate's (B5)."""
    from go_worlds import GO, have_go
    from graph_generator import config, go_toolchain, pipeline
    fixture = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_codes",
                           "iryo-kanri-system")
    manifest = os.path.join(fixture, "ground_truth", "manifest.json")
    if not have_go() or not os.path.isfile(manifest):
        pytest.skip("needs a Go toolchain and the F1 corpus")
    out = str(tmp_path / "out")
    monkeypatch.setattr(config, "OUTPUT_DIR", os.path.join(out, "repos", "iryo"))
    monkeypatch.setattr(config, "GO_TOOLCHAIN", GO)
    monkeypatch.setattr(config, "GO_USER_MODCACHE", str(tmp_path / "empty-cache"))
    data = pipeline.PipelineData(target_dir=fixture)
    data.repo = "iryo"
    data.toolchain = go_toolchain.probe_toolchain(GO)
    pipeline.phase1_scan(data)
    pipeline.phase1b_extract(data)
    pipeline.phase1c_resolve(data)
    art = data.resolutions
    assert art["stats"]["join_misses"] == 0 and art["stats"]["stale"] == 0
    assert all(g["status"] == "ok" for g in art["groups"].values())
    by_pos = {(rel, c["site"]["line"], c["site"]["col"]): c for rel, f in art["files"].items() for c in f["calls"]}
    with open(manifest) as f:
        cases = json.load(f)["cases"]
    wrong = []
    for case in cases:
        got = by_pos.get((case["file"], case["line"], case["col"]))
        want = case["expect"]
        if got is None or got["status"] != "resolved":
            continue
        want_targets = [(t["file"], t["qualname"]) for t in want["targets"]]
        if want["status"] != "resolved" or [(got["target"]["relpath"], got["target"]["qualname"])] != want_targets:
            wrong.append((case["id"], want["status"], want_targets, got["target"]))
    assert wrong == []
