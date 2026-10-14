"""go_classify (spec §5.3-5.5, master plan C5): location classification,
exact name-range mapping (R2) and the class -> status/via table."""

from __future__ import annotations

import pytest

from graph_generator import go_classify, go_gates
from graph_generator.go_classify import ClassifyContext, DeclIndex, Locator
from graph_generator.go_model import (POSSIBLY_REASONS, STATUSES, UNRESOLVED_REASONS, VIA, GoUnit,
                                      Peer)

REPO = "/repo"
GOROOT = "/goroot"
MODCACHE = "/out/go/modcache"
GOCACHE = "/out/go/gocache"
BILLING = "example.com/iryo/billing"


def fn(qualname, line, col, kind="func", name=None, sites=()):
    name = name or qualname.rsplit(".", 1)[-1]
    width = 4 if kind == "closure" else len(name)
    return {"name": name, "qualname": qualname, "member_kind": kind,
            "name_range": [line, col, col + width], "start_line": line, "end_line": line + 2,
            "sites": list(sites)}


def ty(qualname, line, col, kind="struct", methods=()):
    return {"name": qualname, "qualname": qualname, "kind": kind,
            "name_range": [line, col, col + len(qualname)], "start_line": line, "end_line": line + 3,
            "methods": [{"name": m.rsplit(".", 1)[-1], "qualname": m, "name_range": [ln, c, c + len(m.rsplit(".", 1)[-1])]}
                        for m, ln, c in methods]}


def rec(module, package, clause, types=(), funcs=(), included=True):
    return {"sha256": "x", "module": module, "package": package, "package_name": clause,
            "origin": "package", "build_included": included, "syntax_errors": [],
            "types": [{"name": "<package>", "qualname": "<package>", "kind": "package",
                       "name_range": [1, 9, 16], "methods": []}, *types],
            "funcs": list(funcs), "imports": []}


ENTITIES = {
    "billing/fee.go": rec(BILLING, BILLING, "billing",
                          types=[ty("FeeCalculator", 5, 6),
                                 ty("Totaler", 10, 6, "interface", methods=[("Totaler.Total", 11, 2)])],
                          funcs=[fn("FeeCalculator.Calculate", 14, 25, "method"), fn("Round", 20, 6),
                                 fn("Round.func1", 21, 9, "closure")]),
    "billing/impl.go": rec(BILLING, BILLING, "billing",
                           types=[ty("Cash", 3, 6)], funcs=[fn("Cash.Total", 8, 15, "method")]),
    "billing/impl_test.go": rec(BILLING, BILLING, "billing",
                                types=[ty("Fake", 3, 6)], funcs=[fn("Fake.Total", 6, 15, "method")]),
    "billing/twin_linux.go": rec(BILLING, BILLING, "billing", funcs=[fn("Native", 3, 6)]),
    "billing/twin_windows.go": rec(BILLING, BILLING, "billing", funcs=[fn("Native", 3, 6)], included=False),
    "billing/ext_test.go": rec(BILLING, BILLING + "_test", "billing_test", funcs=[fn("Native", 3, 6)]),
}

PEER = Peer(name="shared", root="/peers/shared",
            units=(GoUnit(key="example.com/iryo/shared", root="", go="1.26", toolchain=None, requires=(),
                          replaces=(), vendor=False, in_repo_deps=()),))


@pytest.fixture
def ctx():
    scanned = {f"{REPO}/{rel}": rel for rel in ENTITIES}
    locator = Locator(repo_root=REPO, scanned=scanned, goroot=GOROOT, modcaches=[MODCACHE],
                      gocache=GOCACHE, peers=[PEER])
    return ClassifyContext(decls=DeclIndex(ENTITIES), locator=locator)


def target(path, line, col, name, obj="func", recv="", pkg=BILLING, module="", version="", qualname=None):
    t = {"path": path, "line": line, "col": col, "name": name, "pkg": pkg, "module": module,
         "version": version, "obj": obj, "recv": recv}
    if qualname is not None:
        t["qualname"] = qualname
    return t


