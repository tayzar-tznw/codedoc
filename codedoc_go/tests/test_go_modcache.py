"""Module sources: the user's cache, availability, download mode (spec §3.7)."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from graph_generator import config, go_modcache, go_toolchain
from graph_generator.go_model import BuildConfig, Toolchain
from graph_generator.go_modcache import escape_path, module_available, user_modcache
from graph_generator.go_units import discover, prepare_control_dirs

import go_worlds

LINUX = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)


@pytest.fixture(autouse=True)
def _out(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "defaultout"), raising=False)
    monkeypatch.delattr(config, "GO_USER_MODCACHE", raising=False)
    monkeypatch.delenv("GO_USER_MODCACHE", raising=False)


def test_escape_path():
    assert escape_path("github.com/Azure/azure-sdk") == "github.com/!azure/azure-sdk"
    assert escape_path("v1.0.0-RC1") == "v1.0.0-!r!c1"
    assert escape_path("example.com/ok") == "example.com/ok"


def test_module_available(tmp_path):
    user = tmp_path / "user"
    go_worlds.make_proxy(user / "cache" / "download", {
        "example.com/Up": {"v1.0.0": {"go.mod": "module example.com/Up\n", "u.go": "package up\n"}}})
    iso = tmp_path / "iso"
    os.makedirs(iso / "cache" / "download" / "example.com" / "x" / "@v")
    (iso / "cache" / "download" / "example.com" / "x" / "@v" / "v1.2.0.mod").write_text("module example.com/x\n")
    assert module_available("example.com/Up", "v1.0.0", [str(iso), str(user)])
    assert not module_available("example.com/x", "v1.2.0", [str(iso)])
    assert module_available("example.com/x", "v1.2.0", [str(iso)], mod_only=True)
    os.makedirs(iso / "example.com" / "x@v1.2.0")
    assert module_available("example.com/x", "v1.2.0", [str(iso)])
    assert not module_available("example.com/y", "v1.0.0", [str(iso), str(user)])


# ---------------------------------------------------------------- user_modcache

def _cache(path):
    os.makedirs(os.path.join(path, "cache", "download"), exist_ok=True)
    return str(path)


def test_user_modcache_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GO_USER_MODCACHE", _cache(tmp_path / "mc"), raising=False)
    assert user_modcache(None) == os.path.realpath(tmp_path / "mc")
    monkeypatch.setattr(config, "GO_USER_MODCACHE", str(tmp_path / "empty"), raising=False)
    assert user_modcache(None) is None


def _fake_go(tmp_path, body: str) -> Toolchain:
    root = tmp_path / "goroot"
    os.makedirs(root / "bin", exist_ok=True)
    go = root / "bin" / "go"
    go.write_text("#!/bin/sh\n" + body)
    go.chmod(0o755)
    return Toolchain(go=str(go), version="go1.27.2", goroot=str(root))


def test_user_modcache_asks_go_env_with_the_users_environment(tmp_path, monkeypatch):
    cache = _cache(tmp_path / "home" / "go" / "pkg" / "mod")
    dump = tmp_path / "env.txt"
    tc = _fake_go(tmp_path, f'env > {dump}\n[ "$1 $2" = "env GOMODCACHE" ] || exit 2\necho {cache}\n')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "userxdg"))
    monkeypatch.delenv("GOENV", raising=False)
    monkeypatch.setenv("GOTOOLCHAIN", "auto")
    monkeypatch.setenv("GOFLAGS", "-modfile=/x")
    monkeypatch.setenv("GOPATH", str(tmp_path / "gp"))
    monkeypatch.setenv("GOMODCACHE", cache)
    monkeypatch.setenv("GOPROXY", "direct")
    monkeypatch.setenv("GO_TOOLCHAIN", "/elsewhere/go")
    monkeypatch.setenv("LD_PRELOAD", "/x.so")
    assert user_modcache(tc, str(tmp_path / "base")) == os.path.realpath(cache)
    env = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    # R14: GOTOOLCHAIN=local, a neutral cwd, and only the user's HOME, GOPATH, GOMODCACHE.
    env.pop("PWD", None), env.pop("SHLVL", None), env.pop("_", None), env.pop("OLDPWD", None)
    assert sorted(env) == ["GOENV", "GOMODCACHE", "GOPATH", "GOTOOLCHAIN", "HOME", "PATH",
                           "XDG_CONFIG_HOME"]
    assert env["GOTOOLCHAIN"] == "local" and env["GOMODCACHE"] == cache
    assert env["GOENV"] == str(tmp_path / "userxdg" / "go" / "env")     # the user's go env file
    assert env["GOPATH"] == str(tmp_path / "gp")
    assert env["XDG_CONFIG_HOME"] == os.path.realpath(tmp_path / "base") + "/go/xdg"   # telemetry off
    with open(os.path.join(env["XDG_CONFIG_HOME"], "go", "telemetry", "mode")) as f:
        assert f.read().startswith("off")


def test_user_modcache_failures_mean_none(tmp_path, monkeypatch):
    assert user_modcache(_fake_go(tmp_path, "exit 1\n")) is None
    assert user_modcache(_fake_go(tmp_path, f"echo {tmp_path}/nocache\n")) is None
    assert user_modcache(Toolchain(go=str(tmp_path / "nothing"), version="go1.27.2",
                                   goroot=str(tmp_path))) is None
    assert user_modcache(None) is None


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
def test_user_modcache_with_the_real_go_command(tmp_path, monkeypatch):
    tc = go_toolchain.probe_toolchain(go_worlds.GO)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for key in ("GOPATH", "GOMODCACHE", "GOENV", "XDG_CONFIG_HOME", "GOFLAGS"):
        monkeypatch.delenv(key, raising=False)
    default = _cache(home / "go" / "pkg" / "mod")
    assert user_modcache(tc) == os.path.realpath(default)
    # A GOMODCACHE set in the user's go env file is honoured.
    other = _cache(tmp_path / "elsewhere")
    os.makedirs(home / ".config" / "go")
    (home / ".config" / "go" / "env").write_text(f"GOMODCACHE={other}\n")
    assert user_modcache(tc) == os.path.realpath(other)
    assert not os.path.exists(home / ".config" / "go" / "telemetry")      # nothing written there


# ---------------------------------------------------------------- download

def _disc(root, out, tc, mode="download"):
    return discover(str(root), "repo", builds=[LINUX], peers=[], mode=mode, output_dir=str(out),
                    toolchain=tc)


def test_download_parses_and_attributes_errors(tmp_path):
    lines = [{"ImportPath": "example.com/m"},
             {"ImportPath": "example.com/m/empty", "Error": {"Err": "no Go files in /x"}},
             {"ImportPath": "example.com/dep/sub", "Error": {"Err": "example.com/dep@v1.0.0: 404"}},
             {"ImportPath": "other.org/x/y", "Error": {"Err": "other.org/x@v0.1.0: reading: gone"}},
             {"ImportPath": "fmt"}]
    tc = _fake_go(tmp_path, "cat <<'EOF'\n" + "\n".join(json.dumps(x) for x in lines) + "\nEOF\n")
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire example.com/dep v1.0.0\n",
        "v/go.mod": "module example.com/v\n\ngo 1.22\n\nrequire example.com/dep v1.0.0\n",
        "v/vendor/modules.txt": "# example.com/dep v1.0.0\n## explicit\nexample.com/dep\n",
        "new/go.mod": "module example.com/new\n\ngo 1.29\n"})
    disc = _disc(root, tmp_path / "out", tc)
    report = go_modcache.download(disc, tc, str(tmp_path / "out"))
    by_mod = {g.modules[0]: report[g.id] for g in disc.groups}
    m = by_mod["example.com/m"]
    assert m["status"] == "partial" and m["packages"] == 5 and m["package_errors"] == 1
    assert m["failed_modules"] == {"example.com/dep": "example.com/dep@v1.0.0: 404",
                                   "other.org/x": "other.org/x@v0.1.0: reading: gone"}
    assert m["builds"] == ["linux-amd64"] and m["missing_requirements"] == {}
    g = disc.group_of("example.com/m")
    assert any(w.startswith("2 module(s) could not be downloaded, so calls into them are "
                            "unresolved (missing-module): example.com/dep (example.com/dep@v1.0.0: 404)")
               for w in g.warnings)
    assert any(w.startswith(f"group {g.id}: 2 module(s) could not be downloaded") for w in disc.warnings)
    assert by_mod["example.com/v"] == {**by_mod["example.com/v"], "status": "skipped", "reason": "vendor"}
    assert by_mod["example.com/new"]["status"] == "skipped"
    assert by_mod["example.com/new"]["reason"] == "toolchain"


def test_download_failure_does_not_stop_the_run(tmp_path):
    tc = _fake_go(tmp_path, "echo 'go: errors parsing go.mod' >&2\nexit 1\n")
    root = go_worlds.write_tree(tmp_path / "repo", {"a/go.mod": "module example.com/a\n\ngo 1.26\n",
                                                     "b/go.mod": "module example.com/b\n\ngo 1.26\n"})
    disc = _disc(root, tmp_path / "out", tc)
    report = go_modcache.download(disc, tc, str(tmp_path / "out"))
    assert [r["status"] for r in report.values()] == ["failed", "failed"]
    assert all("errors parsing go.mod" in r["reason"] for r in report.values())


def test_download_passes_the_load_flags(tmp_path, monkeypatch):
    dump = tmp_path / "args.txt"
    tc = _fake_go(tmp_path, f'echo "$PWD $@" >> {dump}\n')
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.work": "go 1.26\n\nuse ./a\n", "a/go.mod": "module example.com/a\n\ngo 1.26\n",
        "b/go.mod": "module example.com/b\n\ngo 1.26\n"})
    tagged = BuildConfig("windows-amd64+x", "windows", "amd64", ("x",), False, False)
    disc = discover(str(root), "repo", builds=[LINUX, tagged], peers=[], mode="download",
                    output_dir=str(tmp_path / "out"), toolchain=tc)
    go_modcache.download(disc, tc, str(tmp_path / "out"))
    calls = [c for c in dump.read_text().splitlines() if " list " in c]   # not `go env GOMODCACHE`
    flags = "list -e -deps -test -compiled=false -buildvcs=false -json=ImportPath,Error,Imports"
    ws, m = disc.groups
    # R12 (refined): a module group downloads in module mode on its control copy.
    assert m.gowork is None and m.modfile.endswith("/go/m0/go.mod")
    assert calls == [f"{ws.dir} {flags} work", f"{ws.dir} {flags} -tags=x work",
                     f"{m.dir} {flags} -mod=mod -modfile={m.modfile} ./...",
                     f"{m.dir} {flags} -tags=x -mod=mod -modfile={m.modfile} ./..."]


@pytest.fixture
def remote_proxy(tmp_path, monkeypatch):
    """A file:// stand-in for proxy.golang.org, an empty user cache (R34 falls
    through to it), and a guard that checks env_for's download environment and
    then switches GOSUMDB off so the test never reaches the network."""
    spec = {"example.com/dep": {"v1.0.0": {"go.mod": "module example.com/dep\n\ngo 1.21\n",
                                           "dep.go": "package dep\n\nfunc D() {}\n"}}}
    remote = tmp_path / "remote"
    sums = go_worlds.make_proxy(remote, spec)
    monkeypatch.setattr(config, "GO_PROXY", remote.as_uri(), raising=False)
    user = tmp_path / "usercache"
    os.makedirs(user / "cache" / "download")
    monkeypatch.setattr(config, "GO_USER_MODCACHE", str(user), raising=False)
    real_env_for = go_toolchain.env_for

    def guarded(*args, **kwargs):
        env = real_env_for(*args, **kwargs)
        assert env["GOSUMDB"] == "sum.golang.org"
        assert env["GOPROXY"] == f"{(user / 'cache' / 'download').as_uri()},{remote.as_uri()}"
        return dict(env, GOSUMDB="off")
    monkeypatch.setattr(go_modcache, "env_for", guarded)
    return sums


def _load(tc, disc, g, out, user=None):
    """The readonly load on the control copy (what the helper does), as {import path: error}."""
    env = go_toolchain.env_for(g, LINUX, mode="download", output_dir=str(out), user_modcache=user,
                               toolchain=tc)
    p = subprocess.run([tc.go, "list", "-e", "-deps", "-test", "-json=ImportPath,Error",
                        f"-modfile={g.modfile}", "./..."], cwd=g.dir, env=dict(env, GOSUMDB="off"),
                       capture_output=True, text=True)
    return {o["ImportPath"]: (o.get("Error") or {}).get("Err", "") for o in go_modcache._json_stream(p.stdout)}


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
def test_r12_download_loses_only_the_missing_module_and_fills_go_sum(tmp_path, remote_proxy):
    """R12 criteria, probed 2026-10-14 against the one-use go.work alternatives:
    (i) a 404 dependency loses only its own packages; (ii) missing go.sum lines are
    filled (in the control copy); (iii) the repo stays byte-identical."""
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n\nrequire (\n\texample.com/dep v1.0.0\n"
                  "\texample.com/gone v1.0.0\n)\n",
        "go.sum": "",
        "m.go": 'package m\n\nimport "example.com/dep"\n\nfunc F() { dep.D() }\n',
        "g/g.go": 'package g\n\nimport "example.com/gone"\n\nvar _ = gone.X\n'})
    before = go_worlds.snapshot(root)
    tc = go_toolchain.probe_toolchain(go_worlds.GO)
    out = tmp_path / "out"
    disc = _disc(root, out, tc)
    prepare_control_dirs(disc, str(out))
    g = disc.groups[0]
    report = go_modcache.download(disc, tc, str(out))["m0"]
    assert report["status"] == "partial" and list(report["failed_modules"]) == ["example.com/gone"]
    assert report["missing_requirements"] == {} and not g.degraded
    iso = os.path.realpath(out) + "/go/modcache"
    assert module_available("example.com/dep", "v1.0.0", [iso])                      # (i)
    control_sum = open(os.path.join(os.path.dirname(g.modfile), "go.sum")).read()
    assert "example.com/dep v1.0.0 h1:" in control_sum                                # (ii)
    errors = {k: v for k, v in _load(tc, disc, g, out).items() if v}
    assert list(errors) == ["example.com/gone"]                                      # (i)
    assert go_worlds.snapshot(root) == before                                        # (iii)


@pytest.mark.go
@pytest.mark.skipif(not go_worlds.have_go(), reason="no go toolchain")
def test_r12_undeclared_imports_fail_like_the_developers_build(tmp_path, remote_proxy):
    """The lead's ruling of 2026-10-14 (F3 NEG-004): the download may add a
    requirement the repo's go.mod lacks, but the copy is put back, so only the
    packages that import an undeclared module fail, as in the developer's
    readonly build; the go.sum fill stays and the repo is untouched."""
    root = go_worlds.write_tree(tmp_path / "repo", {
        "go.mod": "module example.com/m\n\ngo 1.26\n",
        "m.go": 'package m\n\nimport "example.com/dep"\n\nfunc F() { dep.D() }\n',
        "n/n.go": "package n\n\nfunc N() {}\n"})
    before = go_worlds.snapshot(root)
    tc = go_toolchain.probe_toolchain(go_worlds.GO)
    out = tmp_path / "out"
    disc = _disc(root, out, tc)
    prepare_control_dirs(disc, str(out))
    g = disc.groups[0]
    copy = open(g.modfile, "rb").read()
    report = go_modcache.download(disc, tc, str(out))["m0"]
    assert report["missing_requirements"] == {"example.com/dep@v1.0.0": ["example.com/m"]}
    assert open(g.modfile, "rb").read() == copy and not g.degraded
    control_sum = open(os.path.join(os.path.dirname(g.modfile), "go.sum")).read()
    assert "example.com/dep v1.0.0 h1:" in control_sum
    assert any(w.startswith("go.mod lacks requirements for: example.com/dep@v1.0.0 "
                            "(imported by example.com/m).") for w in g.warnings)
    assert any(w.startswith("group m0: go.mod lacks requirements for:") for w in disc.warnings)
    errors = {k: v for k, v in _load(tc, disc, g, out).items() if v}
    assert list(errors) == ["example.com/dep"]
    assert "cannot find module providing package example.com/dep" in errors["example.com/dep"]
    assert go_worlds.snapshot(root) == before


# ---------------------------------------------------------------- the go.mod copy is put back

_LIST_JSON = [
    {"ImportPath": "example.com/m", "Imports": ["fmt"]},
    {"ImportPath": "example.com/m/u", "Imports": ["example.com/dep/sub"]},
    {"ImportPath": "example.com/m/u [example.com/m/u.test]", "Imports": ["example.com/dep/sub", "testing"]},
    {"ImportPath": "example.com/m/u_test [example.com/m/u.test]", "Imports": ["example.com/dep"]},
    {"ImportPath": "example.com/m/u.test", "Imports": ["example.com/m/u_test"]},
    {"ImportPath": "example.com/dep/sub"},
    {"ImportPath": "example.com/lib", "Imports": ["example.com/dep"]},
    {"ImportPath": "fmt"}]


def _filling_go(tmp_path, require_line: str, *, exit_code: int = 0, output: bool = True) -> Toolchain:
    """A fake go whose `list` behaves like -mod=mod: it adds *require_line* to
    the -modfile copy and a line to the go.sum beside it."""
    lines = "\n".join(json.dumps(x) for x in _LIST_JSON) if output else ""
    return _fake_go(tmp_path, f"""[ "$1" = list ] || exit 0
