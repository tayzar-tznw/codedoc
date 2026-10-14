"""Control directories against the real go command (spec §3.5, §3.6; marker go).

Each test builds a repo under tmp_path, runs discover() and
prepare_control_dirs(), then runs go with env_for() and checks what the go
command did and that the repo stayed byte-identical.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from graph_generator import config, go_toolchain
from graph_generator.go_model import BuildConfig
from graph_generator.go_units import discover, prepare_control_dirs

import go_worlds

pytestmark = [pytest.mark.go,
              pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain or git")]

BUILD = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)
DEP = {"example.com/dep": {"v1.0.0": {
    "go.mod": "module example.com/dep\n\ngo 1.21\n",
    "dep.go": "package dep\n\nfunc D() int { return 1 }\n"}}}


@pytest.fixture
def tc():
    return go_toolchain.probe_toolchain(go_worlds.GO)


@pytest.fixture
def user_cache(tmp_path, monkeypatch):
    """A read-only stand-in for the user's module cache holding example.com/dep."""
    cache = tmp_path / "usercache"
    sums = go_worlds.make_proxy(cache / "cache" / "download", DEP)
    monkeypatch.setattr(config, "GO_USER_MODCACHE", str(cache), raising=False)
    for dirpath, _dirs, files in os.walk(cache):
        for name in files:
            os.chmod(os.path.join(dirpath, name), 0o444)
    yield str(cache), sums
    for dirpath, dirs, files in os.walk(cache):
        for name in files + dirs:
            os.chmod(os.path.join(dirpath, name), 0o755)


def _go(tc, env, cwd, *args, check=True):
    proc = subprocess.run([tc.go, *args], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=300)
    if check and proc.returncode != 0:
        raise AssertionError(f"go {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr}")
    return proc


def _packages(stdout: str) -> dict[str, dict]:
    out, dec, i = {}, json.JSONDecoder(), 0
    while i < len(stdout):
        while i < len(stdout) and stdout[i].isspace():
            i += 1
        if i >= len(stdout):
            break
        pkg, i = dec.raw_decode(stdout, i)
        out[pkg["ImportPath"]] = pkg
    return out


def _discover(root, out, tc, mode="offline"):
    return discover(str(root), "repo", builds=[BUILD], peers=[], mode=mode, output_dir=str(out),
                    toolchain=tc)


WORKSPACE = {
    "go.work": "go 1.26\n\nuse (\n\t./app\n\t./lib\n)\n",
    "app/go.mod": "module example.com/app\n\ngo 1.26\n\nrequire example.com/dep v1.0.0\n",
    "app/main.go": 'package main\n\nimport (\n\t"example.com/dep"\n\t"example.com/lib"\n)\n\n'
                   "func main() { _ = dep.D() + lib.F() }\n",
    "app/main_test.go": 'package main\n\nimport "testing"\n\nfunc TestMain1(t *testing.T) {}\n',
    "lib/go.mod": "module example.com/lib\n\ngo 1.26\n",
    "lib/lib.go": "package lib\n\nfunc F() int { return 2 }\n",
}


def _git_repo(tmp_path, files) -> str:
    root = go_worlds.write_tree(tmp_path / "repo", files)
    go_worlds.git(root, "init", "-q")
    go_worlds.git(root, "add", "-A")
    go_worlds.git(root, "commit", "-q", "-m", "init")
    # A stat change without a content change: any `git status` would now rewrite .git/index.
    os.utime(os.path.join(root, "lib", "lib.go"), ns=(1_700_000_000_000_000_000,) * 2)
    return root


def test_control_dir_never_writes_repo(tmp_path, tc, user_cache):
    cache, _sums = user_cache
    root = _git_repo(tmp_path, WORKSPACE)
    before = go_worlds.snapshot(root)
    index_mtime = os.stat(os.path.join(root, ".git", "index")).st_mtime_ns
    cache_before = go_worlds.snapshot(cache)
    out = tmp_path / "out"
    disc = _discover(root, out, tc)
    assert disc.module_source == "file-proxy" and disc.groups[0].missing_modules == []
    prepare_control_dirs(disc, str(out))
    ws = disc.groups[0]
    env = go_toolchain.env_for(ws, BUILD, mode="offline", output_dir=str(out),
                               user_modcache=cache, toolchain=tc)
    assert env["GOWORK"] == ws.gowork and env["GOPROXY"].startswith("file://")
    # From the control dir (the planned cwd), and from inside the repo, where only
    # -buildvcs=false in env_for's GOFLAGS keeps the go command from running git.
    for cwd in (ws.dir, os.path.join(root, "app")):
        proc = _go(tc, env, cwd, "list", "-e", "-deps", "-test", "-json", "work")
        pkgs = _packages(proc.stdout)
        assert {"example.com/app", "example.com/lib", "example.com/dep"} <= set(pkgs)
        assert not any(p.get("Error") for p in pkgs.values()), proc.stdout
    assert go_worlds.snapshot(root) == before
    assert os.stat(os.path.join(root, ".git", "index")).st_mtime_ns == index_mtime
    assert not os.path.exists(os.path.join(root, "go.work.sum"))
    assert "example.com/dep v1.0.0" in open(os.path.join(ws.dir, "go.work.sum")).read()
    assert go_worlds.snapshot(cache) == cache_before
    xdg = env["XDG_CONFIG_HOME"]
    assert [os.path.relpath(os.path.join(d, f), xdg) for d, _s, fs in os.walk(xdg) for f in fs] == [
        "go/telemetry/mode"]