def site(cls, tgt=None, *, site_kind="call", **extra):
    r = {"kind": "site", "path": f"{REPO}/billing/fee.go", "offset": 100, "line": 18, "col": 11,
         "end_col": 20, "name": "x", "site_kind": site_kind, "shape": "selector", "class": cls,
         "target": tgt, "promoted_depth": 0, "embed_invalid": False, "stub": False, "pkg_errors": False,
         "value_kind": None, "alias": None, "linkname": None, "asm": False, "iface": None,
         "variants": [], "build": "linux-amd64"}
    r.update(extra)
    return r


def classify(rec_, ctx):
    return go_gates.apply_gates(go_classify.classify_site(rec_, ctx), go_gates.SiteContext(helper=rec_, ctx=ctx))


def in_repo(relpath, qualname, line, kind="method", package=BILLING):
    return {"unit": BILLING, "package": package, "relpath": relpath, "qualname": qualname,
            "line": line, "kind": kind}


# ---------------------------------------------------------------- locations (§5.3)

@pytest.mark.parametrize("path,expect", [
    (f"{GOROOT}/src/builtin/builtin.go", ("builtin", None, None, "")),
    (f"{GOROOT}/src/unsafe/unsafe.go", ("builtin", None, None, "")),
    (f"{GOROOT}/src/fmt/print.go", ("std", None, "std", "")),
    ("/peers/shared/billing/x.go", ("peer", "billing/x.go", "example.com/iryo/shared", "")),
    (f"{MODCACHE}/github.com/!burnt!sushi/toml@v1.2.0/decode.go", ("modcache", None, "github.com/BurntSushi/toml", "v1.2.0")),
    (f"{MODCACHE}/golang.org/x/text@v0.3.0/unicode/norm/x.go", ("modcache", None, "golang.org/x/text", "v0.3.0")),
    (f"{REPO}/vendor/github.com/x/y/z.go", ("vendor", None, None, "")),
    (f"{REPO}/billing/fee.go", ("repo", "billing/fee.go", None, "")),
    (f"{REPO}/billing/testdata/x.go", ("unscanned", None, None, "")),
    (f"{GOCACHE}/a1/_cgo_gotypes.go", ("cgo", None, "C", "")),
    ("/elsewhere/x.go", ("outside", None, None, "")),
])
def test_locations(ctx, path, expect):
    loc = ctx.locator.locate(path)
    assert (loc.kind, loc.relpath, loc.module, loc.version) == expect
    if loc.kind == "peer":
        assert loc.peer == "shared"


# ---------------------------------------------------------------- the declaration index (R2)

def test_decl_index_maps_exact_name_ranges_only(ctx):
    d = ctx.decls
    assert d.at("billing/fee.go", 14, 25, "method").qualname == "FeeCalculator.Calculate"
    assert d.at("billing/fee.go", 14, 26, "method") is None            # no enclosing fallback
    assert d.at("billing/fee.go", 5, 6, "method") is None              # a class, not a method
    assert d.at("billing/fee.go", 5, 6, "class").qualname == "FeeCalculator"
    assert d.at("billing/fee.go", 11, 2, "method").member_kind == "interface_method"
    assert d.at("billing/fee.go", 21, 9, "method").qualname == "Round.func1"
    assert d.at("billing/fee.go", 1, 9, "class") is None               # <package> is never a target


def test_embeds_accept_named_owners(ctx):
    """`type T2 T1` (kind named) has T1's embedded fields."""
    from graph_generator import resolution
    ents = {"x/t.go": rec(BILLING, BILLING, "x", types=[ty("T1", 3, 6), ty("T2", 7, 6, "named")])}
    from graph_generator.go_classify import ClassifyContext as C, DeclIndex as D, Locator as L
    c = C(decls=D(ents), locator=L(repo_root=REPO, scanned={f"{REPO}/x/t.go": "x/t.go"}, goroot=GOROOT))
    t2 = {"path": f"{REPO}/x/t.go", "line": 7, "col": 6, "name": "T2", "pkg": BILLING}
    t1 = {"path": f"{REPO}/x/t.go", "line": 3, "col": 6, "name": "T1", "pkg": BILLING, "obj": "type"}
    e = resolution._embed_record({"type": t2, "embedded": t1, "pointer": False}, c)
    assert e["type"] == {"relpath": "x/t.go", "qualname": "T2"} and e["embedded"]["qualname"] == "T1"