for a in "$@"; do case "$a" in -modfile=*) mf="${{a#-modfile=}}";; esac; done
printf '\\n{require_line}\\n' >> "$mf"
echo 'example.com/dep v1.1.0 h1:filled=' >> "$(dirname "$mf")/go.sum"
cat <<'JSON'
{lines}
JSON
exit {exit_code}
""")


def _module_repo(tmp_path, gomod: str) -> str:
    return go_worlds.write_tree(tmp_path / "repo", {"go.mod": gomod, "go.sum": "",
                                                     "u/u.go": "package u\n"})


def test_download_puts_the_go_mod_copy_back_and_names_the_importers(tmp_path):
    root = _module_repo(tmp_path, "module example.com/m\n\ngo 1.26\n")
    tc = _filling_go(tmp_path, "require example.com/dep v1.1.0")
    out = tmp_path / "out"
    disc = _disc(root, out, tc)
    prepare_control_dirs(disc, str(out))
    g = disc.groups[0]
    copy = open(g.modfile, "rb").read()
    report = go_modcache.download(disc, tc, str(out))["m0"]
    assert open(g.modfile, "rb").read() == copy                        # the require set is the repo's
    assert "example.com/dep v1.1.0 h1:filled=" in open(os.path.join(os.path.dirname(g.modfile),
                                                                     "go.sum")).read()
    assert report["missing_requirements"] == {"example.com/dep@v1.1.0": ["example.com/lib",
                                                                         "example.com/m/u"]}
    assert report["status"] == "ok" and not g.degraded
    warning = ("go.mod lacks requirements for: example.com/dep@v1.1.0 (imported by example.com/lib, "
               "example.com/m/u). The download added them to the go.mod copy and put the copy back, "
               "so the load stays readonly like the developer's build: imports from these modules "
               "are unresolved, or the go command refuses the module (updates to go.mod needed). "
               "Fix: go mod tidy in the repo")
    assert g.warnings[-1] == warning and disc.warnings[-1] == f"group m0: {warning}"
    assert open(os.path.join(root, "go.mod")).read() == "module example.com/m\n\ngo 1.26\n"


def test_download_reports_a_raised_requirement_with_both_versions(tmp_path):
    root = _module_repo(tmp_path, "module example.com/m\n\ngo 1.26\n\nrequire example.com/dep v1.0.0\n")
    tc = _filling_go(tmp_path, "replace example.com/none => ./none\\nrequire example.com/dep v1.1.0")
    out = tmp_path / "out"
    disc = _disc(root, out, tc)
    report = go_modcache.download(disc, tc, str(out))["m0"]
    assert report["missing_requirements"] == {"example.com/dep@v1.1.0": ["example.com/lib",
                                                                         "example.com/m/u"]}
    g = disc.groups[0]
    assert g.warnings[-1].startswith("go.mod lacks requirements for: example.com/dep v1.0.0 -> v1.1.0 "
                                     "(imported by example.com/lib, example.com/m/u).")
    assert b"replace" not in open(g.modfile, "rb").read()


def test_download_puts_the_copy_back_when_the_go_command_fails(tmp_path):
    root = _module_repo(tmp_path, "module example.com/m\n\ngo 1.26\n")
    tc = _filling_go(tmp_path, "require example.com/dep v1.1.0", exit_code=1, output=False)
    out = tmp_path / "out"
    disc = _disc(root, out, tc)
    prepare_control_dirs(disc, str(out))
    g = disc.groups[0]
    copy = open(g.modfile, "rb").read()
    report = go_modcache.download(disc, tc, str(out))["m0"]
    assert report["status"] == "failed" and open(g.modfile, "rb").read() == copy
    # the requirement was added before the failure, so it is still reported
    assert list(report["missing_requirements"]) == ["example.com/dep@v1.1.0"]


def test_missing_requirement_listing_is_capped():
    missing = {f"example.com/d{i}@v1.0.0": [f"example.com/m/p{j}" for j in range(5)] for i in range(7)}
    text = go_modcache._missing_warning(missing, {})
    assert text.count("(imported by example.com/m/p0, example.com/m/p1, example.com/m/p2 …)") == 5
    assert "example.com/d5" not in text and "example.com/d4@v1.0.0" in text and "; and 2 more. The download" in text
    assert go_modcache._package_of("example.com/m_test [example.com/m.test]") == "example.com/m"
    assert go_modcache._package_of("example.com/m.test") == "example.com/m"
    assert go_modcache._package_of("example.com/m/sub [example.com/m/sub.test]") == "example.com/m/sub"
