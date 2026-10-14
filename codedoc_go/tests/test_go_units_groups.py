"""discover(): units, groups, the toolchain gate, vendor, peers and units.json (spec §3, §8.2)."""
from __future__ import annotations

import json
import os

import pytest

from graph_generator import go_modcache, go_units
from graph_generator.go_model import BuildConfig, Replace, Toolchain
from graph_generator.go_units import discover, discover_peer, prepare_control_dirs

import go_worlds

TC = Toolchain(go="/nonexistent/go", version="go1.27.2", goroot="/nonexistent")
BUILDS = [BuildConfig("linux-amd64", "linux", "amd64", (), False, True)]


@pytest.fixture(autouse=True)
def _no_user_cache(monkeypatch):
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: None)


def _discover(root, tmp_path, *, peers=(), mode="offline", toolchain=TC, repo="r"):
    return discover(str(root), repo, builds=BUILDS, peers=list(peers), mode=mode,
                    output_dir=str(tmp_path / "out"), toolchain=toolchain)


def _group(disc, gid):
    return next(g for g in disc.groups if g.id == gid)


WORKSPACE = {
    "go.work": "go 1.26\n\nuse (\n\t.\n\t./api\n\t./missing\n\t../outside\n)\n",
    "go.mod": "module example.com/r\n\ngo 1.26\n\nrequire (\n\texample.com/r/api v0.0.0\n"
              "\texample.com/r/tool v1.0.0\n\texample.com/ext v1.2.0 // indirect\n)\n",
    "r.go": 'package r\n\nimport (\n\t_ "example.com/r/api"\n\t_ "example.com/r/tool"\n)\n',
    "loose/x.go": "package loose\n",
    "api/go.mod": "module example.com/r/api\n\ngo 1.25\n",
    "api/a.go": "package api\n",
    "tool/go.mod": "module example.com/r/tool\n\ngo 1.24\n",
    "tool/t.go": "package tool\n",
}


def _workspace(tmp_path):
    go_worlds.write_tree(tmp_path / "outside", {"go.mod": "module example.com/outside\n\ngo 1.26\n"})
    return go_worlds.write_tree(tmp_path / "repo", WORKSPACE)


def test_workspace_and_module_groups(tmp_path):
    root = os.path.realpath(_workspace(tmp_path))
    disc = _discover(root, tmp_path)
    assert [u.key for u in disc.units] == ["example.com/r", "example.com/r/api", "example.com/r/tool"]
    ws, mod = disc.groups
    out = os.path.realpath(tmp_path / "out")
    assert (ws.id, ws.kind, ws.pattern, ws.status, ws.vendor) == ("ws0", "workspace", "work", "ok", False)
    assert ws.modules == ["example.com/r", "example.com/r/api"]
    assert ws.gowork == f"{out}/repos/r/go/ws0/go.work" and ws.modfile is None
    assert ws.dir == f"{out}/repos/r/go/ws0"
    assert ws.uses == [root, f"{root}/api", os.path.realpath(tmp_path / "outside")]
    assert ws.source == f"{root}/go.work"
    assert ws.degraded is True
    assert any("./missing" in w and "dropped" in w for w in ws.warnings)
    assert (mod.id, mod.kind, mod.pattern, mod.modules) == ("m0", "module", "./...", ["example.com/r/tool"])
    assert mod.gowork is None and mod.modfile == f"{out}/repos/r/go/m0/go.mod"
    assert mod.dir == f"{root}/tool" and mod.source == f"{root}/tool/go.mod"
    assert disc.unit_of_file == {"r.go": "example.com/r", "loose/x.go": "example.com/r",
                                 "api/a.go": "example.com/r/api", "tool/t.go": "example.com/r/tool"}
    assert disc.group_of("example.com/r/api") is ws
    assert any(w.startswith("group ws0: ") for w in disc.warnings)


def test_units_carry_go_mod_facts_and_in_repo_deps(tmp_path):
    disc = _discover(_workspace(tmp_path), tmp_path)
    r = disc.unit("example.com/r")
    assert (r.root, r.go, r.toolchain, r.vendor) == ("", "1.26", None, False)
    assert r.requires == (("example.com/r/api", "v0.0.0", False), ("example.com/r/tool", "v1.0.0", False),
                          ("example.com/ext", "v1.2.0", True))
    # r.go imports api and tool; tool is not in the workspace and not replaced, so the
    # published version builds (spec §3.2) and only api is an in-repo dependency.
    assert r.in_repo_deps == ("example.com/r/api",)
    assert disc.unit("example.com/r/tool").in_repo_deps == ()


def test_module_group_reaches_in_repo_code_only_through_a_local_replace(tmp_path):
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", {
        "a/go.mod": "module example.com/a\n\ngo 1.26\n\nrequire (\n\texample.com/b v0.0.0\n"
                    "\texample.com/c v1.0.0\n)\n\nreplace example.com/b => ../b\n",
        "a/a.go": 'package a\n\nimport (\n\t_ "example.com/b"\n\t_ "example.com/c"\n)\n',
        "b/go.mod": "module example.com/b\n\ngo 1.26\n",
        "c/go.mod": "module example.com/c\n\ngo 1.26\n"}))
    disc = _discover(root, tmp_path)
    assert [g.id for g in disc.groups] == ["m0", "m1", "m2"]
    a = disc.unit("example.com/a")
    assert a.in_repo_deps == ("example.com/b",)
    assert a.replaces == (Replace("example.com/b", "", f"{root}/b", "", True),)