def test_twins_are_same_directory_and_clause(ctx):
    linux = ctx.decls.at("billing/twin_linux.go", 3, 6, "method")
    assert [n.relpath for n in ctx.decls.twins(linux)] == ["billing/twin_windows.go"]
    assert ctx.decls.twins(ctx.decls.at("billing/fee.go", 20, 6, "method")) == []


# ---------------------------------------------------------------- the C5 table

def test_builtin(ctx):
    r = classify(site("builtin"), ctx)
    assert (r["status"], r["via"], r["target"], r["external"]) == ("builtin", "none", None, None)


def test_func_and_method_in_repo(ctx):
    r = classify(site("func", target(f"{REPO}/billing/fee.go", 20, 6, "Round", module=BILLING)), ctx)
    assert (r["status"], r["via"]) == ("resolved", "types")
    assert r["target"] == in_repo("billing/fee.go", "Round", 20)
    r = classify(site("method", target(f"{REPO}/billing/fee.go", 14, 25, "Calculate", "method",
                                       "*FeeCalculator", module=BILLING)), ctx)
    assert r["target"] == in_repo("billing/fee.go", "FeeCalculator.Calculate", 14)


def test_asm_declarations_resolve_to_the_bodyless_declaration(ctx):
    r = classify(site("func", target(f"{REPO}/billing/fee.go", 20, 6, "Round"), asm=True), ctx)
    assert (r["status"], r["via"], r["target"]["qualname"]) == ("resolved", "types", "Round")


@pytest.mark.parametrize("tgt,external", [
    (target(f"{GOROOT}/src/fmt/print.go", 270, 6, "Println", pkg="fmt"),
     {"module": "std", "version": "", "package": "fmt", "qualname": "Println", "peer": False}),
    (target(f"{MODCACHE}/github.com/!burnt!sushi/toml@v1.2.0/decode.go", 9, 20, "Decode", "method",
            "*Decoder", pkg="github.com/BurntSushi/toml", module="github.com/BurntSushi/toml", version="v1.2.0"),
     {"module": "github.com/BurntSushi/toml", "version": "v1.2.0", "package": "github.com/BurntSushi/toml",
      "qualname": "Decoder.Decode", "peer": False}),
    (target(f"{REPO}/vendor/github.com/x/y/z.go", 3, 6, "Z", pkg="github.com/x/y", module="github.com/x/y",
            version="v0.1.0"),
     {"module": "github.com/x/y", "version": "v0.1.0", "package": "github.com/x/y", "qualname": "Z", "peer": False}),
    (target(f"{GOCACHE}/a1/_cgo_gotypes.go", 40, 6, "_Cfunc_puts", pkg="example.com/iryo/billing"),
     {"module": "C", "version": "", "package": "example.com/iryo/billing", "qualname": "_Cfunc_puts", "peer": False}),
])
def test_external_targets(ctx, tgt, external):
    r = classify(site("func" if not tgt["recv"] else "method", tgt), ctx)
    assert (r["status"], r["via"], r["target"], r["external"]) == ("external", "none", None, external)


def test_peer_targets_keep_repo_relpath_and_qualname(ctx):
    r = classify(site("method", target("/peers/shared/money/yen.go", 7, 15, "Add", "method", "Yen",
                                       pkg="example.com/iryo/shared/money", module="example.com/iryo/shared")), ctx)
    assert (r["status"], r["via"]) == ("external", "none")
    assert r["external"] == {"module": "example.com/iryo/shared", "version": "",
                             "package": "example.com/iryo/shared/money", "qualname": "Yen.Add", "peer": True}
    assert r["target"] == {"repo": "shared", "unit": "example.com/iryo/shared",
                           "package": "example.com/iryo/shared/money", "relpath": "money/yen.go",
                           "qualname": "Yen.Add", "line": 7, "kind": "method"}


