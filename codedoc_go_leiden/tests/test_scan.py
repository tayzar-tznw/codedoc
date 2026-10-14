"""Phase 1: the scan through go_units.scan_go_files (WP-D owns the skip rules),
hashes (spec §2.6), the directory tree, and discovery through go_units.discover."""

import hashlib
import json
import os
import sys
import types

import pytest

from graph_generator import config, pipeline
from graph_generator.pipeline import PipelineData, phase1_rescan, phase1_scan, scan_source_files, source_hash
from tests.conftest import SAMPLE_GO, SAMPLE_TREE, write_tree
from tests.pipeline_fakes import fake_scan_go_files


@pytest.fixture
def repo(tmp_path):
    root = write_tree(tmp_path / "repo", {
        **SAMPLE_TREE, "libs/billing/testdata/golden.go": "package x\n",
        "vendor/example.com/dep/dep.go": "package dep\n", "_tools/gen.go": "package main\n",
        ".git/hooks/x.go": "package x\n", "README.md": "# r\n",
        "会計/計算.go": "package 会計\n", "my dir/#x%.go": "package x\n"})
    return root


@pytest.fixture
def scanner(monkeypatch):
    seen = []

    def scan(root):
        seen.append(root)
        return fake_scan_go_files(root)
    monkeypatch.setattr(pipeline, "_scan_go_files", scan)
    monkeypatch.setattr(pipeline, "_discover", lambda data: None)
    return seen


EXPECTED = sorted(SAMPLE_GO + ["会計/計算.go", "my dir/#x%.go"])


def test_scan_uses_scan_go_files_and_hashes_every_file(repo, out_dir, scanner, capsys):
    data = PipelineData(target_dir=repo)
    phase1_scan(data)
    assert scanner == [os.path.realpath(repo)]
    assert sorted(data.file_hashes) == EXPECTED
    assert sorted(os.path.relpath(fp, data.target_dir) for fp in data.file_list) == EXPECTED
    assert data.file_hashes["libs/kernel/doc.go"] == hashlib.sha256(
        SAMPLE_TREE["libs/kernel/doc.go"].encode()).hexdigest()
    assert data.repo == "repo"
    assert data.skipped == {"dot": 1, "testdata": 1, "underscore": 1, "vendor": 1}
    out = capsys.readouterr().out
    assert "[Phase 1] Scanned 7 files in" in out
    assert "(skipped: dot 1, testdata 1, underscore 1, vendor 1)" in out
    assert "phase1_scan" in data.timings


def test_dir_tree_is_bottom_up(repo, out_dir, scanner):
    data = PipelineData(target_dir=repo)
    phase1_scan(data)
    rels = [pipeline._relpath(data, d) for d in data.dir_queue]
    assert rels[-1] == "" and set(rels) == {"", "cmd", "cmd/reception", "libs", "libs/billing",
                                            "libs/kernel", "会計", "my dir"}
    assert rels.index("libs/billing") < rels.index("libs")
    billing = os.path.join(data.target_dir, "libs", "billing")
    assert sorted(os.path.basename(f) for f in data.dir_tree[billing]["files"]) == [
        "doc.go", "fee.go", "service.go"]


def test_root_reached_through_symlink(repo, tmp_path, out_dir, scanner):
    link = tmp_path / "rootlink"
    os.symlink(repo, link)
    data = PipelineData(target_dir=str(link))
    phase1_scan(data)
    assert data.target_dir == os.path.realpath(repo)
    assert sorted(data.file_hashes) == EXPECTED
    assert data.repo == "rootlink"  # same default name as the CLI's --repo-name


def test_rescan_does_not_discover(repo, out_dir, monkeypatch):
    monkeypatch.setattr(pipeline, "_scan_go_files", fake_scan_go_files)
    monkeypatch.setattr(pipeline, "_discover", lambda data: pytest.fail("no discovery in --wiki"))
    data = PipelineData(target_dir=repo, repo="iryo")
    phase1_rescan(data)
    assert sorted(data.file_hashes) == EXPECTED and "phase1_rescan" in data.timings


def test_source_hash_is_over_raw_bytes(tmp_path):
    raw = "package 会計\r\n﻿var x = 1\r\n".encode("utf-8")
    path = tmp_path / "bom.go"
    path.write_bytes(raw)
    assert source_hash(str(path)) == hashlib.sha256(raw).hexdigest()


def test_non_utf8_and_unreadable_files_are_skipped_with_a_warning(repo, out_dir, scanner, capsys):
    bad = os.path.join(os.fsencode(str(repo)), b"libs", b"caf\xe9.go")  # Latin-1 byte in the name
    with open(bad, "wb") as f:
        f.write(b"package billing\n")
    locked = os.path.join(repo, "libs", "billing", "locked.go")
    with open(locked, "w", encoding="utf-8") as f:
        f.write("package billing\n")
    os.chmod(locked, 0)
    data = PipelineData(target_dir=repo)
    try:
        phase1_scan(data)
    finally:
        os.chmod(locked, 0o644)
    out = capsys.readouterr().out
    root_user = os.geteuid() == 0  # root can still open a mode-000 file
    assert sorted(data.file_hashes) == sorted(EXPECTED + (["libs/billing/locked.go"] if root_user else []))
    assert "caf\\udce9.go" in out and "the path is not valid UTF-8" in out
    if not root_user:
        assert "skipped libs/billing/locked.go" in out
        assert data.skipped["unreadable"] == 2