def test_workspace_group_carries_its_go_work_replaces(tmp_path):
    """Group.replaces: the repo go.work's replaces, local targets resolved
    from the go.work's directory (R54 reads them); units.json lists them."""
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse ./a\n\nreplace (\n\texample.com/b => ./b\n"
                   "\texample.com/c v1.0.0 => example.com/c-fork v1.1.0\n)\n",
        "a/go.mod": "module example.com/a\n\ngo 1.26\n",
        "b/go.mod": "module example.com/b\n\ngo 1.26\n"}))
    disc = _discover(root, tmp_path)
    ws, mod = _group(disc, "ws0"), _group(disc, "m0")
    assert ws.replaces == [Replace("example.com/b", "", f"{root}/b", "", True),
                           Replace("example.com/c", "v1.0.0", "example.com/c-fork", "v1.1.0", False)]
    assert mod.modules == ["example.com/b"] and mod.replaces == []
    path = tmp_path / "units.json"
    go_units.write_units_json(disc, str(path))
    groups = {g["id"]: g for g in json.loads(path.read_text())["groups"]}
    assert groups["ws0"]["replaces"][0] == {"old": "example.com/b", "old_version": "", "new": f"{root}/b",
                                             "new_version": "", "local": True}
    assert groups["m0"]["replaces"] == []


def test_toolchain_too_new_fails_closed(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "new/go.mod": "module example.com/new\n\ngo 1.28\n",
        "new/n.go": "package n\n",
        "ok/go.mod": "module example.com/ok\n\ngo 1.27.2\n",
        "rc/go.mod": "module example.com/rc\n\ngo 1.27rc1\n"})
    disc = _discover(root, tmp_path)
    by_mod = {g.modules[0]: g for g in disc.groups}
    new = by_mod["example.com/new"]
    assert new.status == "toolchain"
    msg = " ".join(new.warnings)
    assert 'new/go.mod:3: "go 1.28"' in msg and "go1.27.2" in msg
    assert "GO_TOOLCHAIN" in msg and "setup go" in msg and "unresolved (toolchain)" in msg
    assert by_mod["example.com/ok"].status == "ok"
    assert by_mod["example.com/rc"].status == "ok"


def test_toolchain_gate_in_workspaces_and_with_old_toolchains(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.27\n\nuse ./a\n",
        "a/go.mod": "module example.com/a\n\ngo 1.27\n"})
    old = Toolchain(go="/x/go", version="go1.26.5", goroot="/x")
    ws = _discover(root, tmp_path, toolchain=old).groups[0]
    assert ws.status == "toolchain"
    msg = " ".join(ws.warnings)
    assert 'go.work:1: "go 1.27"' in msg and 'a/go.mod:3: "go 1.27"' in msg
    assert "python -m graph_generator setup go" in msg      # go1.27.2 would satisfy 1.27


def test_newer_toolchain_line_only_warns(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\ntoolchain go1.28.1\n"})
    g = _discover(root, tmp_path).groups[0]
    assert g.status == "ok"
    assert any('go.mod:5: "toolchain go1.28.1"' in w and "standard-library targets may differ" in w
               for w in g.warnings)


def test_no_toolchain_skips_the_gate(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {"go.mod": "module example.com/m\n\ngo 1.99\n"})
    disc = _discover(root, tmp_path, toolchain=None)
    assert disc.toolchain == "" and disc.groups[0].status == "ok"


VENDOR_GOMOD = "module example.com/v\n\ngo 1.22\n\nrequire example.com/dep v1.1.0\n"
VENDOR_TXT = "# example.com/dep v1.1.0\n## explicit; go 1.21\nexample.com/dep\n"


def test_vendored_module_group(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "v/go.mod": VENDOR_GOMOD, "v/vendor/modules.txt": VENDOR_TXT,
        "v/vendor/example.com/dep/dep.go": "package dep\n",
        "bad/go.mod": VENDOR_GOMOD.replace("example.com/v", "example.com/bad"),
        "bad/vendor/modules.txt": VENDOR_TXT.replace("v1.1.0", "v1.0.0"),
        "old/go.mod": VENDOR_GOMOD.replace("example.com/v", "example.com/old").replace("1.22", "1.13"),
        "old/vendor/modules.txt": VENDOR_TXT})
    disc = _discover(root, tmp_path)
    groups = {g.modules[0]: g for g in disc.groups}
    assert disc.unit("example.com/v").vendor and groups["example.com/v"].vendor
    # R35: an inconsistent vendor/ is a group error with the go command's message,
    # never a silent readonly build.
    bad = groups["example.com/bad"]
    assert not disc.unit("example.com/bad").vendor and not bad.vendor and bad.status == "error"
    assert any("go: inconsistent vendoring in" in w and "example.com/dep@v1.1.0" in w
               and "go mod vendor" in w for w in bad.warnings)
    assert not disc.unit("example.com/old").vendor            # go < 1.14: no default vendor mode
    assert groups["example.com/old"].status == "ok"
    assert disc.module_source == "none"


def test_vendored_dependency_go_line_is_gated(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": VENDOR_GOMOD, "vendor/modules.txt": VENDOR_TXT.replace("go 1.21", "go 1.29")})
    g = _discover(root, tmp_path).groups[0]
    assert g.vendor and g.status == "toolchain"
    assert any('vendor/modules.txt:2: "go 1.29"' in w for w in g.warnings)


def test_workspace_vendor(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.22\n\nuse (\n\t./a\n\t./b\n)\n",
        "a/go.mod": "module example.com/a\n\ngo 1.22\n\nrequire example.com/dep v1.0.0\n",
        "b/go.mod": "module example.com/b\n\ngo 1.22\n\nrequire example.com/dep v1.2.0\n",
        "vendor/modules.txt": "## workspace\n# example.com/dep v1.0.0\n## explicit; go 1.21\n"
                              "# example.com/dep v1.2.0\n## explicit; go 1.21\nexample.com/dep\n",
        "vendor/example.com/dep/dep.go": "package dep\n"})
    disc = _discover(root, tmp_path)
    assert disc.groups[0].vendor is True
    assert disc.module_source == "vendor"