def test_helper_qualname_wins_over_derivation(ctx):
    r = classify(site("method", target(f"{GOROOT}/src/x/y.go", 3, 6, "M", "method", "*T", pkg="x",
                                       qualname="T.M")), ctx)
    assert r["external"]["qualname"] == "T.M"


@pytest.mark.parametrize("path,reason", [
    (f"{REPO}/billing/testdata/x.go", "unscanned"),
    ("/elsewhere/x.go", "outside-repo"),
    (f"{REPO}/billing/fee.go", "no-declaration"),       # G7: no node at exactly this name range
])
def test_unmappable_targets_are_unresolved(ctx, path, reason):
    r = classify(site("func", target(path, 99, 3, "Gone")), ctx)
    assert (r["status"], r["reason"], r["target"]) == ("unresolved", reason, None)


def iface(recv_type, *cands, open_world=False, suppressed=False):
    return {"recv_type": recv_type, "open_world": open_world, "suppressed": suppressed,
            "candidates": [{"path": p, "line": ln, "col": c, "name": "Total", "pkg": BILLING, "type": t,
                            "pointer": False, "test_only": to, "via_embedded_iface": ve}
                           for p, ln, c, t, to, ve in cands]}


CASH = (f"{REPO}/billing/impl.go", 8, 15, "Cash", False, False)
FAKE = (f"{REPO}/billing/impl_test.go", 6, 15, "Fake", True, False)
TOTAL = target(f"{REPO}/billing/fee.go", 11, 2, "Total", "iface_method", "Totaler", module=BILLING)


def test_iface_in_repo_resolves_to_the_interface_method_with_candidates(ctx):
    r = classify(site("iface", TOTAL, iface=iface(f"{BILLING}.Totaler", CASH, FAKE, CASH, open_world=True)), ctx)
    assert (r["status"], r["via"], r["open_world"]) == ("resolved", "types", True)
    assert r["target"] == in_repo("billing/fee.go", "Totaler.Total", 11)
    assert r["candidates"] == [
        {**in_repo("billing/impl.go", "Cash.Total", 8), "test_only": False, "via_embedded_iface": False},
        {**in_repo("billing/impl_test.go", "Fake.Total", 6), "test_only": True, "via_embedded_iface": False}]


def test_typeparam_is_treated_like_iface(ctx):
    r = classify(site("typeparam", TOTAL, iface=iface(f"{BILLING}.Totaler", CASH)), ctx)
    assert (r["status"], r["target"]["qualname"], len(r["candidates"])) == ("resolved", "Totaler.Total", 1)


def test_iface_outside_the_repo_is_external_with_in_repo_candidates(ctx):
    write = target(f"{GOROOT}/src/io/io.go", 99, 2, "Write", "iface_method", "Writer", pkg="io")
    r = classify(site("iface", write, iface=iface("io.Writer", CASH, open_world=True)), ctx)
    assert (r["status"], r["via"], r["target"]) == ("external", "none", None)
    assert r["external"]["qualname"] == "Writer.Write" and r["external"]["module"] == "std"
    assert [c["qualname"] for c in r["candidates"]] == ["Cash.Total"] and r["open_world"]


