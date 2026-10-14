"""go_gates (spec §5.6, R1, R5): G1-G10 on classified records, in order."""

from __future__ import annotations

import pytest

from graph_generator import go_classify, go_gates
from graph_generator.go_gates import SiteContext
from test_go_classify import (BILLING, CASH, GOROOT, REPO, TOTAL, ctx, iface, in_repo,  # noqa: F401
                              site, target)


def gated(rec, ctx, **kw):
    return go_gates.apply_gates(go_classify.classify_site(rec, ctx), SiteContext(helper=rec, ctx=ctx, **kw))


ROUND = target(f"{REPO}/billing/fee.go", 20, 6, "Round", module=BILLING)
NATIVE = target(f"{REPO}/billing/twin_linux.go", 3, 6, "Native", module=BILLING)


# ---------------------------------------------------------------- per site

def test_g1_variants_that_disagree_make_the_site_ambiguous(ctx):
    other = target(f"{REPO}/billing/impl_test.go", 6, 15, "Total", "method", "Fake")
    r = gated(site("method", ROUND, variants=[ROUND, other]), ctx)
    assert (r["status"], r["reason"], r["via"], r["target"]) == ("ambiguous", "variant", "types", None)
    assert [c["qualname"] for c in r["candidates"]] == ["Round", "Fake.Total"]
    # test_only marks a candidate declared in a _test.go file (spec §5.4).
    assert [(c["test_only"], c["via_embedded_iface"]) for c in r["candidates"]] == [(False, False), (True, False)]


def test_g2_a_target_in_a_stub_package_is_missing_module(ctx):
    r = gated(site("func", None, stub=True), ctx)
    assert (r["status"], r["reason"], r["target"]) == ("unresolved", "missing-module", None)


def test_g3_an_invalid_embedded_field_on_the_path_is_incomplete_type(ctx):
    r = gated(site("method", None, embed_invalid=True, promoted_depth=2), ctx)
    assert (r["status"], r["reason"]) == ("unresolved", "incomplete-type")


def test_g4_suppressed_candidate_search_keeps_the_static_target(ctx):
    r = gated(site("iface", TOTAL, iface=iface(f"{BILLING}.Totaler", CASH, suppressed=True)), ctx)
    assert (r["status"], r["target"]["qualname"], r["candidates"]) == ("resolved", "Totaler.Total", [])


@pytest.mark.parametrize("facts,reason", [
    (dict(unit=None, group_status=None, group_reason=None, build_included=True), "no-module"),
    (dict(unit="m", group_status="toolchain", group_reason=None, build_included=True), "toolchain"),
    (dict(unit="m", group_status="error", group_reason="toolchain", build_included=True), "toolchain"),
    (dict(unit="m", group_status="error", group_reason="load-error", build_included=False), "load-error"),
    (dict(unit="m", group_status="ok", group_reason=None, build_included=False), "excluded-by-build"),
    (dict(unit="m", group_status="ok", group_reason=None, build_included=True), "load-error"),
])
def test_g5_g6_and_r5_sites_without_an_answer(ctx, facts, reason):
    assert go_gates.no_answer_reason(**facts) == reason
    r = go_gates.apply_gates(go_gates.no_answer_record("linux-amd64"), SiteContext(no_answer=reason, line=3))
    assert (r["status"], r["reason"], r["via"]) == ("unresolved", reason, "none")


def test_r5_reason_from_the_helpers_diagnostics():
    assert go_gates.load_failure_reason("go: go.work requires go >= 1.29 (running go 1.27.2)") == "toolchain"
    assert go_gates.load_failure_reason("signal: killed") == "load-error"
    assert go_gates.load_failure_reason("") == "load-error"


def test_g7_a_target_without_a_node_is_no_declaration(ctx):
    r = gated(site("func", target(f"{REPO}/billing/fee.go", 33, 1, "Hidden")), ctx)
    assert (r["status"], r["reason"]) == ("unresolved", "no-declaration")


def test_g9_sites_in_an_error_region(ctx):
    r = gated(site("func", ROUND), ctx, line=40, syntax_errors=[40, 52])
    assert (r["status"], r["reason"]) == ("unresolved", "syntax-error")
    r = gated(site("func", ROUND), ctx, line=39, syntax_errors=[40])
    assert r["status"] == "resolved"


def test_g10_build_twins_are_listed_and_not_edged(ctx):
    r = gated(site("func", NATIVE), ctx)
    assert (r["status"], r["target"]["relpath"]) == ("resolved", "billing/twin_linux.go")
    assert r["build_variants"] == [in_repo("billing/twin_windows.go", "Native", 3)]


def test_g9_promoted_answers_through_a_package_with_syntax_errors(ctx):
    """A syntax error can swallow a shallower method; a promoted selection or
    a method alias then names the wrong method (spec-review of WP-B2)."""
    calc = target(f"{REPO}/billing/fee.go", 14, 25, "Calculate", "method", "*FeeCalculator")
    broken = frozenset({"billing"})
    for kw in ({"promoted_depth": 1}, {"alias": calc, "value_kind": "local"}):
        cls = "value" if "alias" in kw else "method"
        rec_ = site(cls, None if cls == "value" else calc, **kw)
        r = gated(rec_, ctx, relpath="cmd/app/main.go", broken_dirs=broken)        # the target's dir
        assert (r["status"], r["reason"]) == ("unresolved", "syntax-error")
        r = gated(rec_, ctx, relpath="billing/use.go", broken_dirs=frozenset({"billing"}))   # the site's dir
        assert r["reason"] == "syntax-error"
        r = gated(rec_, ctx, relpath="cmd/app/main.go", broken_dirs=frozenset({"other"}))
        assert r["status"] == "resolved"
    ext = target(f"{GOROOT}/src/sync/mutex.go", 5, 18, "Lock", "method", "*Mutex", pkg="sync")
    r = gated(site("method", ext, promoted_depth=1), ctx, relpath="billing/use.go", broken_dirs=broken)
    assert r["reason"] == "syntax-error"
    r = gated(site("method", calc), ctx, relpath="billing/use.go", broken_dirs=broken)   # not promoted
    assert r["status"] == "resolved"