def test_invalid_go_mod_means_no_module(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t./good\n\t./broken\n)\n",
        "good/go.mod": "module example.com/good\n\ngo 1.26\n",
        "good/g.go": "package good\n",
        "broken/go.mod": "module example.com/broken\ngo 1.26\ngo 1.27\n",
        "broken/b.go": "package broken\n"})
    disc = _discover(root, tmp_path)
    assert [u.key for u in disc.units] == ["example.com/good"]
    assert "broken/b.go" not in disc.unit_of_file
    assert any("broken/go.mod:3: repeated go statement" in w for w in disc.warnings)
    ws = disc.groups[0]
    assert ws.degraded and ws.uses == [os.path.realpath(root) + "/good"]


def test_duplicate_module_paths_keep_the_workspace_one(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse ./z\n",
        "a/go.mod": "module example.com/same\n\ngo 1.26\n",
        "a/a.go": "package same\n",
        "z/go.mod": "module example.com/same\n\ngo 1.26\n",
        "z/z.go": "package same\n"})
    disc = _discover(root, tmp_path)
    assert [(u.key, u.root) for u in disc.units] == [("example.com/same", "z")]
    assert disc.unit_of_file == {"z/z.go": "example.com/same"}
    assert any("a/go.mod" in w and "example.com/same" in w for w in disc.warnings)


def test_unparseable_go_work_falls_back_to_module_groups(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\nuse ./a\nfrob x\n",
        "a/go.mod": "module example.com/a\n\ngo 1.26\n"})
    disc = _discover(root, tmp_path)
    assert [(g.id, g.kind) for g in disc.groups] == [("m0", "module")]
    assert any("go.work:3: unknown directive: frob" in w for w in disc.warnings)


def test_workspace_go_line_is_raised_to_the_modules(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.21\n\nuse ./a\n",
        "a/go.mod": "module example.com/a\n\ngo 1.22\n"})
    disc = _discover(root, tmp_path)
    ws = disc.groups[0]
    assert ws.degraded and ws.status == "ok"
    assert any('go.work:1: "go 1.21"' in w and "go 1.22" in w for w in ws.warnings)
    prepare_control_dirs(disc, str(tmp_path / "out"))
    assert "\ngo 1.22\n" in open(ws.gowork).read()


def test_module_listed_by_two_workspaces_belongs_to_the_nearest(tmp_path):
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t.\n\t./sub/m\n)\n",
        "go.mod": "module example.com/r\n\ngo 1.26\n",
        "sub/go.work": "go 1.26\n\nuse ./m\n",
        "sub/m/go.mod": "module example.com/m\n\ngo 1.26\n"}))
    disc = _discover(root, tmp_path)
    ws0, ws1 = disc.groups
    assert ws0.modules == ["example.com/r"] and ws0.uses == [root, f"{root}/sub/m"]
    assert ws1.modules == ["example.com/m"] and ws1.source == f"{root}/sub/go.work"


def test_bad_repo_names_are_refused(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {"go.mod": "module example.com/m\n"})
    for bad in ("", "..", "a/b"):
        with pytest.raises(ValueError):
            _discover(root, tmp_path, repo=bad)


def test_bad_mode_is_refused(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {"go.mod": "module example.com/m\n"})
    with pytest.raises(ValueError):
        _discover(root, tmp_path, mode="online")


# ---------------------------------------------------------------- module sources

def test_missing_modules_offline(tmp_path, monkeypatch):
    user = tmp_path / "usercache"
    go_worlds.make_proxy(user / "cache" / "download", {
        "example.com/have": {"v1.0.0": {"go.mod": "module example.com/have\n", "h.go": "package have\n"}},
        "example.com/Upper": {"v1.0.0": {"go.mod": "module example.com/Upper\n", "u.go": "package u\n"}}})
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: str(user))
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire (\n\texample.com/have v1.0.0\n"
                  "\texample.com/Upper v1.0.0\n\texample.com/gone v1.0.0\n\texample.com/old v1.0.0\n"
                  "\texample.com/loc v0.0.0\n)\n\nreplace example.com/old => example.com/new v2.0.0\n"
                  "replace example.com/loc => ./loc\n",
        "loc/go.mod": "module example.com/loc\n"})
    disc = _discover(root, tmp_path)
    assert disc.module_source == "file-proxy"
    g = disc.group_of("example.com/m")
    assert g.missing_modules == ["example.com/gone@v1.0.0", "example.com/new@v2.0.0"]
    assert any("2 required modules are missing offline" in w and "--download-modules" in w
               for w in g.warnings)
    down = _discover(root, tmp_path, mode="download")
    assert down.module_source == "proxy" and down.group_of("example.com/m").missing_modules == []