def test_scan_source_files_for_a_peer_root(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_scan_go_files", fake_scan_go_files)
    link = tmp_path / "peerlink"
    os.symlink(repo, link)
    assert scan_source_files(repo) == EXPECTED
    assert scan_source_files(str(link)) == EXPECTED


def test_scan_go_files_is_the_wp_d_hand_off(repo, monkeypatch):
    monkeypatch.setitem(sys.modules, "graph_generator.go_units", None)
    with pytest.raises(NotImplementedError, match=r"go_units\.scan_go_files .*\(WP-D\)"):
        pipeline._scan_go_files(repo)


# ── discovery (go_units.discover, WP-D) ──────────────────────────

class _FakeDiscovery:
    def __init__(self, repo, groups, warnings):
        self.repo, self.groups, self.warnings = repo, groups, warnings
        self.units = [types.SimpleNamespace(key="example.com/iryo")]
        self.builds = [types.SimpleNamespace(id="linux-amd64")]
        self.toolchain, self.mode, self.module_source = "go1.27.2", "offline", "file-proxy"


@pytest.fixture
def fake_go_units(monkeypatch):
    """WP-D's real modules with discover, write_units_json, scan_go_files and
    find_toolchain replaced (builds_from_config stays real)."""
    from graph_generator import go_toolchain, go_units
    calls = {}
    toolchain = types.SimpleNamespace(go="/go/bin/go", version="go1.27.2", goroot="/go")
    group = types.SimpleNamespace(id="ws0", kind="workspace", status="ok", degraded=False,
                                  missing_modules=["example.com/x@v1.0.0", "example.com/y@v1.2.0"])

    def discover(repo_root, repo, *, builds, peers, mode, output_dir, toolchain):
        calls["discover"] = dict(repo_root=repo_root, repo=repo, builds=builds, peers=peers,
                                 mode=mode, output_dir=output_dir, toolchain=toolchain)
        return _FakeDiscovery(repo, [group], ["group ws0: go.work use ./gone is missing"])

    def write_units_json(discovery, path):
        calls["units_json"] = path
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "repo": discovery.repo}, f)

    monkeypatch.setattr(go_units, "discover", discover)
    monkeypatch.setattr(go_units, "write_units_json", write_units_json)
    monkeypatch.setattr(go_units, "scan_go_files", fake_scan_go_files)
    monkeypatch.setattr(go_toolchain, "find_toolchain", lambda: calls.setdefault("found", toolchain))
    return calls


def test_phase1_discovers_and_writes_units_json(repo, out_dir, fake_go_units, monkeypatch, capsys):
    monkeypatch.setattr(config, "GO_EXTRA_BUILDS", "windows/amd64")
    peer = types.SimpleNamespace(name="shared")
    data = PipelineData(target_dir=repo, repo="iryo", mode="offline", peers=[peer])
    phase1_scan(data)
    call = fake_go_units["discover"]
    assert (call["repo_root"], call["repo"], call["mode"]) == (os.path.realpath(repo), "iryo", "offline")
    assert call["output_dir"] == out_dir and call["peers"] == [peer]
    assert call["toolchain"] is fake_go_units["found"] is data.toolchain
    assert [(b.id, b.primary, b.cgo) for b in call["builds"]] == [
        ("linux-amd64", True, False), ("windows-amd64", False, False)]
    assert fake_go_units["units_json"] == os.path.join(out_dir, "units.json")
    assert data.discovery.repo == "iryo"
    out = capsys.readouterr().out
    assert "[Phase 1] warning: group ws0: go.work use ./gone is missing" in out
    assert "2 required modules are missing offline" in out and "--download-modules" in out
    assert "Discovered 1 modules; groups: ws0 workspace ok; builds: linux-amd64" in out


def test_phase1_reuses_the_preflight_toolchain(repo, out_dir, fake_go_units):
    toolchain = types.SimpleNamespace(go="/other/go", version="go1.27.2", goroot="/other")
    data = PipelineData(target_dir=repo, repo="iryo", toolchain=toolchain)
    phase1_scan(data)
    assert fake_go_units["discover"]["toolchain"] is toolchain and "found" not in fake_go_units


def test_discovery_and_scan_get_the_base_output_dir(repo, tmp_path, fake_go_units, monkeypatch):
    """During a repo's run OUTPUT_DIR is <base>/repos/<repo>; discover names the
    control dirs from <base>, and the scan prunes the whole output tree."""
    base = tmp_path / "out"
    monkeypatch.setattr(config, "OUTPUT_DIR", str(base / "repos" / "iryo"))
    seen = {}

    def scan(root, exclude=()):
        seen["exclude"] = exclude
        return fake_scan_go_files(root)
    from graph_generator import go_units
    monkeypatch.setattr(go_units, "scan_go_files", scan)
    data = PipelineData(target_dir=repo, repo="iryo")
    phase1_scan(data)
    assert seen["exclude"] == (str(base),)
    assert fake_go_units["discover"]["output_dir"] == str(base)
    assert fake_go_units["units_json"] == os.path.join(str(base), "repos", "iryo", "units.json")