def test_a_dropped_external_embedded_candidate_marks_the_set_partial(ctx):
    """W satisfies the interface through its embedded io.Writer (probe p9d):
    that candidate is outside the repo and has no node, so it is left out and
    the record says its candidate set is not closed. The rows do not change."""
    writer = (f"{GOROOT}/src/io/io.go", 99, 2, "Writer", False, True)
    r = classify(site("iface", TOTAL, iface=iface(f"{BILLING}.Totaler", CASH, writer)), ctx)
    assert r["status"] == "resolved" and [c["qualname"] for c in r["candidates"]] == ["Cash.Total"]
    assert r["candidates_partial"] == "via_embedded_external"
    anon = target(f"{REPO}/billing/fee.go", 30, 22, "Total", "iface_method", "")
    r = classify(site("iface", anon, iface=iface("", CASH, writer)), ctx)
    assert (r["status"], r["candidates_partial"]) == ("ambiguous", "via_embedded_external")
    # A candidate outside the repo that is not reached through an embedded
    # interface, or an in-repo one that is, leaves the set closed.
    elsewhere = (f"{GOROOT}/src/bytes/buffer.go", 9, 2, "Buffer", False, False)
    inrepo = (CASH[0], CASH[1], CASH[2], "Cash", False, True)
    for cands in ((CASH, elsewhere), (inrepo,)):
        r = classify(site("iface", TOTAL, iface=iface(f"{BILLING}.Totaler", *cands)), ctx)
        assert "candidates_partial" not in r


def test_partial_candidate_sets_name_their_reason(ctx):
    """R50: one field, two reasons: a suppressed search (G4), first, then an
    external candidate through an embedded interface. A complete set has no key."""
    writer = (f"{GOROOT}/src/io/io.go", 99, 2, "Writer", False, True)

    def partial(info):
        return classify(site("iface", TOTAL, iface=info), ctx).get("candidates_partial")
    assert partial({**iface(f"{BILLING}.Totaler", suppressed=True), "open_world": True}) == "suppressed"
    assert partial({**iface(f"{BILLING}.Totaler", writer, suppressed=True), "open_world": True}) == "suppressed"
    assert partial(iface(f"{BILLING}.Totaler", CASH, writer)) == "via_embedded_external"
    assert partial(iface(f"{BILLING}.Totaler", CASH)) is None


def test_g4_marks_the_record_suppressed_and_refusals_drop_the_mark():
    from graph_generator import go_gates
    rec = {"status": "resolved", "candidates": [{"qualname": "Cash.Total"}],
           "candidates_partial": "via_embedded_external"}
    assert "candidates_partial" not in go_gates.unresolved(rec, "stale")
    sc = go_gates.SiteContext(helper={"iface": {"suppressed": True}})
    out = go_gates._g4_iface_offline(dict(rec), sc)
    assert out["candidates"] == [] and out["candidates_partial"] == "suppressed"
    dyn = go_gates._g4_iface_offline({"status": "dynamic", "candidates": []}, sc)
    assert "candidates_partial" not in dyn                 # no candidate edge to mark


def test_iface_suppressed_keeps_the_static_target_without_candidates(ctx):
    r = classify(site("iface", TOTAL, iface=iface(f"{BILLING}.Totaler", suppressed=True)), ctx)
    assert (r["status"], r["candidates"]) == ("resolved", [])


@pytest.mark.parametrize("cands,status", [((CASH,), "ambiguous"), ((), "dynamic")])
def test_anonymous_interfaces_never_edge(ctx, cands, status):
    anon = target(f"{REPO}/billing/fee.go", 30, 22, "Total", "iface_method", "")
    r = classify(site("iface", anon, iface=iface("", *cands)), ctx)
    assert (r["status"], r["via"], r["target"]) == (status, "types", None)
    if status == "ambiguous":
        assert r["reason"] == "implementation" and r["candidates"][0]["qualname"] == "Cash.Total"


def test_values(ctx):
    r = classify(site("value", value_kind="param"), ctx)
    assert (r["status"], r["via"], r["target"]) == ("dynamic", "none", None)
    alias = target(f"{REPO}/billing/fee.go", 20, 6, "Round")
    r = classify(site("value", value_kind="local", alias=alias), ctx)
    assert (r["status"], r["via"], r["target"]["qualname"]) == ("resolved", "convention:func-alias", "Round")
    ext = target(f"{GOROOT}/src/strings/strings.go", 9, 6, "ToUpper", pkg="strings")
    r = classify(site("value", value_kind="local", alias=ext), ctx)
    assert (r["status"], r["via"], r["external"]["qualname"]) == ("external", "convention:func-alias", "ToUpper")