def test_zips_are_needed_only_for_imported_modules(tmp_path, monkeypatch):
    user = tmp_path / "usercache"
    go_worlds.make_proxy(user / "cache" / "download", {
        m: {"v1.0.0": {"go.mod": f"module {m}\n"}} for m in ("example.com/used", "example.com/tool")})
    for m in ("used", "tool"):
        os.remove(user / "cache" / "download" / "example.com" / m / "@v" / "v1.0.0.zip")
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: str(user))
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire (\n\texample.com/used v1.0.0\n"
                  "\texample.com/tool v1.0.0 // indirect\n)\n",
        "m.go": 'package m\n\nimport (\n\t"fmt"\n\tu "example.com/used/sub"\n)\n\nvar _ = fmt.Sprint(u.X)\n'})
    # Only the imported module's zip is required; the other only needs its go.mod.
    assert _discover(root, tmp_path).groups[0].missing_modules == ["example.com/used@v1.0.0"]
    # Imports of files no build configuration compiles do not count.
    go_worlds.write_tree(root, {"m.go": "//go:build tools\n\n" + open(os.path.join(root, "m.go")).read()})
    assert _discover(root, tmp_path).groups[0].missing_modules == []


def test_workspace_needs_the_zip_of_the_selected_version_only(tmp_path, monkeypatch):
    user = tmp_path / "usercache"
    go_worlds.make_proxy(user / "cache" / "download", {"example.com/dep": {
        v: {"go.mod": "module example.com/dep\n", "d.go": "package dep\n"} for v in ("v1.1.0", "v1.3.0")}})
    os.remove(user / "cache" / "download" / "example.com" / "dep" / "@v" / "v1.1.0.zip")
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: str(user))
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t./a\n\t./b\n)\n",
        "a/go.mod": "module example.com/a\n\ngo 1.26\n\nrequire example.com/dep v1.3.0\n",
        "a/a.go": 'package a\n\nimport _ "example.com/dep"\n',
        "b/go.mod": "module example.com/b\n\ngo 1.26\n\nrequire example.com/dep v1.1.0 // indirect\n"})
    assert _discover(root, tmp_path).groups[0].missing_modules == []


def test_file_imports(tmp_path):
    go_worlds.write_tree(tmp_path, {"x.go": '// c\npackage x\n\nimport "a.b/c"\nimport (\n\t_ "d.e/f"\n'
                                            '\tg "h.i/j" // k\n\t. `l.m/n`\n)\n\nfunc F() {}\n'})
    assert go_units.file_imports(str(tmp_path / "x.go")) == ["a.b/c", "d.e/f", "h.i/j", "l.m/n"]
    assert go_units.file_imports(str(tmp_path / "missing.go")) == []


def test_workspace_requirement_on_a_workspace_module_needs_its_go_mod(tmp_path, monkeypatch):
    """Probed with go1.27.2 on 2026-10-14: in workspace mode `require b v0.0.0` without a
    replace still fetches b@v0.0.0's go.mod for the module graph (offline it fails)."""
    user = tmp_path / "usercache"
    go_worlds.make_proxy(user / "cache" / "download", {
        "example.com/c": {"v1.0.0": {"go.mod": "module example.com/c\n"}}})
    os.remove(user / "cache" / "download" / "example.com" / "c" / "@v" / "v1.0.0.zip")
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: str(user))
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t./a\n\t./b\n\t./c\n\t./d\n)\n",
        "a/go.mod": "module example.com/a\n\ngo 1.26\n\nrequire (\n\texample.com/b v0.0.0\n"
                    "\texample.com/c v1.0.0\n\texample.com/d v0.0.0\n)\n\nreplace example.com/d => ../d\n",
        "a/a.go": 'package a\n\nimport (\n\t_ "example.com/b"\n\t_ "example.com/c"\n\t_ "example.com/d"\n)\n',
        "b/go.mod": "module example.com/b\n\ngo 1.26\n",
        "c/go.mod": "module example.com/c\n\ngo 1.26\n",
        "d/go.mod": "module example.com/d\n\ngo 1.26\n"})
    g = _discover(root, tmp_path).groups[0]
    # b: no replace, no go.mod cached -> missing; c: go.mod cached is enough; d: replaced.
    assert g.missing_modules == ["example.com/b@v0.0.0"]
    assert _discover(root, tmp_path).unit("example.com/a").in_repo_deps == (
        "example.com/b", "example.com/c", "example.com/d")


def test_isolated_cache_counts_as_available(tmp_path):
    out = tmp_path / "out"
    os.makedirs(out / "go" / "modcache" / "cache" / "download" / "example.com" / "x" / "@v")
    open(out / "go" / "modcache" / "cache" / "download" / "example.com" / "x" / "@v" / "v1.0.0.mod", "w").close()
    os.makedirs(out / "go" / "modcache" / "example.com" / "x@v1.0.0")
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire example.com/x v1.0.0\n"})
    assert _discover(root, tmp_path).groups[0].missing_modules == []


# ---------------------------------------------------------------- peers (spec §8.2)

def _peer(tmp_path, name, mods: dict[str, str]):
    files = {f"{d}/go.mod": f"module {m}\n\ngo 1.26\n" for d, m in mods.items()}
    root = go_worlds.write_tree(tmp_path / name, files)
    return discover_peer(name, root)


def test_discover_peer(tmp_path):
    p = _peer(tmp_path, "shared", {"core": "example.com/shared", "extra": "example.com/shared/extra"})
    assert p.name == "shared" and p.root == os.path.realpath(tmp_path / "shared")
    assert [(u.key, u.root, u.go) for u in p.units] == [
        ("example.com/shared", "core", "1.26"), ("example.com/shared/extra", "extra", "1.26")]