def test_vcs_stamping_is_what_buildvcs_false_prevents(tmp_path, tc, user_cache):
    """Negative control: the same load from inside the repo without -buildvcs=false
    runs git status there and rewrites .git/index, so the check above can see it."""
    cache, _sums = user_cache
    root = _git_repo(tmp_path, WORKSPACE)
    index_mtime = os.stat(os.path.join(root, ".git", "index")).st_mtime_ns
    out = tmp_path / "out"
    disc = _discover(root, out, tc)
    prepare_control_dirs(disc, str(out))
    ws = disc.groups[0]
    env = go_toolchain.env_for(ws, BUILD, mode="offline", output_dir=str(out),
                               user_modcache=cache, toolchain=tc)
    env["GOFLAGS"] = "-mod=readonly -modcacherw"
    _go(tc, env, os.path.join(root, "app"), "list", "-e", "-deps", "-test", "-json", "work")
    assert os.stat(os.path.join(root, ".git", "index")).st_mtime_ns != index_mtime


def test_modfile_group_reads_the_control_go_sum(tmp_path, tc, user_cache):
    cache, sums = user_cache
    files = {"go.mod": "module example.com/m\n\ngo 1.26\n\nrequire example.com/dep v1.0.0\n",
             "m.go": 'package m\n\nimport "example.com/dep"\n\nvar X = dep.D()\n',
             "go.sum": f"example.com/dep v1.0.0/go.mod {sums['example.com/dep v1.0.0/go.mod']}\n"}
    root = go_worlds.write_tree(tmp_path / "repo", files)
    before = go_worlds.snapshot(root)
    out = tmp_path / "out"
    disc = _discover(root, out, tc)
    prepare_control_dirs(disc, str(out))
    g = disc.groups[0]
    env = go_toolchain.env_for(g, BUILD, mode="offline", output_dir=str(out),
                               user_modcache=cache, toolchain=tc)
    assert env["GOWORK"] == "off"
    args = ("list", "-e", "-deps", "-test", "-json=ImportPath,Error", f"-modfile={g.modfile}", "./...")
    # The repo's go.sum lacks the zip hash: readonly mode refuses, like the developer's build.
    first = _packages(_go(tc, env, g.dir, *args).stdout)
    assert "missing go.sum entry" in json.dumps(first)
    # Completing only the control copy fixes it: the go command reads <modfile dir>/go.sum.
    with open(os.path.join(os.path.dirname(g.modfile), "go.sum"), "a") as f:
        f.write(f"example.com/dep v1.0.0 {sums['example.com/dep v1.0.0']}\n")
    second = _packages(_go(tc, env, g.dir, *args).stdout)
    assert not any(p.get("Error") for p in second.values()) and "example.com/dep" in second
    assert go_worlds.snapshot(root) == before


def test_vendored_module_with_modfile_uses_vendor(tmp_path, tc, monkeypatch):
    monkeypatch.setattr(config, "GO_USER_MODCACHE", "", raising=False)
    files = {"go.mod": "module example.com/v\n\ngo 1.22\n\nrequire example.com/dep v1.0.0\n",
             "v.go": 'package v\n\nimport "example.com/dep"\n\nvar X = dep.D()\n',
             "vendor/modules.txt": "# example.com/dep v1.0.0\n## explicit; go 1.21\nexample.com/dep\n",
             "vendor/example.com/dep/dep.go": "package dep\n\nfunc D() int { return 1 }\n"}
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", files))
    before = go_worlds.snapshot(root)
    out = tmp_path / "out"
    disc = _discover(root, out, tc)
    g = disc.groups[0]
    assert g.vendor and disc.module_source == "vendor"
    prepare_control_dirs(disc, str(out))
    env = go_toolchain.env_for(g, BUILD, mode="offline", output_dir=str(out), user_modcache=None,
                               toolchain=tc)
    assert env["GOFLAGS"].startswith("-mod=vendor") and env["GOPROXY"] == "off"
    pkgs = _packages(_go(tc, env, g.dir, "list", "-e", "-deps", "-json=ImportPath,Dir,Error",
                         f"-modfile={g.modfile}", "./...").stdout)
    assert pkgs["example.com/dep"]["Dir"] == f"{root}/vendor/example.com/dep"
    assert not any(p.get("Error") for p in pkgs.values())
    assert go_worlds.snapshot(root) == before