@pytest.mark.parametrize("alias", [ROUND, TOTAL, target(f"{GOROOT}/src/strings/strings.go", 9, 6, "ToUpper", pkg="strings")])
def test_g9_any_func_alias_answer_in_a_file_with_syntax_errors(ctx, alias):
    """A reassignment swallowed by the parse error would break the single-binding proof."""
    rec_ = site("value", None, alias=alias, value_kind="local", iface=iface(f"{BILLING}.Totaler", CASH))
    r = gated(rec_, ctx, line=3, syntax_errors=[40])
    assert (r["status"], r["reason"]) == ("unresolved", "syntax-error")
    assert gated(rec_, ctx, line=3, syntax_errors=[])["status"] in ("resolved", "external")


def test_precedence(ctx):
    # G9 beats G2 beats G1.
    r = gated(site("func", ROUND, stub=True, variants=[ROUND, NATIVE]), ctx, line=5, syntax_errors=[5])
    assert r["reason"] == "syntax-error"
    r = gated(site("func", ROUND, stub=True, variants=[ROUND, NATIVE]), ctx)
    assert r["reason"] == "missing-module"
    # A syntax error also beats a file-level reason.
    r = go_gates.apply_gates(go_gates.no_answer_record("b"),
                             SiteContext(no_answer="excluded-by-build", line=7, syntax_errors=[2]))
    assert r["reason"] == "syntax-error"


def test_gated_records_carry_no_internal_keys(ctx):
    for rec_ in (site("func", target(f"{REPO}/billing/fee.go", 33, 1, "Hidden")), site("func", ROUND)):
        assert not [k for k in gated(rec_, ctx) if k.startswith("_")]


# ---------------------------------------------------------------- G8, after every group

def call(status="resolved", target=None, candidates=(), variants=(), reason=None):
    return {"site": {"caller": "F", "kind": "call", "name": "x", "line": 1, "col": 1}, "status": status,
            "via": "types" if status == "resolved" else "none", "target": target, "static_target": None,
            "external": None, "candidates": list(candidates), "open_world": False,
            "build_variants": list(variants), "reason": reason, "build": "linux-amd64"}


def test_g8_stale_files_and_records_pointing_into_them():
    files = {
        "a.go": {"calls": [call(target=in_repo("b.go", "B", 3)), call(target=in_repo("a.go", "A", 1))]},
        "b.go": {"calls": [call(target=in_repo("a.go", "A", 1))]},
        "c.go": {"calls": [call(target=in_repo("a.go", "A", 1), candidates=[in_repo("b.go", "B", 3)]),
                           call(target=in_repo("a.go", "A", 1), variants=[in_repo("b.go", "B", 3)]),
                           call(status="external", target={"repo": "peer", "relpath": "b.go", "qualname": "B",
                                                           "unit": "u", "package": "p", "line": 1,
                                                           "kind": "method"})]},
    }
    assert go_gates.mark_stale(files, {"b.go"}) == 4
    reasons = {rel: [(c["status"], c["reason"]) for c in f["calls"]] for rel, f in files.items()}
    stale = ("unresolved", "stale")
    assert reasons == {"a.go": [stale, ("resolved", None)], "b.go": [stale],
                       "c.go": [stale, stale, ("external", None)]}
    assert files["a.go"]["calls"][0]["target"] is None and files["c.go"]["calls"][0]["candidates"] == []


def test_g8_covers_imports_embeds_and_implements():
    imp = {"line": 3, "col": 8, "import_path": "example.com/b", "status": "resolved",
           "target": {"unit": "u", "package": "example.com/b", "dir": "b", "clause": "b"}, "external": None,
           "reason": None}
    emb = {"type": {"relpath": "a.go", "qualname": "A"}, "embedded": in_repo("b.go", "B", 3, "class"),
           "status": "resolved", "external": None, "pointer": False}
    impl = {"type": {"relpath": "a.go", "qualname": "A"}, "iface": in_repo("c.go", "I", 1, "class"),
            "status": "resolved", "external": None, "pointer_receiver": False}
    files = {"a.go": {"calls": [], "imports": [dict(imp)], "embeds": [dict(emb)], "implements": [dict(impl)]},
             "b.go": {"calls": [], "imports": [dict(imp)], "embeds": [], "implements": []}}
    assert go_gates.mark_stale(files, {"b.go"}) == 2
    assert (files["a.go"]["embeds"][0]["status"], files["a.go"]["embeds"][0]["embedded"]) == ("unresolved", None)
    assert files["a.go"]["imports"][0]["status"] == "resolved"                   # a.go itself did not change
    assert files["a.go"]["implements"][0]["status"] == "resolved"                # c.go did not change
    assert (files["b.go"]["imports"][0]["status"], files["b.go"]["imports"][0]["reason"],
            files["b.go"]["imports"][0]["target"]) == ("unresolved", "stale", None)
    assert go_gates.mark_stale(files, {"a.go"}) == 3
    assert files["a.go"]["implements"][0]["reason"] == "stale" and files["a.go"]["imports"][0]["reason"] == "stale"