def test_peer_substitution_only_for_required_paths(tmp_path):
    peer = _peer(tmp_path, "shared", {"core": "example.com/shared", "other": "example.com/other"})
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse ./web\n",
        "web/go.mod": "module example.com/web\n\ngo 1.26\n\nrequire example.com/shared v1.0.0\n",
        "api/go.mod": "module example.com/api\n\ngo 1.26\n\nrequire example.com/shared v1.0.0 // indirect\n"})
    disc = _discover(root, tmp_path, peers=[peer])
    assert go_units.peer_replaces(disc, disc.groups[0]) == [
        ("example.com/shared", os.path.realpath(tmp_path / "shared" / "core"))]
    assert go_units.peer_replaces(disc, disc.group_of("example.com/api")) == [
        ("example.com/shared", os.path.realpath(tmp_path / "shared" / "core"))]
    prepare_control_dirs(disc, str(tmp_path / "out"))
    work = open(disc.groups[0].gowork).read()
    assert f"replace example.com/shared => {os.path.realpath(tmp_path / 'shared' / 'core')}\n" in work
    assert "example.com/other" not in work
    modfile = open(disc.group_of("example.com/api").modfile).read()
    assert modfile.startswith(open(os.path.join(root, "api", "go.mod")).read())
    assert f"replace example.com/shared => {os.path.realpath(tmp_path / 'shared' / 'core')}" in modfile


def test_peer_not_substituted_for_vendored_group(tmp_path):
    peer = _peer(tmp_path, "depp", {".": "example.com/dep"})
    root = go_worlds.write_tree(tmp_path / "repo", {"go.mod": VENDOR_GOMOD,
                                                     "vendor/modules.txt": VENDOR_TXT})
    disc = _discover(root, tmp_path, peers=[peer])
    g = disc.groups[0]
    assert g.vendor and go_units.peer_replaces(disc, g) == []
    assert any("vendored" in w and "depp" in w for w in g.warnings)
    prepare_control_dirs(disc, str(tmp_path / "out"))
    assert open(g.modfile, "rb").read() == VENDOR_GOMOD.encode()


def test_peer_refused_for_duplicate_module_paths(tmp_path):
    p1 = _peer(tmp_path, "one", {".": "example.com/shared"})
    p2 = _peer(tmp_path, "two", {"fork": "example.com/shared"})
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire example.com/shared v1.0.0\n"})
    disc = _discover(root, tmp_path, peers=[p1, p2])
    g = disc.groups[0]
    assert go_units.peer_replaces(disc, g) == []
    assert any("example.com/shared" in w and "one" in w and "two" in w for w in g.warnings)


def test_peer_not_substituted_where_the_repo_decides(tmp_path):
    peer = _peer(tmp_path, "shared", {".": "example.com/shared", "own": "example.com/m2",
                                       "n": "example.com/new"})
    os.makedirs(tmp_path / "shared" / "n", exist_ok=True)
    with open(tmp_path / "shared" / "n" / "go.mod", "w") as f:
        f.write("module example.com/new\n\ngo 1.29\n")
    peer = discover_peer("shared", str(tmp_path / "shared"))
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire (\n\texample.com/shared v1.0.0\n"
                  "\texample.com/m2 v1.0.0\n\texample.com/new v1.0.0\n)\n\n"
                  "replace example.com/shared v1.0.0 => example.com/fork v1.0.1\n",
        "m2/go.mod": "module example.com/m2\n\ngo 1.26\n"})
    disc = _discover(root, tmp_path, peers=[peer])
    g = disc.group_of("example.com/m")
    assert go_units.peer_replaces(disc, g) == []
    text = " ".join(g.warnings)
    assert "example.com/shared" in text and "already replaces" in text
    assert "example.com/m2" in text and "this repo" in text
    assert "example.com/new" in text and "go 1.29" in text


# ---------------------------------------------------------------- control directories (spec §3.5)

def test_prepare_control_dirs_workspace(tmp_path):
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\ntoolchain go1.26.8\n\ngodebug (\n\tpanicnil=1\n)\n\nuse (\n\t.\n\t./api\n)\n\n"
                   "replace example.com/x v1.0.0 => ./fork\nreplace example.com/y => example.com/z v1.2.3\n",
        "go.work.sum": "example.com/q v1.0.0 h1:abc=\n",
        "go.mod": "module example.com/r\n\ngo 1.26\n",
        "api/go.mod": "module example.com/r/api\n\ngo 1.26\n",
        "fork/go.mod": "module example.com/x\n"}))
    before = go_worlds.snapshot(root)
    disc = _discover(root, tmp_path)
    prepare_control_dirs(disc, str(tmp_path / "out"))
    ws = disc.groups[0]
    assert open(ws.gowork).read() == (
        f"// Generated by codedoc_go from {root}/go.work; do not edit.\n"
        "go 1.26\n"
        "toolchain go1.26.8\n"
        "godebug panicnil=1\n"
        "use (\n"
        f"\t{root}\n"
        f"\t{root}/api\n"
        ")\n"
        f"replace example.com/x v1.0.0 => {root}/fork\n"
        "replace example.com/y => example.com/z v1.2.3\n")
    assert open(os.path.join(ws.dir, "go.work.sum")).read() == "example.com/q v1.0.0 h1:abc=\n"
    assert not os.path.lexists(os.path.join(ws.dir, "vendor"))
    assert go_worlds.snapshot(root) == before