def test_a_method_value_of_an_interface_behaves_like_an_iface_site(ctx):
    r = classify(site("value", value_kind="local", alias=TOTAL, iface=iface(f"{BILLING}.Totaler", CASH)), ctx)
    assert (r["status"], r["via"], r["target"]["qualname"]) == ("resolved", "convention:func-alias", "Totaler.Total")
    assert [c["qualname"] for c in r["candidates"]] == ["Cash.Total"]


def test_funclit(ctx):
    closure = target(f"{REPO}/billing/fee.go", 21, 9, "func", "closure")
    r = classify(site("funclit", closure, shape="funclit"), ctx)
    assert (r["status"], r["via"], r["target"]["qualname"]) == ("resolved", "convention:funclit", "Round.func1")


def test_linkname(ctx):
    r = classify(site("linkname", target(f"{REPO}/billing/fee.go", 20, 6, "Round"),
                      linkname=f"{BILLING}.Round"), ctx)
    assert (r["status"], r["via"], r["target"]["qualname"]) == ("resolved", "convention:linkname", "Round")
    r = classify(site("linkname", target(f"{GOROOT}/src/runtime/time.go", 3, 6, "nanotime", pkg="runtime"),
                      linkname="runtime.nanotime"), ctx)
    assert (r["status"], r["via"], r["external"]["module"]) == ("external", "convention:linkname", "std")


@pytest.mark.parametrize("symbol,external", [
    ("runtime.nanotime", {"module": "std", "version": "", "package": "runtime", "qualname": "nanotime", "peer": False}),
    ("internal/poll.(*FD).Close", {"module": "std", "version": "", "package": "internal/poll", "qualname": "FD.Close", "peer": False}),
    ("example.com/x/y.T.m", {"module": "", "version": "", "package": "example.com/x/y", "qualname": "T.m", "peer": False}),
])
def test_a_linkname_target_outside_the_load_is_external(ctx, symbol, external):
    r = classify(site("linkname", None, linkname=symbol), ctx)
    assert (r["status"], r["via"], r["external"]) == ("external", "convention:linkname", external)


def test_range_sites_call_the_ranged_function(ctx):
    calc = target(f"{REPO}/billing/fee.go", 14, 25, "Calculate", "method", "*FeeCalculator")
    r = classify(site("method", calc, site_kind="range"), ctx)
    assert (r["status"], r["via"]) == ("resolved", "convention:range")
    seq = target(f"{GOROOT}/src/maps/iter.go", 9, 6, "Keys", pkg="maps")
    r = classify(site("func", seq, site_kind="range"), ctx)
    assert (r["status"], r["via"]) == ("external", "convention:range")
    r = classify(site("value", site_kind="range", value_kind="local"), ctx)
    assert (r["status"], r["via"]) == ("dynamic", "none")


@pytest.mark.parametrize("pkg_errors,reason", [(True, "missing-module"), (False, "load-error")])
def test_unknown(ctx, pkg_errors, reason):
    r = classify(site("unknown", pkg_errors=pkg_errors), ctx)
    assert (r["status"], r["via"], r["reason"]) == ("unresolved", "none", reason)


def test_every_record_shape_uses_the_vocabularies(ctx):
    shapes = [site("builtin"), site("value"), site("unknown"),
              site("func", target(f"{REPO}/billing/fee.go", 20, 6, "Round")),
              site("iface", TOTAL, iface=iface(f"{BILLING}.Totaler", CASH)),
              site("iface", target(f"{REPO}/x.go", 1, 1, "M", "iface_method", ""), iface=iface("", CASH))]
    keys = {"status", "via", "target", "static_target", "external", "candidates", "open_world",
            "build_variants", "reason", "build"}
    for s in shapes:
        r = classify(s, ctx)
        assert set(r) == keys
        assert r["status"] in STATUSES and r["via"] in VIA
        assert r["reason"] is None or r["reason"] in UNRESOLVED_REASONS + POSSIBLY_REASONS
        assert r["build"] == "linux-amd64"
