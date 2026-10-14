"""The shared vocabularies of go_model (spec §5.7, §6.1-6.2): pinned, because resolution,
surfaces and evaluate all import them."""
from __future__ import annotations

from graph_generator import go_model


def test_vocabularies_are_pinned():
    assert go_model.STATUSES == ("resolved", "external", "builtin", "ambiguous", "dynamic",
                                 "unresolved")
    assert go_model.VIA == ("types", "convention:funclit", "convention:func-alias",
                            "convention:linkname", "convention:range", "none")
    assert go_model.POSSIBLY_REASONS == ("implementation", "variant")
    assert go_model.UNRESOLVED_REASONS == (
        "missing-module", "incomplete-type", "excluded-by-build", "toolchain", "no-declaration",
        "stale", "syntax-error", "unscanned", "outside-repo", "no-module", "load-error")
    assert go_model.SITE_KINDS == ("call", "go", "defer", "range")
    assert go_model.FILE_ORIGINS == ("package", "command", "test", "xtest")
    assert go_model.CLASS_KINDS == ("struct", "interface", "named", "alias", "package")
    assert go_model.MEMBER_KINDS == ("func", "method", "interface_method", "init", "closure",
                                     "var_init")


def test_discovery_helpers():
    g = go_model.Group(id="ws0", kind="workspace", dir="/o/ws0", modules=["a"], gowork="/o/ws0/go.work",
                       modfile=None, vendor=False, degraded=False, status="ok", pattern="work")
    u = go_model.GoUnit(key="a", root="", go="1.26", toolchain=None, requires=(), replaces=(),
                        vendor=False, in_repo_deps=())
    d = go_model.Discovery(repo="r", repo_root="/r", units=[u], unit_of_file={}, groups=[g], builds=[],
                           toolchain="go1.27.2", mode="offline", module_source="none", peers=[],
                           skipped={}, warnings=[])
    assert d.unit("a") is u and d.group_of("a") is g and d.group_of("b") is None
    assert g.missing_modules == [] and g.warnings == [] and g.uses == [] and g.source is None