def test_prepare_control_dirs_module_copy_and_stale_dirs(tmp_path):
    gomod = "module example.com/m\n\ngo 1.26\n\nrequire example.com/dep v1.0.0\n\nreplace example.com/x => ../x\n"
    root = go_worlds.write_tree(tmp_path / "repo", {"m/go.mod": gomod, "m/go.sum": "example.com/dep v1.0.0 h1:x=\n"})
    out = tmp_path / "out"
    stale = out / "repos" / "r" / "go" / "ws9"
    os.makedirs(stale)
    disc = _discover(root, tmp_path)
    prepare_control_dirs(disc, str(out))
    g = disc.groups[0]
    assert open(g.modfile, "rb").read() == gomod.encode()
    assert open(os.path.join(os.path.dirname(g.modfile), "go.sum")).read() == "example.com/dep v1.0.0 h1:x=\n"
    assert not stale.exists()


def test_workspace_vendor_gets_a_symlink(tmp_path):
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.22\n\nuse ./a\n",
        "a/go.mod": "module example.com/a\n\ngo 1.22\n\nrequire example.com/dep v1.0.0\n",
        "vendor/modules.txt": "## workspace\n# example.com/dep v1.0.0\n## explicit; go 1.21\nexample.com/dep\n",
        "vendor/example.com/dep/dep.go": "package dep\n"}))
    disc = _discover(root, tmp_path)
    prepare_control_dirs(disc, str(tmp_path / "out"))
    link = os.path.join(disc.groups[0].dir, "vendor")
    assert os.path.islink(link) and os.readlink(link) == f"{root}/vendor"


def test_workspace_vendor_with_directory_replaces_gets_a_rewritten_view(tmp_path):
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.22\n\nuse ./a\n\nreplace example.com/w => ./wdir\n",
        "a/go.mod": "module example.com/a\n\ngo 1.22\n\nrequire (\n\texample.com/loc v0.0.0\n"
                    "\texample.com/w v0.0.0\n)\n\nreplace example.com/loc => ./local\n",
        "a/local/go.mod": "module example.com/loc\n\ngo 1.22\n",
        "wdir/go.mod": "module example.com/w\n\ngo 1.22\n",
        "vendor/modules.txt": "## workspace\n# example.com/loc v0.0.0 => ./a/local\n## explicit; go 1.22\n"
                              "example.com/loc\n# example.com/w v0.0.0 => ./wdir\n## explicit; go 1.22\n"
                              "example.com/w\n# example.com/loc => ./a/local\n# example.com/w => ./wdir\n",
        "vendor/example.com/loc/l.go": "package loc\n",
        "vendor/example.com/w/w.go": "package w\n"}))
    disc = _discover(root, tmp_path)
    ws = disc.groups[0]
    assert ws.vendor is True
    prepare_control_dirs(disc, str(tmp_path / "out"))
    view = os.path.join(ws.dir, "vendor")
    assert os.path.isdir(view) and not os.path.islink(view)
    assert os.readlink(os.path.join(view, "example.com")) == f"{root}/vendor/example.com"
    rel_local = os.path.relpath(f"{root}/a/local", ws.dir)
    assert open(os.path.join(view, "modules.txt")).read() == (
        f"## workspace\n# example.com/loc v0.0.0 => {rel_local}\n## explicit; go 1.22\n"
        f"example.com/loc\n# example.com/w v0.0.0 => {root}/wdir\n## explicit; go 1.22\n"
        f"example.com/w\n# example.com/loc => {rel_local}\n# example.com/w => {root}/wdir\n")


# ---------------------------------------------------------------- units.json

def test_write_units_json(tmp_path):
    disc = _discover(_workspace(tmp_path), tmp_path)
    path = tmp_path / "units.json"
    go_units.write_units_json(disc, str(path))
    doc = json.loads(path.read_text())
    assert doc["version"] == 1 and doc["repo"] == "r" and doc["toolchain"] == "go1.27.2"
    assert doc["builds"] == [{"id": "linux-amd64", "goos": "linux", "goarch": "amd64", "tags": [],
                              "cgo": False, "primary": True}]
    assert (doc["mode"], doc["module_source"]) == ("offline", "none")
    ws = doc["groups"][0]
    assert {"id", "kind", "dir", "gowork", "modfile", "modules", "vendor", "degraded", "status",
            "pattern", "missing_modules", "warnings", "source", "uses"} <= set(ws)
    unit = next(u for u in doc["units"] if u["key"] == "example.com/r")
    assert unit["group"] == "ws0" and unit["root"] == "" and unit["go"] == "1.26"
    assert unit["requires"][2] == {"path": "example.com/ext", "version": "v1.2.0", "indirect": True}
    assert unit["in_repo_deps"] == ["example.com/r/api"]
    assert doc["peers"] == [] and doc["warnings"] == disc.warnings and "skipped" in doc
    assert not list(tmp_path.glob("units.json.tmp*"))


# ---------------------------------------------------------------- build configurations (spec §3.4)

def _settings(monkeypatch, **values):
    from graph_generator import config
    for key in ("GO_BUILD_GOOS", "GO_BUILD_GOARCH", "GO_BUILD_TAGS", "GO_BUILD_CGO", "GO_EXTRA_BUILDS"):
        monkeypatch.delattr(config, key, raising=False)
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setattr(config, key, value, raising=False)


def test_builds_from_config(monkeypatch):
    _settings(monkeypatch)
    assert go_units.builds_from_config() == [BuildConfig("linux-amd64", "linux", "amd64", (), False, True)]
    _settings(monkeypatch, GO_BUILD_TAGS=" integration, e2e ,", GO_BUILD_CGO=True,
              GO_EXTRA_BUILDS="windows/amd64  darwin/arm64+ui+ios")
    assert go_units.builds_from_config() == [
        BuildConfig("linux-amd64+integration+e2e", "linux", "amd64", ("integration", "e2e"), True, True),
        BuildConfig("windows-amd64", "windows", "amd64", (), False, False),
        BuildConfig("darwin-arm64+ui+ios", "darwin", "arm64", ("ui", "ios"), False, False)]


