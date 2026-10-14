import io
import json
import os
import subprocess
import sys
import zipfile

import pytest

from conftest import PROXY, proxy_sums, write

sys.path.insert(0, PROXY)
import build_proxy as bp  # noqa: E402

MODULES = ("example.com/thirdparty/textfmt", "example.com/thirdparty/uuidlite", "example.com/thirdparty/yamlish")


def test_committed_proxy_is_up_to_date(capsys):
    assert bp.main(["build_proxy.py", "--check"]) == 0
    assert "proxy OK: 7 modules, 10 versions" in capsys.readouterr().out


def test_layout_list_info_mod_and_zip():
    outputs = bp.build()
    for module in MODULES:
        base = module + "/@v/"
        assert outputs[base + "list"] == b"v1.0.0\nv1.1.0\n"
        info = json.loads(outputs[base + "v1.1.0.info"])
        assert info == {"Version": "v1.1.0", "Time": "2026-03-02T00:00:00Z"}
        assert outputs[base + "v1.0.0.mod"].startswith(f"module {module}\n".encode())
        with zipfile.ZipFile(io.BytesIO(outputs[base + "v1.1.0.zip"])) as zf:
            names = zf.namelist()
            assert names == sorted(names) and all(n.startswith(f"{module}@v1.1.0/") for n in names)
            assert f"{module}@v1.1.0/go.mod" in names and not any(n.endswith("/") for n in names)
            assert all(i.compress_type == zipfile.ZIP_STORED and i.date_time == bp.ZIP_DATE for i in zf.infolist())


def test_v1_1_0_adds_a_method():
    outputs = bp.build()
    for module, method in (("uuidlite", "Short"), ("textfmt", "Len"), ("yamlish", "Keys")):
        def source(version):
            with zipfile.ZipFile(io.BytesIO(outputs[f"example.com/thirdparty/{module}/@v/{version}.zip"])) as zf:
                return zf.read(f"example.com/thirdparty/{module}@{version}/{'uuid' if module == 'uuidlite' else module}.go")
        old, new = source("v1.0.0").decode(), source("v1.1.0").decode()
        assert f") {method}(" in new and f") {method}(" not in old and new.startswith(old.rstrip("\n"))


def test_hashes_equal_the_go_command():
    # values printed by `go mod download -json` with go1.27.2 (2026-10-13)
    sums = proxy_sums("example.com/thirdparty/uuidlite v1.0.0", "example.com/thirdparty/yamlish v1.0.0")
    assert "example.com/thirdparty/uuidlite v1.0.0 h1:U4ihG+ZfSDCx4Qn73SDgzuKoN31GU+Oz4icQttytru0=" in sums
    assert "example.com/thirdparty/uuidlite v1.0.0/go.mod h1:Wicloi4Thty50gJIuDh9Mgg6dA693lPRDliLNNxuFfI=" in sums
    assert "example.com/thirdparty/yamlish v1.0.0 h1:QjNfF0TEG9HVMkAUcB73wz+w3L0C5F5v0dAdw93pghc=" in sums
    assert bp.hash1({"go.mod": b"module x\n"}) == bp.hash1({"go.mod": b"module x\n"})


def test_escape_and_versions():
    assert bp.escape("github.com/Azure/Go") == "github.com/!azure/!go"
    assert sorted(["v1.10.0", "v1.2.0", "v1.2.0-rc.1"], key=bp.version_key) == ["v1.2.0-rc.1", "v1.2.0", "v1.10.0"]


def test_build_rejects_bad_sources_and_skips_nested_modules(tmp_path):
    src = tmp_path / "src"
    write(str(src), {"example.com/x/v1.0.0/go.mod": "module example.com/x\n\ngo 1.21\n",
                     "example.com/x/v1.0.0/x.go": "package x\n",
                     "example.com/x/v1.0.0/sub/go.mod": "module example.com/x/sub\n",
                     "example.com/x/v1.0.0/sub/s.go": "package sub\n",
                     "example.com/x/v1.0.0/vendor/v.go": "package v\n"})
    out = bp.build(str(src))
    with zipfile.ZipFile(io.BytesIO(out["example.com/x/@v/v1.0.0.zip"])) as zf:
        assert zf.namelist() == ["example.com/x@v1.0.0/go.mod", "example.com/x@v1.0.0/x.go"]
    write(str(src), {"example.com/y/v2.0.0/go.mod": "module example.com/y\n"})
    with pytest.raises(bp.ProxyError, match="needs the /v2 suffix"):
        bp.build(str(src))
    os.remove(src / "example.com/y/v2.0.0/go.mod")
    write(str(src), {"example.com/z/v1.0.0/go.mod": "module example.com/other\n"})
    with pytest.raises(bp.ProxyError, match="declares module 'example.com/other'"):
        bp.build(str(src))


@pytest.mark.go
def test_temp_module_builds_through_the_proxy(go_bin, tmp_path):
    mod = tmp_path / "m"
    write(str(mod), {
        "go.mod": "module example.com/smoke\n\ngo 1.26\n\nrequire example.com/thirdparty/uuidlite v1.1.0\n",
        "go.sum": proxy_sums("example.com/thirdparty/uuidlite v1.1.0"),
        "main.go": 'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/thirdparty/uuidlite"\n)\n\n'
                   'func main() { fmt.Println(uuidlite.New().Short(), uuidlite.FromInt(42)) }\n'})
    env = {"PATH": os.path.dirname(go_bin) + ":/usr/bin:/bin", "HOME": os.environ.get("HOME", str(tmp_path)),
           "GOENV": "off", "GOTOOLCHAIN": "local", "GOFLAGS": "-modcacherw", "GOPROXY": "file://" + PROXY,
           "GOSUMDB": "off", "GOMODCACHE": str(tmp_path / "modcache"), "GOPATH": str(tmp_path / "gopath"),
           "GOWORK": "off", "CGO_ENABLED": "0"}
    if os.environ.get("GOCACHE"):
        env["GOCACHE"] = os.environ["GOCACHE"]
    run = subprocess.run([go_bin, "run", "."], cwd=mod, env=env, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "00000000 00000000-0000-4000-8000-00000000002a\n"
    versions = subprocess.run([go_bin, "list", "-m", "-versions", *MODULES], cwd=mod, env=env, capture_output=True,
                              text=True, timeout=600)
    assert versions.returncode == 0, versions.stderr
    assert all(line.endswith(" v1.0.0 v1.1.0") for line in versions.stdout.splitlines())
    dl = subprocess.run([go_bin, "mod", "download", "-json", *[m + "@v1.0.0" for m in MODULES]], cwd=mod, env=env,
                        capture_output=True, text=True, timeout=600)
    assert dl.returncode == 0, dl.stderr
    decoder, text, got = json.JSONDecoder(), dl.stdout.strip(), []
    while text:
        obj, end = decoder.raw_decode(text)
        got.append(f"{obj['Path']} {obj['Version']} {obj['Sum']}")
        got.append(f"{obj['Path']} {obj['Version']}/go.mod {obj['GoModSum']}")
        text = text[end:].strip()
    for line in got:
        assert line + "\n" in proxy_sums(line.rsplit(" ", 1)[0]), line
    assert open(mod / "go.sum").read() == proxy_sums("example.com/thirdparty/uuidlite v1.1.0")