def test_workspace_vendor_view_is_consistent(tmp_path, tc, monkeypatch):
    monkeypatch.setattr(config, "GO_USER_MODCACHE", "", raising=False)
    files = {"go.work": "go 1.22\n\nuse ./a\n\nreplace example.com/w => ./wdir\n",
             "a/go.mod": "module example.com/a\n\ngo 1.22\n\nrequire (\n\texample.com/loc v0.0.0\n"
                         "\texample.com/w v0.0.0\n)\n\nreplace example.com/loc => ./local\n",
             "a/a.go": 'package a\n\nimport (\n\t"example.com/loc"\n\t"example.com/w"\n)\n\n'
                       "var X = loc.L() + w.W()\n",
             "a/local/go.mod": "module example.com/loc\n\ngo 1.22\n",
             "a/local/l.go": "package loc\n\nfunc L() int { return 1 }\n",
             "wdir/go.mod": "module example.com/w\n\ngo 1.22\n",
             "wdir/w.go": "package w\n\nfunc W() int { return 2 }\n"}
    root = os.path.realpath(go_worlds.write_tree(tmp_path / "repo", files))
    out = tmp_path / "out"
    env = go_toolchain.base_env(tc, str(out))
    env.update({"GOFLAGS": "-mod=mod -buildvcs=false", "GOPROXY": "off", "GOSUMDB": "off",
                "GOWORK": os.path.join(root, "go.work"), "CGO_ENABLED": "0"})
    _go(tc, env, root, "work", "vendor")          # the developer's own `go work vendor`
    assert open(os.path.join(root, "vendor", "modules.txt")).readline() == "## workspace\n"
    before = go_worlds.snapshot(root)
    disc = _discover(root, out, tc)
    ws = disc.groups[0]
    assert ws.vendor
    prepare_control_dirs(disc, str(out))
    assert os.path.isdir(os.path.join(ws.dir, "vendor"))
    env = go_toolchain.env_for(ws, BUILD, mode="offline", output_dir=str(out), user_modcache=None,
                               toolchain=tc)
    proc = _go(tc, env, ws.dir, "list", "-e", "-deps", "-json=ImportPath,Dir,Error", "work")
    pkgs = _packages(proc.stdout)
    assert not any(p.get("Error") for p in pkgs.values()), proc.stdout
    assert os.path.realpath(pkgs["example.com/loc"]["Dir"]) == f"{root}/vendor/example.com/loc"
    assert go_worlds.snapshot(root) == before


def test_raised_workspace_go_line_loads_where_the_repo_go_work_fails(tmp_path, tc, user_cache):
    cache, _sums = user_cache
    files = {"go.work": "go 1.21\n\nuse ./a\n",
             "a/go.mod": "module example.com/a\n\ngo 1.22\n", "a/a.go": "package a\n"}
    root = go_worlds.write_tree(tmp_path / "repo", files)
    out = tmp_path / "out"
    disc = _discover(root, out, tc)
    prepare_control_dirs(disc, str(out))
    ws = disc.groups[0]
    env = go_toolchain.env_for(ws, BUILD, mode="offline", output_dir=str(out),
                               user_modcache=cache, toolchain=tc)
    repo_env = dict(env, GOWORK=os.path.join(root, "go.work"))
    bad = _go(tc, repo_env, ws.dir, "list", "work", check=False)
    assert bad.returncode != 0 and "go.work lists go 1.21" in bad.stderr
    assert _go(tc, env, ws.dir, "list", "work").stdout.split() == ["example.com/a"]
    assert not os.path.exists(os.path.join(root, "go.work.sum"))


def test_go_command_agrees_with_the_toolchain_gate(tmp_path, tc, user_cache):
    """A newer go line fails closed in the go command too; a newer toolchain line does not."""
    cache, _sums = user_cache
    files = {"new/go.mod": "module example.com/new\n\ngo 1.28\n", "new/n.go": "package n\n",
             "tl/go.mod": "module example.com/tl\n\ngo 1.26\n\ntoolchain go1.28.1\n",
             "tl/t.go": "package tl\n"}
    root = go_worlds.write_tree(tmp_path / "repo", files)
    out = tmp_path / "out"
    disc = _discover(root, out, tc)
    prepare_control_dirs(disc, str(out))
    by_mod = {g.modules[0]: g for g in disc.groups}
    for key, want_rc in (("example.com/new", 1), ("example.com/tl", 0)):
        g = by_mod[key]
        assert g.status == ("toolchain" if want_rc else "ok")
        env = go_toolchain.env_for(g, BUILD, mode="offline", output_dir=str(out),
                                   user_modcache=cache, toolchain=tc)
        proc = _go(tc, env, g.dir, "list", f"-modfile={g.modfile}", "./...", check=False)
        assert proc.returncode == want_rc, proc.stderr
        if want_rc:
            assert "requires go >= 1.28 (running go 1.27.2; GOTOOLCHAIN=local)" in proc.stderr