@pytest.mark.parametrize("key,value,message", [
    ("GO_EXTRA_BUILDS", "windows", "is not goos/goarch"),
    ("GO_EXTRA_BUILDS", "windows/amd64+", "is not goos/goarch"),
    ("GO_EXTRA_BUILDS", "Windows/amd64", "not a GOOS/GOARCH pair"),
    ("GO_EXTRA_BUILDS", "linux/amd64", "listed twice"),
    ("GO_EXTRA_BUILDS", "linux/arm64+a-b", "build tag 'a-b'"),
    ("GO_BUILD_GOOS", "linux os", "not a GOOS/GOARCH pair"),
    ("GO_BUILD_TAGS", "ok,no way", "build tag 'no way'"),
])
def test_malformed_build_settings(monkeypatch, key, value, message):
    _settings(monkeypatch, **{key: value})
    with pytest.raises(ValueError, match=message):
        go_units.builds_from_config()


# ---------------------------------------------------------------- spec Appendix B amendments

def test_r4_a_non_ancestor_go_work_does_not_group_modules(tmp_path):
    """Probe p10: the go command never sees tools/go.work from api/ or server/."""
    root = go_worlds.write_tree(tmp_path / "repo", {
        "api/go.mod": "module example.com/api\n\ngo 1.26\n",
        "server/go.mod": "module example.com/server\n\ngo 1.26\n\nrequire example.com/api v1.0.0\n",
        "server/main.go": 'package main\n\nimport _ "example.com/api"\n\nfunc main() {}\n',
        "tools/go.work": "go 1.26\n\nuse (\n\t../api\n\t../server\n)\n"})
    disc = _discover(root, tmp_path)
    assert [(g.kind, g.modules) for g in disc.groups] == [
        ("module", ["example.com/api"]), ("module", ["example.com/server"])]
    assert disc.unit("example.com/server").in_repo_deps == ()    # the published api builds
    assert sum("tools/go.work lists" in w and "single module (GOWORK=off)" in w
               for w in disc.warnings) == 2


def test_r4_each_module_belongs_to_the_go_work_found_walking_up(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t./app\n\t./core\n)\n",
        "app/go.mod": "module example.com/app\n\ngo 1.26\n",
        "core/go.mod": "module example.com/core\n\ngo 1.26\n",
        "scratch/go.work": "go 1.26\n\nuse ../core\n",
        "other/go.mod": "module example.com/other\n\ngo 1.26\n"})
    disc = _discover(root, tmp_path)
    assert [(g.id, g.modules) for g in disc.groups] == [
        ("ws0", ["example.com/app", "example.com/core"]), ("m0", ["example.com/other"])]
    assert any("scratch/go.work lists core" in w and "workspace of go.work" in w for w in disc.warnings)
    assert not any("other" in w for w in disc.warnings)          # listed by no go.work: no warning


def test_r35_inconsistent_workspace_vendor_is_an_error(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.22\n\nuse ./a\n",
        "a/go.mod": "module example.com/a\n\ngo 1.22\n\nrequire example.com/dep v1.1.0\n",
        "vendor/modules.txt": "## workspace\n# example.com/dep v1.0.0\n## explicit; go 1.21\nexample.com/dep\n"})
    ws = _discover(root, tmp_path).groups[0]
    assert ws.status == "error" and not ws.vendor
    assert any("go: inconsistent vendoring in" in w and "go work vendor" in w for w in ws.warnings)


def test_r5_a_dependency_go_line_newer_than_the_toolchain_warns(tmp_path, monkeypatch):
    """Probe p7: a dependency with `go 1.28` fails the whole load; doctor predicts it
    from cached go.mod files (offline: the user cache; download: file:// proxies)."""
    newgo = {"example.com/newgo": {"v1.0.0": {"go.mod": "module example.com/newgo\n\ngo 1.28\n",
                                              "n.go": "package newgo\n"}}}
    user = tmp_path / "usercache"
    go_worlds.make_proxy(user / "cache" / "download", newgo)
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: str(user))
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire example.com/newgo v1.0.0\n"})
    g = _discover(root, tmp_path).groups[0]
    assert g.status == "ok"
    assert any('dependency example.com/newgo@v1.0.0 says "go 1.28"' in w and "GO_TOOLCHAIN" in w
               for w in g.warnings)
    proxy = tmp_path / "proxy"
    go_worlds.make_proxy(proxy, newgo)
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: None)
    from graph_generator import config
    monkeypatch.setattr(config, "GO_PROXY", proxy.as_uri(), raising=False)
    down = _discover(root, tmp_path, mode="download").groups[0]
    assert any('example.com/newgo@v1.0.0 says "go 1.28"' in w for w in down.warnings)


