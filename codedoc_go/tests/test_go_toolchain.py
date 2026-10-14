"""Toolchain discovery, version order, env_for (spec §3.6), the helper build, setup go."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile

import pytest

from graph_generator import config, go_toolchain
from graph_generator.go_model import BuildConfig, Group, Toolchain
from graph_generator.go_toolchain import (HelperBuildError, ToolchainError, compare_go_versions,
                                          env_for, find_toolchain, from_toolchain_name,
                                          version_tuple)

import go_worlds

LINUX = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    go_toolchain._probe_cache.clear()
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "defaultout"), raising=False)
    yield
    go_toolchain._probe_cache.clear()


def _fake_goroot(base, version="go1.27.2", version_file=True, script=None):
    """A GOROOT whose bin/go is a shell script (it is only run when VERSION is absent)."""
    root = str(base)
    os.makedirs(os.path.join(root, "bin"), exist_ok=True)
    go = os.path.join(root, "bin", "go")
    with open(go, "w") as f:
        f.write(script or f"#!/bin/sh\necho {root}\necho {version}\n")
    os.chmod(go, 0o755)
    if version_file:
        with open(os.path.join(root, "VERSION"), "w") as f:
            f.write(f"{version}\ntime 2026-10-02T20:28:03Z\n")
    return root, go


# ---------------------------------------------------------------- versions

@pytest.mark.parametrize("lo,hi", [
    ("1.21", "1.21rc1"), ("1.21rc1", "1.21rc2"), ("1.21rc2", "1.21.0"), ("1.21.0", "1.21.1"),
    ("1.21.1", "1.22"), ("1.21alpha1", "1.21beta1"), ("1.21beta2", "1.21rc1"),
    ("go1.26.8", "go1.27.2"), ("1.27rc1", "go1.27.2"), ("1.9", "1.10"), ("junk", "1.0"),
])
def test_compare_go_versions_orders_like_the_go_command(lo, hi):
    assert compare_go_versions(lo, hi) == -1
    assert compare_go_versions(hi, lo) == 1


def test_compare_go_versions_equalities():
    assert compare_go_versions("1.20", "1.20.0") == 0      # before 1.21 a missing patch is .0
    assert compare_go_versions("1.21", "1.21.0") == -1
    assert compare_go_versions("go1.27.2", "1.27.2") == 0
    assert compare_go_versions("bad", "worse") == 0


def test_version_tuple():
    assert version_tuple("go1.26.8") == (1, 26, 8)
    assert version_tuple("1.26") == (1, 26, 0)
    assert version_tuple("go1.21rc1") == (1, 21, 0)
    assert version_tuple("go1.27.2-bigcorp") == (1, 27, 2)
    with pytest.raises(ValueError):
        version_tuple("default")


def test_from_toolchain_name():
    assert from_toolchain_name("go1.2.3-bigcorp") == "1.2.3"
    assert from_toolchain_name("go1.21rc1") == "1.21rc1"
    assert from_toolchain_name("default") == ""
    assert from_toolchain_name("go1.x") == ""
    assert from_toolchain_name("go1.2/../x") == ""


# ---------------------------------------------------------------- finding the toolchain

@pytest.fixture
def helper_dir(tmp_path, monkeypatch):
    d = go_worlds.write_tree(tmp_path / "gohelper", {"go.mod": "module codedoc.local/gohelper\n\ngo 1.26\n"})
    monkeypatch.setattr(go_toolchain, "HELPER_DIR", d)
    return d


def test_find_toolchain_prefers_go_toolchain_setting(tmp_path, monkeypatch, helper_dir):
    root, go = _fake_goroot(tmp_path / "goroot")
    monkeypatch.setattr(config, "GO_TOOLCHAIN", go, raising=False)
    monkeypatch.setattr(go_toolchain, "TOOLS_DIR", str(tmp_path / "tools"))
    _fake_goroot(tmp_path / "tools" / "go", version="go1.26.0")
    assert find_toolchain() == Toolchain(go=go, version="go1.27.2", goroot=root)
    go_toolchain._probe_cache.clear()
    monkeypatch.setattr(config, "GO_TOOLCHAIN", root, raising=False)     # a GOROOT works too
    assert find_toolchain().go == go


def test_find_toolchain_then_tools_dir_then_path(tmp_path, monkeypatch, helper_dir):
    monkeypatch.setattr(config, "GO_TOOLCHAIN", None, raising=False)
    monkeypatch.setattr(go_toolchain, "TOOLS_DIR", str(tmp_path / "tools"))
    path_root, _ = _fake_goroot(tmp_path / "pathgo", version="go1.26.3")
    monkeypatch.setenv("PATH", os.path.join(path_root, "bin"))
    assert find_toolchain().version == "go1.26.3"
    tools_root, tools_go = _fake_goroot(tmp_path / "tools" / "go")
    assert find_toolchain() == Toolchain(go=tools_go, version="go1.27.2", goroot=tools_root)


def test_find_toolchain_errors_name_the_fix(tmp_path, monkeypatch, helper_dir):
    monkeypatch.setattr(go_toolchain, "TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(config, "GO_TOOLCHAIN", str(tmp_path / "nope" / "go"), raising=False)
    with pytest.raises(ToolchainError, match="GO_TOOLCHAIN="):
        find_toolchain()
    monkeypatch.setattr(config, "GO_TOOLCHAIN", None, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ToolchainError) as exc:
        find_toolchain()
    assert "setup go" in str(exc.value) and "GO_TOOLCHAIN" in str(exc.value)


def test_find_toolchain_refuses_a_toolchain_older_than_the_helper(tmp_path, monkeypatch, helper_dir):
    _root, go = _fake_goroot(tmp_path / "old", version="go1.25.4")
    monkeypatch.setattr(config, "GO_TOOLCHAIN", go, raising=False)
    with pytest.raises(ToolchainError) as exc:
        find_toolchain()
    assert "go1.25.4" in str(exc.value) and "go1.26" in str(exc.value) and "setup go" in str(exc.value)


def test_probe_falls_back_to_go_env(tmp_path, monkeypatch):
    root = str(tmp_path / "goroot")
    _r, go = _fake_goroot(tmp_path / "goroot", version_file=False,
                          script=f"#!/bin/sh\n[ \"$1 $2 $3\" = 'env GOROOT GOVERSION' ] || exit 3\n"
                                 f"[ \"$GOTOOLCHAIN\" = local ] || exit 4\necho {root}\necho go1.27.2\n")
    assert go_toolchain.probe_toolchain(go) == Toolchain(go=go, version="go1.27.2", goroot=root)
    _r2, bad = _fake_goroot(tmp_path / "bad", version_file=False, script="#!/bin/sh\nexit 1\n")
    with pytest.raises(ToolchainError):
        go_toolchain.probe_toolchain(bad)
    with pytest.raises(ToolchainError):
        go_toolchain.probe_toolchain(str(tmp_path / "missing"))


def test_minimum_go(tmp_path, monkeypatch):
    monkeypatch.setattr(go_toolchain, "HELPER_DIR", str(tmp_path / "none"))
    assert go_toolchain.minimum_go() == "1.25"
    d = go_worlds.write_tree(tmp_path / "h", {"go.mod": "module x\n\ngo 1.24\n"})
    monkeypatch.setattr(go_toolchain, "HELPER_DIR", d)
    assert go_toolchain.minimum_go() == "1.25"
    d2 = go_worlds.write_tree(tmp_path / "h2", {"go.mod": "module x\n\ngo 1.26\n"})
    monkeypatch.setattr(go_toolchain, "HELPER_DIR", d2)
    assert go_toolchain.minimum_go() == "1.26"


# ---------------------------------------------------------------- env_for (spec §3.6)

TC = Toolchain(go="/opt/go/bin/go", version="go1.27.2", goroot="/opt/go")


def _group(**kw):
    base = dict(id="m0", kind="module", dir="/repo/m", modules=["example.com/m"], gowork=None,
                modfile="/out/repos/r/go/m0/go.mod", vendor=False, degraded=False, status="ok",
                pattern="./...")
    base.update(kw)
    return Group(**base)


@pytest.fixture
def hostile_env(monkeypatch):
    """Inherited settings that would run programs or write into the repo."""
    for key, value in {"GOFLAGS": "-mod=mod -toolexec=/bin/evil", "GOPROXY": "direct",
                       "GOPRIVATE": "*", "GONOPROXY": "*", "GONOSUMDB": "*", "GOINSECURE": "*",
                       "GOCACHEPROG": "/bin/evil", "GOEXPERIMENT": "arenas", "GODEBUG": "x=1",
                       "CC": "/bin/evil", "PKG_CONFIG": "/bin/evil", "CGO_CFLAGS": "-x",
                       "GOTOOLCHAIN": "auto", "GOENV": "/home/u/.config/go/env",
                       "GOWORK": "/repo/go.work", "HOME": "/home/u", "USER": "u",
                       "GOAMD64": "v3", "LD_PRELOAD": "/x.so"}.items():
        monkeypatch.setenv(key, value)


def test_env_for_offline_exact(tmp_path, hostile_env):
    out = tmp_path / "out"
    env = env_for(_group(), LINUX, mode="offline", output_dir=str(out),
                  user_modcache="/home/u/go/pkg/mod", toolchain=TC)
    g = os.path.realpath(out) + "/go"
    assert env == {
        "HOME": "/home/u", "USER": "u",
        "PATH": "/opt/go/bin:/usr/bin:/bin", "GOROOT": "/opt/go", "GOTOOLCHAIN": "local",
        "GOENV": "off", "GOFLAGS": "-mod=readonly -modcacherw -buildvcs=false", "GOWORK": "off",
        "GOPROXY": "file:///home/u/go/pkg/mod/cache/download", "GOSUMDB": "off",
        "GOVCS": "*:off", "GOAUTH": "off", "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64",
        "GOMODCACHE": f"{g}/modcache", "GOCACHE": f"{g}/gocache", "GOPATH": f"{g}/gopath",
        "TMPDIR": f"{g}/tmp", "GOTMPDIR": f"{g}/tmp", "XDG_CONFIG_HOME": f"{g}/xdg"}
    with open(f"{g}/xdg/go/telemetry/mode") as f:
        assert f.read().startswith("off 20")
    for d in ("modcache", "gocache", "gopath", "tmp", "bin"):
        assert os.path.isdir(f"{g}/{d}")


def test_env_for_offline_without_a_user_cache_and_vendored(tmp_path, hostile_env):
    env = env_for(_group(vendor=True), LINUX, mode="offline", output_dir=str(tmp_path),
                  user_modcache=None, toolchain=TC)
    assert env["GOPROXY"] == "off" and env["GOSUMDB"] == "off"
    assert env["GOFLAGS"] == "-mod=vendor -modcacherw -buildvcs=false"


def test_env_for_download_and_workspace(tmp_path, hostile_env, monkeypatch):
    monkeypatch.setattr(config, "GO_PROXY", "https://proxy.example.com,direct|off", raising=False)
    ws = _group(id="ws0", kind="workspace", gowork="/out/repos/r/go/ws0/go.work", modfile=None,
                pattern="work")
    cgo = BuildConfig("linux-arm64-cgo", "linux", "arm64", (), True, True)
    env = env_for(ws, cgo, mode="download", output_dir=str(tmp_path), user_modcache="/u/mod",
                  toolchain=TC)
    # R34: the user's cache first, then GO_PROXY (direct and off entries dropped).
    assert env["GOPROXY"] == "file:///u/mod/cache/download,https://proxy.example.com"
    assert env["GOSUMDB"] == "sum.golang.org"
    assert env["GOWORK"] == "/out/repos/r/go/ws0/go.work"
    assert (env["CGO_ENABLED"], env["GOOS"], env["GOARCH"]) == ("1", "linux", "arm64")
    monkeypatch.delattr(config, "GO_PROXY", raising=False)
    monkeypatch.delenv("GO_PROXY", raising=False)
    assert env_for(ws, cgo, mode="download", output_dir=str(tmp_path), user_modcache=None,
                   toolchain=TC)["GOPROXY"] == "https://proxy.golang.org"


def test_env_for_reads_go_sumdb_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GO_SUMDB", "off", raising=False)
    env = env_for(_group(), LINUX, mode="download", output_dir=str(tmp_path), user_modcache=None,
                  toolchain=TC)
    assert env["GOSUMDB"] == "off"
    monkeypatch.setattr(config, "GO_SUMDB", "sum.example.com", raising=False)
    assert env_for(_group(), LINUX, mode="download", output_dir=str(tmp_path), user_modcache=None,
                   toolchain=TC)["GOSUMDB"] == "sum.example.com"
    assert env_for(_group(), LINUX, mode="offline", output_dir=str(tmp_path), user_modcache=None,
                   toolchain=TC)["GOSUMDB"] == "off"


def test_env_for_never_puts_modfile_in_goflags(tmp_path):
    """R13: go/packages' first `go list … unsafe` runs with GO111MODULE=off and fails on
    -modfile in GOFLAGS; -modfile is a command-line flag and -mod stays explicit."""
    for mode in ("offline", "download"):
        for vendor in (False, True):
            flags = env_for(_group(vendor=vendor), LINUX, mode=mode, output_dir=str(tmp_path),
                            user_modcache=None, toolchain=TC)["GOFLAGS"].split()
            assert not any(f.startswith("-modfile") for f in flags)
            assert flags[0] in ("-mod=readonly", "-mod=vendor")


def test_env_for_rejects_unknown_modes(tmp_path):
    with pytest.raises(ValueError):
        env_for(_group(), LINUX, mode="online", output_dir=str(tmp_path), user_modcache=None,
                toolchain=TC)


def test_env_for_finds_the_toolchain_when_not_given(tmp_path, monkeypatch, helper_dir):
    root, go = _fake_goroot(tmp_path / "goroot")
    monkeypatch.setattr(config, "GO_TOOLCHAIN", go, raising=False)
    env = env_for(_group(), LINUX, mode="offline", output_dir=str(tmp_path), user_modcache=None)
    assert env["GOROOT"] == root and env["PATH"].startswith(root + "/bin:")


# ---------------------------------------------------------------- the helper binary

HELPER_SRC = {
    "go.mod": "module codedoc.local/gohelper\n\ngo 1.26\n",
    "main.go": 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("helper ok") }\n',
    "internal/x/x.go": "package x\n",
    "internal/x/x_test.go": "package x\n",
    "internal/x/testdata/t.go": "package t\n",
    "vendor/modules.txt": "",
}


def test_helper_source_hash_tracks_what_goes_into_the_binary(tmp_path):
    src = go_worlds.write_tree(tmp_path / "h", HELPER_SRC)
    assert go_toolchain._helper_files(src) == ["go.mod", "internal/x/x.go", "main.go",
                                               "vendor/modules.txt"]
    h0 = go_toolchain.helper_source_hash(src)
    go_worlds.write_tree(src, {"internal/x/x_test.go": "package x // changed\n",
                               "internal/x/testdata/t.go": "package t // changed\n"})
    assert go_toolchain.helper_source_hash(src) == h0
    go_worlds.write_tree(src, {"go.sum": "x v1 h1:y=\n"})
    h1 = go_toolchain.helper_source_hash(src)
    assert h1 != h0
    go_worlds.write_tree(src, {"internal/x/x.go": "package x // changed\n"})
    assert go_toolchain.helper_source_hash(src) != h1


def test_ensure_helper_refuses_a_toolchain_below_the_helper_floor(tmp_path, monkeypatch, helper_dir):
    go_worlds.write_tree(helper_dir, {"go.mod": "module codedoc.local/gohelper\n\ngo 1.26.0\n"})
    old = Toolchain(go="/opt/go125/bin/go", version="go1.25.9", goroot="/opt/go125")
    with pytest.raises(ToolchainError) as exc:
        go_toolchain.ensure_helper(old, str(tmp_path / "out"))
    assert "go1.26.0" in str(exc.value) and "setup go" in str(exc.value)
    assert "GO_TOOLCHAIN" in str(exc.value)


def test_ensure_helper_needs_a_go_mod(tmp_path, monkeypatch):
    monkeypatch.setattr(go_toolchain, "HELPER_DIR", str(tmp_path / "missing"))
    with pytest.raises(HelperBuildError, match="go.mod"):
        go_toolchain.ensure_helper(TC, str(tmp_path / "out"))


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
def test_ensure_helper_builds_once_and_caches(tmp_path, monkeypatch):
    src = go_worlds.write_tree(tmp_path / "repo" / "gohelper", HELPER_SRC)
    go_worlds.git(tmp_path / "repo", "init", "-q")
    go_worlds.git(tmp_path / "repo", "add", "-A")
    go_worlds.git(tmp_path / "repo", "commit", "-q", "-m", "x")
    os.utime(os.path.join(src, "main.go"), ns=(1_700_000_000_000_000_000,) * 2)
    index = tmp_path / "repo" / ".git" / "index"
    index_mtime = index.stat().st_mtime_ns
    monkeypatch.setattr(go_toolchain, "HELPER_DIR", src)
    tc = go_toolchain.probe_toolchain(go_worlds.GO)
    out = str(tmp_path / "out")
    path = go_toolchain.ensure_helper(tc, out)
    digest = go_toolchain.helper_source_hash(src)[:16]
    assert path == os.path.realpath(out) + f"/go/bin/gohelper-go1.27.2-{digest}"
    assert os.access(path, os.X_OK)
    import subprocess
    assert subprocess.run([path], capture_output=True, text=True).stdout == "helper ok\n"
    assert index.stat().st_mtime_ns == index_mtime       # -buildvcs=false: no git status
    mtime = os.stat(path).st_mtime_ns
    monkeypatch.setattr(go_toolchain.subprocess, "run", lambda *a, **k: pytest.fail("rebuilt"))
    assert go_toolchain.ensure_helper(tc, out) == path and os.stat(path).st_mtime_ns == mtime


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
def test_ensure_helper_reports_compiler_errors(tmp_path, monkeypatch):
    src = go_worlds.write_tree(tmp_path / "gohelper", {**HELPER_SRC, "main.go": "package main\nfunc main() {\n"})
    monkeypatch.setattr(go_toolchain, "HELPER_DIR", src)
    tc = go_toolchain.probe_toolchain(go_worlds.GO)
    with pytest.raises(HelperBuildError) as exc:
        go_toolchain.ensure_helper(tc, str(tmp_path / "out"))
    assert "main.go" in str(exc.value)
    assert not [f for f in os.listdir(tmp_path / "out" / "go" / "bin") if ".tmp-" in f]


# ---------------------------------------------------------------- setup go (download mocked)

def _tarball(files: dict[str, bytes], modes: dict[str, int] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = (modes or {}).get(name, 0o644)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _serve(monkeypatch, data: bytes):
    calls = []

    def fetch(url, path):
        calls.append(url)
        with open(path, "wb") as f:
            f.write(data)
    monkeypatch.setattr(go_toolchain, "_fetch", fetch)
    return calls


GOOD = {"go/VERSION": b"go1.27.2\ntime 2026-10-02T20:28:03Z\n",
        "go/bin/go": b"#!/bin/sh\nexit 0\n", "go/src/go.mod": b"module std\n"}


def test_pinned_archive_constants():
    assert go_toolchain.PINNED_URL == "https://go.dev/dl/go1.27.2.linux-amd64.tar.gz"
    assert go_toolchain.PINNED_SHA256 == ("ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf"
                                          "76996045fe5")


def test_setup_go_verifies_and_installs(tmp_path, monkeypatch):
    data = _tarball(GOOD, {"go/bin/go": 0o755})
    monkeypatch.setattr(go_toolchain, "PINNED_SHA256", hashlib.sha256(data).hexdigest())
    calls = _serve(monkeypatch, data)
    dest = tmp_path / "tools" / "go"
    tc = go_toolchain.setup_go(str(dest))
    assert calls == [go_toolchain.PINNED_URL]
    assert tc == Toolchain(go=str(dest / "bin" / "go"), version="go1.27.2", goroot=str(dest))
    assert os.stat(dest / "bin" / "go").st_mode & stat.S_IXUSR
    assert sorted(os.listdir(tmp_path / "tools")) == ["go"]
    monkeypatch.setattr(go_toolchain, "_fetch", lambda url, path: pytest.fail("downloaded again"))
    go_toolchain._probe_cache.clear()
    assert go_toolchain.setup_go(str(dest)) == tc


def test_setup_go_refuses_a_bad_checksum(tmp_path, monkeypatch):
    _serve(monkeypatch, _tarball(GOOD, {"go/bin/go": 0o755}))
    dest = tmp_path / "tools" / "go"
    with pytest.raises(ToolchainError, match="checksum mismatch"):
        go_toolchain.setup_go(str(dest))
    assert os.listdir(tmp_path / "tools") == []


def test_setup_go_refuses_a_wrong_archive_and_replaces_an_old_install(tmp_path, monkeypatch):
    dest = tmp_path / "tools" / "go"
    _fake_goroot(dest, version="go1.26.0")
    bad = _tarball({"go/VERSION": b"go1.27.1\n", "go/bin/go": b"#!/bin/sh\n"}, {"go/bin/go": 0o755})
    monkeypatch.setattr(go_toolchain, "PINNED_SHA256", hashlib.sha256(bad).hexdigest())
    _serve(monkeypatch, bad)
    with pytest.raises(ToolchainError, match="does not contain"):
        go_toolchain.setup_go(str(dest))
    assert open(dest / "VERSION").read().startswith("go1.26.0")
    good = _tarball(GOOD, {"go/bin/go": 0o755})
    monkeypatch.setattr(go_toolchain, "PINNED_SHA256", hashlib.sha256(good).hexdigest())
    _serve(monkeypatch, good)
    assert go_toolchain.setup_go(str(dest)).version == "go1.27.2"
    assert sorted(os.listdir(tmp_path / "tools")) == ["go"]


def test_setup_go_rejects_path_traversal(tmp_path, monkeypatch):
    evil = _tarball({**GOOD, "../escape.txt": b"x"}, {"go/bin/go": 0o755})
    monkeypatch.setattr(go_toolchain, "PINNED_SHA256", hashlib.sha256(evil).hexdigest())
    _serve(monkeypatch, evil)
    with pytest.raises(Exception):
        go_toolchain.setup_go(str(tmp_path / "tools" / "go"))
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "tools" / "escape.txt").exists()
