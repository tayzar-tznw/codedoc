"""discover() against F2's hand-written ground truth (test_codes/go_layouts/ground_truth/
expected_units.json; each top-level directory is one repo). Offline mode is evaluate's
offline mode (empty module cache, no user cache); env mode is download mode against
the committed proxy test_codes/_proxy."""
from __future__ import annotations

import json
import os

import pytest

from graph_generator import config, go_units
from graph_generator.go_model import BuildConfig, Toolchain

LAYOUTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_codes",
                       "go_layouts")
EXPECTED = os.path.join(LAYOUTS, "ground_truth", "expected_units.json")
PROXY = os.path.join(os.path.dirname(LAYOUTS), "_proxy")
TC = Toolchain(go="/nonexistent/go", version="go1.27.2", goroot="/nonexistent")
BUILD = BuildConfig("linux-amd64", "linux", "amd64", (), False, True)

pytestmark = pytest.mark.skipif(not os.path.isfile(EXPECTED), reason="F2 ground truth not present")


def _expected() -> dict:
    with open(EXPECTED, encoding="utf-8") as f:
        return json.load(f)


def _key(disc, group) -> str:
    rel = os.path.relpath(os.path.dirname(group.source), disc.repo_root)
    return f"{group.kind}:{rel}"


def _discover(name, tmp_path, monkeypatch, mode):
    empty = tmp_path / "no-user-cache"
    os.makedirs(empty, exist_ok=True)
    monkeypatch.setattr(config, "GO_USER_MODCACHE", str(empty), raising=False)
    monkeypatch.setattr(config, "GO_PROXY", "file://" + PROXY, raising=False)
    return go_units.discover(os.path.join(LAYOUTS, name), name, builds=[BUILD], peers=[], mode=mode,
                             output_dir=str(tmp_path / f"out-{mode}"), toolchain=TC)


def _diff(name, disc, want, mode_want) -> list[str]:
    out = []
    root = disc.repo_root
    keys = {g.id: _key(disc, g) for g in disc.groups}
    group_of = {m: keys[g.id] for g in disc.groups for m in g.modules}
    got_units = {u.key: u for u in disc.units}
    if sorted(got_units) != sorted(want["units"]):
        out.append(f"{name}: units {sorted(got_units)} != {sorted(want['units'])}")
    for key, wu in want["units"].items():
        u = got_units.get(key)
        if u is None:
            continue
        got = {"root": u.root, "go": u.go, "toolchain": u.toolchain, "vendor": u.vendor,
               "group": group_of.get(key), "requires": [list(r) for r in u.requires],
               "replaces": [{"old": r.old, "old_version": r.old_version,
                             "new": os.path.relpath(r.new, root) if r.local else r.new,
                             "new_version": r.new_version, "local": r.local} for r in u.replaces],
               "in_repo_deps": list(u.in_repo_deps)}
        for field, value in got.items():
            if field in wu and wu[field] != value:
                out.append(f"{name}: unit {key} {field}: got {value!r}, want {wu[field]!r}")
    got_groups = {keys[g.id]: g for g in disc.groups}
    if sorted(got_groups) != sorted(want["groups"]):
        out.append(f"{name}: groups {sorted(got_groups)} != {sorted(want['groups'])}")
    for key, wg in want["groups"].items():
        g = got_groups.get(key)
        if g is None:
            continue
        for field in ("kind", "modules", "vendor", "degraded", "status", "pattern"):
            if wg[field] != getattr(g, field):
                out.append(f"{name}: group {key} {field}: got {getattr(g, field)!r}, want {wg[field]!r}")
        for text in wg.get("warnings_contain", []):
            if not any(text in w for w in g.warnings):
                out.append(f"{name}: group {key} has no warning containing {text!r}: {g.warnings}")
    for rel, unit in want["files"].items():
        if disc.unit_of_file.get(rel) != unit:
            out.append(f"{name}: file {rel}: got {disc.unit_of_file.get(rel)!r}, want {unit!r}")
    files, skipped = go_units.scan_go_files(root)
    if len(files) != want["scanned_files"]:
        out.append(f"{name}: scanned {len(files)} files, want {want['scanned_files']}")
    leaked = sorted(set(want["not_scanned"]) & set(files))
    if leaked:
        out.append(f"{name}: scanned {leaked}")
    if skipped != want["skipped"] or disc.skipped != want["skipped"]:
        out.append(f"{name}: skipped {disc.skipped}, want {want['skipped']}")
    for text in want.get("warnings_contain", []):
        if not any(text in w for w in disc.warnings):
            out.append(f"{name}: no repo warning containing {text!r}: {disc.warnings}")
    if disc.module_source != mode_want["module_source"]:
        out.append(f"{name} ({disc.mode}): module_source {disc.module_source!r}, "
                   f"want {mode_want['module_source']!r}")
    got_missing = {keys[g.id]: g.missing_modules for g in disc.groups if g.missing_modules}
    if got_missing != mode_want["missing_modules"]:
        out.append(f"{name} ({disc.mode}): missing_modules {got_missing}, "
                   f"want {mode_want['missing_modules']}")
    return out


@pytest.mark.parametrize("mode,mode_key", [("offline", "offline"), ("download", "env")])
def test_discovery_matches_f2_ground_truth(tmp_path, monkeypatch, mode, mode_key):
    expected = _expected()
    assert expected["toolchain"] == TC.version
    problems = []
    for name, want in sorted(expected["repos"].items()):
        disc = _discover(name, tmp_path / name, monkeypatch, mode)
        problems += _diff(name, disc, want, want["modes"][mode_key])
    assert problems == []