def test_r12_module_groups_keep_the_modfile_copy_in_both_modes(tmp_path):
    """R12 as refined (2026-10-14): download mode fills the -modfile copy in module
    mode (go_modcache.download), so module groups look the same in both modes."""
    peer = _peer(tmp_path, "shared", {".": "example.com/shared"})
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", {
        "m/go.mod": "module example.com/m\n\ngo 1.26\n\nrequire example.com/shared v1.0.0\n",
        "m/go.sum": "example.com/x v1.0.0 h1:x=\n"}))
    for mode in ("offline", "download"):
        disc = _discover(root, tmp_path, mode=mode, peers=[peer])
        g = disc.groups[0]
        assert (g.gowork, g.dir, g.pattern) == (None, f"{root}/m", "./...")
        assert g.modfile.endswith("/repos/r/go/m0/go.mod")
        prepare_control_dirs(disc, str(tmp_path / "out"))
        assert open(g.modfile).read().endswith(
            f"replace example.com/shared => {os.path.realpath(tmp_path / 'shared')}\n")
        assert open(os.path.join(os.path.dirname(g.modfile), "go.sum")).read() == "example.com/x v1.0.0 h1:x=\n"


def test_r15_stops_only_when_every_group_is_too_new(tmp_path):
    root = go_worlds.write_tree(tmp_path / "repo", {
        "a/go.mod": "module example.com/a\n\ngo 1.28\n", "b/go.mod": "module example.com/b\n\ngo 1.29\n"})
    disc = _discover(root, tmp_path)
    [stop] = go_units.stops(disc)
    assert "every group" in stop and "GO_TOOLCHAIN" in stop
    go_worlds.write_tree(root, {"c/go.mod": "module example.com/c\n\ngo 1.26\n"})
    assert go_units.stops(_discover(root, tmp_path)) == []


def test_r28_substituted_peer_requirements_count_offline(tmp_path, monkeypatch):
    user = tmp_path / "usercache"
    go_worlds.make_proxy(user / "cache" / "download", {"example.com/newer": {"v1.0.0": {
        "go.mod": "module example.com/newer\n\ngo 1.29\n", "n.go": "package newer\n"}}})
    monkeypatch.setattr(go_modcache, "user_modcache", lambda toolchain, output_dir=None: str(user))
    peer_root = go_worlds.write_tree(tmp_path / "shared", {
        "go.mod": "module example.com/shared\n\ngo 1.26\n\nrequire (\n\texample.com/deep v1.0.0\n"
                  "\texample.com/newer v1.0.0\n)\n"})
    peer = discover_peer("shared", peer_root)
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire example.com/shared v1.0.0\n"})
    g = _discover(root, tmp_path, peers=[peer]).groups[0]
    assert go_units.peer_replaces(_discover(root, tmp_path, peers=[peer]), g) == [
        ("example.com/shared", os.path.realpath(peer_root))]
    assert g.missing_modules == ["example.com/deep@v1.0.0"]
    text = " ".join(g.warnings)
    assert "substituted peer module example.com/shared requires 1 modules that are missing" in text
    assert 'dependency example.com/newer@v1.0.0 says "go 1.29"' in text


def test_peer_not_substituted_when_the_go_work_or_another_member_replaces_it(tmp_path):
    peer = _peer(tmp_path, "shared", {"x": "example.com/x", "y": "example.com/y"})
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t./a\n\t./b\n)\n\nreplace example.com/x v1.0.0 => example.com/x2 v1.0.0\n",
        "a/go.mod": "module example.com/a\n\ngo 1.26\n\nrequire (\n\texample.com/x v1.0.0\n"
                    "\texample.com/y v1.0.0\n)\n",
        "b/go.mod": "module example.com/b\n\ngo 1.26\n\nreplace example.com/y => ../ylocal\n"})
    disc = _discover(root, tmp_path, peers=[peer])
    ws = disc.groups[0]
    assert go_units.peer_replaces(disc, ws) == []
    assert sum("already replaces" in w for w in ws.warnings) == 2


def test_in_repo_deps_come_from_imports_that_build_in_repo_source(tmp_path):
    """The lead's definition (2026-10-14): imports of the unit's files under the primary
    build, tests included, of a package under an in-repo module (longest module-path
    prefix), when the group builds it from in-repo source."""
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse (\n\t./a\n\t./b\n\t./c\n)\n",
        "a/go.mod": "module example.com/a\n\ngo 1.26\n\nrequire (\n\texample.com/d v0.0.0\n"
                    "\texample.com/c v0.0.0\n)\n\nreplace example.com/d => ../d\n",
        "a/a.go": 'package a\n\nimport (\n\t_ "example.com/b/x"\n\t_ "example.com/b/sub/pkg"\n'
                  '\t_ "example.com/d"\n\t_ "example.com/e"\n\t_ "fmt"\n)\n',
        "a/a_test.go": 'package a\n\nimport _ "example.com/c"\n',
        "a/only_windows.go": '//go:build windows\n\npackage a\n\nimport _ "example.com/f"\n',
        "b/go.mod": "module example.com/b\n\ngo 1.26\n",
        "b/b.go": 'package b\n',
        "b/sub/go.mod": "module example.com/b/sub\n\ngo 1.26\n",
        "c/go.mod": "module example.com/c\n\ngo 1.26\n",
        "d/go.mod": "module example.com/d\n\ngo 1.26\n",
        "e/go.mod": "module example.com/e\n\ngo 1.26\n",
        "f/go.mod": "module example.com/f\n\ngo 1.26\n"})
    disc = _discover(root, tmp_path)
    # b: workspace module, imported without a require; c: imported by a test file; d: a's local
    # replace; b/sub is the longest prefix of b/sub/pkg but outside the workspace (published);
    # e: in-repo but published here; f: only imported by a file the primary build excludes.
    assert disc.unit("example.com/a").in_repo_deps == ("example.com/b", "example.com/c", "example.com/d")
    assert disc.unit("example.com/b").in_repo_deps == ()
    assert disc.unit("example.com/e").in_repo_deps == ()
