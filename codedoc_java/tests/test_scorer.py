"""Tests for graph_generator.scorer — deterministic acceptance scorer.

Everything is synthetic (tmp dirs) except the final schema-sanity tests,
which read the real corpus manifest read-only.

Fixtures speak the Java contract: symbols follow the java_decls grammar
(lowercase packages, `Type.<init>(...)` constructors, declared param types),
identity is the Maven `artifact`, and `via` is one of source / maven / jdk.
"""

import json
import os

import pytest

from graph_generator import scorer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_MANIFEST = os.path.join(
    REPO, "test_codes", "iryo-kanri-system", "ground_truth", "expected_resolutions.json"
)

CALLER_FILE = "reception-desk/src/main/java/iryo/reception/flows/DailyCloseFlow.java"


# ── builders ──────────────────────────────────────────────────


def mk_case(
    cid,
    *,
    file=CALLER_FILE,
    line=10,
    token="log",
    site_kind="invocation",
    resolution="resolved",
    symbol="iryo.kernel.Logger.log(String)",
    artifact="iryo-kernel",
    via="maven",
    candidate_count=None,
    static_target=None,
    runtime_target=None,
    distractors=(),
):
    targets = []
    if resolution in ("resolved", "external"):
        targets = [
            {
                "symbol": symbol,
                "kind": "method",
                "project": artifact,
                "artifact": artifact,
                "via": via,
                "file": None,
                "line": None,
            }
        ]
    elif resolution == "ambiguous":
        targets = [{"candidate_count": candidate_count}]
    expected = {"resolution": resolution, "targets": targets}
    if static_target is not None:
        expected["static_target"] = static_target
    if runtime_target is not None:
        expected["runtime_target"] = runtime_target
    return {
        "id": cid,
        "category": "synthetic",
        "file": file,
        "line": line,
        "token": token,
        "site_kind": site_kind,
        "snippet": token,
        "source_line": token,
        "expected": expected,
        "distractors": list(distractors),
    }


def mk_site(
    *,
    file=CALLER_FILE,
    line=10,
    token="log",
    site_kind="invocation",
    status="resolved",
    symbol="iryo.kernel.Logger.log(String)",
    artifact="iryo-kernel",
    candidate_count=None,
):
    site = {
        "file": file,
        "line": line,
        "col": 1,
        "token": token,
        "site_kind": site_kind,
        "enclosing": None,
        "enclosing_artifact": "reception-desk",
        "status": status,
        "target": None,
        "static_target": None,
        "resolved_via": None,
        "candidate_count": candidate_count,
    }
    if status in ("resolved", "external") and symbol is not None:
        site["target"] = {
            "symbol": symbol,
            "artifact": artifact,
            "kind": "method",
            "file": None,
            "line": None,
        }
    return site


def mk_manifest(cases):
    return {
        "version": 3,
        "root": ".",
        "language_note": "",
        "symbol_grammar": "",
        "case_count": len(cases),
        "cases": cases,
    }


def decls_for(symbols):
    return [
        {
            "artifact": "iryo-kernel",
            "symbol": s,
            "kind": "method",
            "file": "iryo-kernel/src/main/java/iryo/kernel/Logger.java",
            "line": 1,
            "container": "iryo.kernel.Logger",
            "namespace": "iryo.kernel",
        }
        for s in symbols
    ]


def mk_resolution(sites, declarations=(), target_dir="/tmp/corpus"):
    return {
        "version": 2,
        "target_dir": target_dir,
        "lsp": {"enabled": False, "cmd": "jdtls"},
        "solutions": [],
        "fallback_files": [],
        "declarations": list(declarations),
        "sites": list(sites),
        "ambiguous_symbols": [],
        "stats": {},
    }


def verdict_of(case, sites, declarations=(), pins=None):
    result = scorer.score(
        "/tmp/corpus", mk_resolution(sites, declarations), mk_manifest([case]), pins
    )
    return result["results"][0]["verdict"], result


# ── verdict: correct ──────────────────────────────────────────


def test_correct_resolved():
    v, _ = verdict_of(mk_case("A-001"), [mk_site()])
    assert v == "correct"


def test_correct_resolved_null_expected_artifact_ignores_site_artifact():
    case = mk_case("A-001", artifact=None)
    v, _ = verdict_of(case, [mk_site(artifact="whatever")])
    assert v == "correct"


def test_resolved_artifact_mismatch_is_wrong():
    v, _ = verdict_of(mk_case("A-001", artifact="iryo-kernel"),
                      [mk_site(artifact="iryo-pharmacy")])
    assert v == "wrong"


def test_correct_external_by_artifact():
    case = mk_case("A-001", resolution="external",
                   symbol="java.util.stream.Stream.filter(Predicate)",
                   artifact="java.base", via="jdk")
    site = mk_site(status="external", symbol="anything", artifact="java.base")
    v, _ = verdict_of(case, [site])
    assert v == "correct"


def test_external_wrong_artifact_is_wrong():
    case = mk_case("A-001", resolution="external",
                   symbol="java.util.stream.Stream.filter(Predicate)",
                   artifact="java.base", via="jdk")
    v, _ = verdict_of(case, [mk_site(status="external", artifact="java.sql")])
    assert v == "wrong"


def test_correct_ambiguous_candidate_count():
    case = mk_case("A-001", resolution="ambiguous", candidate_count=5)
    v, _ = verdict_of(case, [mk_site(status="ambiguous", symbol=None, candidate_count=5)])
    assert v == "correct"


def test_ambiguous_count_mismatch_is_unanswered():
    case = mk_case("A-001", resolution="ambiguous", candidate_count=5)
    v, _ = verdict_of(case, [mk_site(status="ambiguous", symbol=None, candidate_count=3)])
    assert v == "unanswered"


def test_correct_runtime():
    case = mk_case("A-001", resolution="runtime")
    v, _ = verdict_of(case, [mk_site(status="runtime", symbol=None)])
    assert v == "correct"


# ── verdict: documented_alternate ─────────────────────────────


def test_alternate_static_target_object_under_expected():
    case = mk_case(
        "A-001",
        symbol="iryo.scheduling.SmsNotifier.send(String)",
        static_target={"symbol": "iryo.kernel.Notifier.send(String)",
                       "file": "iryo-kernel/src/main/java/iryo/kernel/Notifier.java",
                       "line": 1},
    )
    v, _ = verdict_of(case, [mk_site(symbol="iryo.kernel.Notifier.send(String)")])
    assert v == "documented_alternate"


def test_alternate_runtime_target_flat_string_on_case_root():
    case = mk_case("A-001", symbol="iryo.domain.Entity.describe()")
    case["runtime_target"] = "iryo.domain.Patient.describe()"  # flat-string root shape
    v, _ = verdict_of(case, [mk_site(symbol="iryo.domain.Patient.describe()")])
    assert v == "documented_alternate"


def test_alternate_when_expected_runtime_but_resolved_to_documented():
    case = mk_case("A-001", resolution="runtime",
                   runtime_target={"symbol": "iryo.data.ChartArchive.compact()",
                                   "file": "iryo-data/src/main/java/iryo/data/ChartArchive.java",
                                   "line": 2})
    v, _ = verdict_of(case, [mk_site(symbol="iryo.data.ChartArchive.compact()")])
    assert v == "documented_alternate"


def test_alternate_not_counted_in_correct():
    case = mk_case(
        "A-001",
        symbol="iryo.scheduling.SmsNotifier.send(String)",
        static_target={"symbol": "iryo.kernel.Notifier.send(String)",
                       "file": "iryo-kernel/src/main/java/iryo/kernel/Notifier.java",
                       "line": 1},
    )
    _, result = verdict_of(case, [mk_site(symbol="iryo.kernel.Notifier.send(String)")])
    assert result["correct"] == 0
    assert result["alternate"] == 1
    assert result["wrong"] == 0


# ── verdict: wrong ────────────────────────────────────────────


def test_distractor_symbol_is_wrong():
    case = mk_case("A-001", distractors=["iryo.pharmacy.Logger.log(String)"])
    v, _ = verdict_of(case, [mk_site(symbol="iryo.pharmacy.Logger.log(String)")])
    assert v == "wrong"


def test_unknown_symbol_is_wrong():
    v, _ = verdict_of(mk_case("A-001"), [mk_site(symbol="iryo.kernel.Logger.log(int)")])
    assert v == "wrong"


def test_expected_runtime_but_resolved_nonalternate_is_wrong():
    case = mk_case("A-001", resolution="runtime")
    v, _ = verdict_of(case, [mk_site(symbol="iryo.kernel.Logger.log(String)")])
    assert v == "wrong"


def test_expected_ambiguous_but_external_is_wrong():
    case = mk_case("A-001", resolution="ambiguous", candidate_count=5)
    v, _ = verdict_of(case, [mk_site(status="external", artifact="java.base")])
    assert v == "wrong"


def test_wrong_details_recorded():
    case = mk_case("A-001", distractors=["iryo.pharmacy.Logger.log(String)"])
    _, result = verdict_of(case, [mk_site(symbol="iryo.pharmacy.Logger.log(String)")])
    (d,) = result["wrong_details"]
    assert d["id"] == "A-001"
    assert d["expected"] == "iryo.kernel.Logger.log(String)"
    assert d["got"] == "iryo.pharmacy.Logger.log(String)"
    assert d["site"] == f"{CALLER_FILE}:10"


# ── verdict: unanswered ───────────────────────────────────────


def test_no_site_is_unanswered():
    v, _ = verdict_of(mk_case("A-001"), [])
    assert v == "unanswered"


def test_unresolved_is_unanswered():
    v, _ = verdict_of(mk_case("A-001"), [mk_site(status="unresolved", symbol=None)])
    assert v == "unanswered"


def test_builtin_is_unanswered():
    v, _ = verdict_of(mk_case("A-001"), [mk_site(status="builtin", symbol=None)])
    assert v == "unanswered"


def test_runtime_when_expected_resolved_is_unanswered():
    v, _ = verdict_of(mk_case("A-001"), [mk_site(status="runtime", symbol=None)])
    assert v == "unanswered"


# ── site matching ─────────────────────────────────────────────


def test_matching_prefers_same_site_kind():
    case = mk_case("A-001", site_kind="invocation",
                   distractors=["iryo.pharmacy.Logger.log(String)"])
    decoy = mk_site(site_kind="method_reference", symbol="iryo.pharmacy.Logger.log(String)")
    good = mk_site(site_kind="invocation", symbol="iryo.kernel.Logger.log(String)")
    v, _ = verdict_of(case, [decoy, good])  # decoy first: kind preference must win
    assert v == "correct"


def test_matching_falls_back_to_first_match():
    case = mk_case("A-001", site_kind="invocation")
    only = mk_site(site_kind="method_reference", symbol="iryo.kernel.Logger.log(String)")
    v, _ = verdict_of(case, [only])
    assert v == "correct"


def test_matching_requires_file_line_token():
    case = mk_case("A-001")
    v, _ = verdict_of(case, [mk_site(line=11)])
    assert v == "unanswered"
    v, _ = verdict_of(case, [mk_site(file="admin-console/src/main/java/iryo/admin/DailyBatch.java")])
    assert v == "unanswered"
    v, _ = verdict_of(case, [mk_site(token="flush")])
    assert v == "unanswered"


# ── result shape ──────────────────────────────────────────────


def test_result_shape_and_per_category():
    """Category buckets split the Java case IDs on '-' (XPR-001 -> XPR)."""
    cases = [
        mk_case("XPR-001", file="a/A.java"),
        mk_case("XPR-002", file="b/B.java", distractors=["iryo.pharmacy.Logger.log(String)"]),
        mk_case("DI-001", file="c/C.java"),
    ]
    sites = [
        mk_site(file="a/A.java"),
        mk_site(file="b/B.java", symbol="iryo.pharmacy.Logger.log(String)"),
    ]
    result = scorer.score("/tmp/corpus", mk_resolution(sites), mk_manifest(cases), None)
    assert result["case_count"] == 3
    assert result["correct"] == 1
    assert result["wrong"] == 1
    assert result["unanswered"] == 1
    assert result["per_category"]["XPR"] == {
        "total": 2, "correct": 1, "alternate": 0, "unanswered": 0, "wrong": 1,
    }
    assert result["per_category"]["DI"]["unanswered"] == 1
    r0 = result["results"][0]
    assert r0["id"] == "XPR-001" and r0["verdict"] == "correct"
    assert r0["expected"]["symbol"] == "iryo.kernel.Logger.log(String)"
    assert r0["expected"]["artifact"] == "iryo-kernel"
    assert r0["got"]["status"] == "resolved"
    assert r0["got"]["symbol"] == "iryo.kernel.Logger.log(String)"
    assert r0["got"]["artifact"] == "iryo-kernel"
    unanswered = [r for r in result["results"] if r["id"] == "DI-001"][0]
    assert unanswered["got"] is None
    assert isinstance(result["passed"], bool)
    assert set(result["gates"]) == {
        "completeness", "zero_wrong", "node_coverage", "edge_resolution",
    }


# ── gate mechanics on a full-size corpus ──────────────────────


def big_corpus(n=106):
    cases, sites, syms = [], [], []
    for i in range(n):
        sym = f"iryo.demo.C{i}.run()"
        f = f"iryo-demo/src/main/java/iryo/demo/C{i}.java"
        cases.append(mk_case(f"GEN-{i:03d}", file=f, symbol=sym))
        sites.append(mk_site(file=f, symbol=sym))
        syms.append(sym)
    return cases, sites, syms


def test_all_gates_pass():
    cases, sites, syms = big_corpus()
    result = scorer.score(
        "/tmp/corpus", mk_resolution(sites, decls_for(syms)), mk_manifest(cases), None
    )
    assert result["gates"] == {
        "completeness": True, "zero_wrong": True,
        "node_coverage": True, "edge_resolution": True,
    }
    assert result["passed"] is True
    assert result["correct"] == 106


def test_gate_completeness_fails_alone_at_89():
    cases, sites, syms = big_corpus()
    result = scorer.score(
        "/tmp/corpus",
        mk_resolution(sites[:89], decls_for(syms)),  # 17 cases get no site
        mk_manifest(cases),
        None,
    )
    assert result["correct"] == 89
    assert result["gates"]["completeness"] is False
    assert result["gates"]["zero_wrong"] is True
    assert result["gates"]["node_coverage"] is True
    assert result["gates"]["edge_resolution"] is True
    assert result["passed"] is False


def test_gate_zero_wrong_fails_alone():
    cases, sites, syms = big_corpus()
    cases[0]["distractors"] = ["iryo.demo.Evil.run()"]
    sites[0] = mk_site(file="iryo-demo/src/main/java/iryo/demo/C0.java",
                       symbol="iryo.demo.Evil.run()")
    result = scorer.score(
        "/tmp/corpus",
        mk_resolution(sites, decls_for(syms + ["iryo.demo.Evil.run()"])),
        mk_manifest(cases),
        None,
    )
    assert result["wrong"] == 1 and result["correct"] == 105
    assert result["gates"]["zero_wrong"] is False
    assert result["gates"]["completeness"] is True
    assert result["gates"]["node_coverage"] is True
    assert result["gates"]["edge_resolution"] is True


def test_gate_node_coverage_fails_alone():
    cases, sites, syms = big_corpus()
    result = scorer.score(
        "/tmp/corpus",
        mk_resolution(sites, decls_for(syms[:80])),  # 80/106 = 0.755 < 0.85
        mk_manifest(cases),
        None,
    )
    assert result["gates"]["node_coverage"] is False
    assert result["node_inventory"] == 106
    assert result["node_covered"] == 80
    assert result["gates"]["completeness"] is True
    assert result["gates"]["zero_wrong"] is True
    assert result["gates"]["edge_resolution"] is True


def test_gate_edge_resolution_fails_alone():
    cases, sites, syms = big_corpus()
    extra = [
        mk_site(file=f"iryo-demo/src/main/java/iryo/demo/N{i}.java",
                status="unresolved", symbol=None)
        for i in range(20)
    ]  # 106/126 = 0.841 < 0.85
    result = scorer.score(
        "/tmp/corpus",
        mk_resolution(sites + extra, decls_for(syms)),
        mk_manifest(cases),
        None,
    )
    assert result["gates"]["edge_resolution"] is False
    assert result["gates"]["completeness"] is True
    assert result["gates"]["zero_wrong"] is True
    assert result["gates"]["node_coverage"] is True


def test_edge_rate_filters_kind_and_builtin():
    sites = [
        mk_site(file="a.java", site_kind="invocation", status="resolved"),
        mk_site(file="b.java", site_kind="object_creation", status="external",
                artifact="java.base"),
        mk_site(file="c.java", site_kind="invocation", status="builtin", symbol=None),
        mk_site(file="d.java", site_kind="class_literal", status="unresolved", symbol=None),
        mk_site(file="e.java", site_kind="invocation", status="unresolved", symbol=None),
        mk_site(file="f.java", site_kind="invocation", status="ambiguous", symbol=None,
                candidate_count=2),
    ]
    result = scorer.score("/tmp/corpus", mk_resolution(sites), mk_manifest([]), None)
    # denominator: a, b, e, f (builtin and class_literal excluded); numerator: a, b
    assert result["edge_total"] == 4
    assert result["edge_resolved"] == 2
    assert result["edge_rate"] == pytest.approx(0.5)


# ── node inventory: pins vs manifest fallback ─────────────────


def test_inventory_from_manifest_when_no_pins():
    cases = [
        mk_case("A-001", file="a.java", symbol="iryo.kernel.Logger.log(String)", via="maven",
                distractors=["iryo.pharmacy.Logger.log(String)"],
                static_target={"symbol": "iryo.kernel.Notifier.send(String)",
                               "file": "x.java", "line": 1}),
        mk_case("A-002", file="b.java", resolution="external",
                symbol="java.util.stream.Stream.filter(Predicate)",
                artifact="java.base", via="jdk"),
    ]
    resolution = mk_resolution(
        [], decls_for(["iryo.kernel.Logger.log(String)", "iryo.kernel.Notifier.send(String)"]))
    result = scorer.score("/tmp/corpus", resolution, mk_manifest(cases), None)
    # inventory = {Logger.log (maven), pharmacy Logger.log (distractor),
    #              Notifier.send (static_target)}; the jdk target is excluded
    assert result["node_inventory"] == 3
    assert result["node_covered"] == 2
    assert result["node_coverage"] == pytest.approx(2 / 3)


def test_inventory_from_pins_when_given():
    cases = [mk_case("A-001", file="a.java", symbol="iryo.kernel.Logger.log(String)")]
    resolution = mk_resolution([], decls_for(["iryo.kernel.Logger.log(String)"]))
    pins = {
        "iryo.kernel.Logger.log(String)": ("a.java", "regex", 1),
        "iryo.kernel.Logger.flush()": ("g.java", "regex", 1),
    }
    result = scorer.score("/tmp/corpus", resolution, mk_manifest(cases), pins)
    assert result["node_inventory"] == 2
    assert result["node_covered"] == 1
    assert result["node_coverage"] == pytest.approx(0.5)


def test_source_via_included_in_manifest_inventory():
    cases = [mk_case("A-001", file="a.java", symbol="iryo.reception.Desk.open()",
                     artifact="reception-desk", via="source")]
    result = scorer.score("/tmp/corpus", mk_resolution([]), mk_manifest(cases), None)
    assert result["node_inventory"] == 1
    assert result["node_covered"] == 0


def test_maven_via_included_and_jdk_via_excluded_from_inventory():
    cases = [
        mk_case("A-001", file="a.java", symbol="iryo.billing.PointMath.toYen(int)",
                artifact="iryo-billing", via="maven"),
        mk_case("A-002", file="b.java", resolution="external",
                symbol="java.util.List.get(int)", artifact="java.base", via="jdk"),
    ]
    result = scorer.score("/tmp/corpus", mk_resolution([]), mk_manifest(cases), None)
    assert result["node_inventory"] == 1  # only the maven target


# ── completeness threshold: derived from the loaded manifest ──


def test_gate_completeness_boundary_is_over_85_percent():
    """85% of 106 is 90.1 — 90 correct is NOT over 85%, 91 is
    (floor(0.85*106)+1 = 91: the C# boundary, now derived)."""
    cases, sites, syms = big_corpus()
    at_90 = scorer.score("/tmp/corpus", mk_resolution(sites[:90], decls_for(syms)),
                         mk_manifest(cases), None)
    assert at_90["completeness_min"] == 91
    assert at_90["correct"] == 90
    assert at_90["gates"]["completeness"] is False
    at_91 = scorer.score("/tmp/corpus", mk_resolution(sites[:91], decls_for(syms)),
                         mk_manifest(cases), None)
    assert at_91["correct"] == 91
    assert at_91["gates"]["completeness"] is True


def test_threshold_derived_from_case_count_not_hardcoded():
    """N=20: floor(0.85*20)+1 = 18 — strictly more than 85% even though 85%
    of 20 divides evenly (17 = exactly 85% must FAIL)."""
    cases, sites, syms = big_corpus(20)
    at_17 = scorer.score("/tmp/corpus", mk_resolution(sites[:17], decls_for(syms)),
                         mk_manifest(cases), None)
    assert at_17["case_count"] == 20
    assert at_17["completeness_min"] == 18
    assert at_17["correct"] == 17
    assert at_17["gates"]["completeness"] is False
    at_18 = scorer.score("/tmp/corpus", mk_resolution(sites[:18], decls_for(syms)),
                         mk_manifest(cases), None)
    assert at_18["correct"] == 18
    assert at_18["gates"]["completeness"] is True


@pytest.mark.parametrize(
    "n,expected_min",
    [(0, 1), (1, 1), (20, 18), (95, 81), (100, 86), (106, 91)],  # floor(0.85N)+1, min 1
)
def test_completeness_min_formula(n, expected_min):
    cases, _, _ = big_corpus(n)
    result = scorer.score("/tmp/corpus", mk_resolution([]), mk_manifest(cases), None)
    assert result["completeness_min"] == expected_min


def test_report_shows_derived_threshold():
    cases, sites, syms = big_corpus(20)
    result = scorer.score("/tmp/corpus", mk_resolution(sites, decls_for(syms)),
                          mk_manifest(cases), None)
    report = scorer.format_report(result)
    assert "基準 18 件以上" in report
    assert "正解 20/20" in report


def test_manifest_version_3_accepted_and_version_is_not_consulted():
    """The Java manifest is version 3; scoring depends on fields only."""
    cases, sites, syms = big_corpus(20)
    v3 = mk_manifest(cases)
    assert v3["version"] == 3
    scored_v3 = scorer.score("/tmp/corpus", mk_resolution(sites, decls_for(syms)), v3, None)
    versionless = {k: v for k, v in v3.items() if k != "version"}
    scored_none = scorer.score(
        "/tmp/corpus", mk_resolution(sites, decls_for(syms)), versionless, None)
    assert scored_v3["passed"] is True
    assert scored_v3["gates"] == scored_none["gates"]
    assert scored_v3["results"] == scored_none["results"]


# ── CLI ───────────────────────────────────────────────────────


def write_corpus(tmp_path, manifest, resolution, cases_py=None):
    corpus = tmp_path / "corpus"
    gt = corpus / "ground_truth"
    gt.mkdir(parents=True)
    (gt / "expected_resolutions.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    res_path = tmp_path / "resolution.json"
    res_path.write_text(json.dumps(resolution, ensure_ascii=False), encoding="utf-8")
    if cases_py is not None:
        (gt / "cases.py").write_text(cases_py, encoding="utf-8")
    return corpus, res_path


def test_cli_pass_exit_0_and_report(tmp_path, capsys):
    cases, sites, syms = big_corpus()
    corpus, res_path = write_corpus(
        tmp_path, mk_manifest(cases), mk_resolution(sites, decls_for(syms))
    )
    rc = scorer.main([str(corpus), "--resolution", str(res_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "完全性 (completeness)" in out
    assert "誤答ゼロ (zero-wrong)" in out
    assert "ノード被覆率 (node-coverage)" in out
    assert "エッジ解決率 (edge-resolution)" in out
    assert "106/106" in out
    assert "基準 91 件以上" in out
    assert "100.0%" in out
    assert "合格" in out


def test_cli_fail_exit_1_lists_wrong(tmp_path, capsys):
    cases, sites, syms = big_corpus()
    sites[0] = mk_site(file="iryo-demo/src/main/java/iryo/demo/C0.java",
                       symbol="iryo.demo.Evil.run()")
    corpus, res_path = write_corpus(
        tmp_path, mk_manifest(cases),
        mk_resolution(sites, decls_for(syms + ["iryo.demo.Evil.run()"])),
    )
    rc = scorer.main([str(corpus), "--resolution", str(res_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "GEN-000" in out
    assert "iryo.demo.Evil.run()" in out


def test_cli_missing_resolution_exit_2(tmp_path, capsys):
    cases, _, _ = big_corpus(3)
    corpus, _ = write_corpus(tmp_path, mk_manifest(cases), mk_resolution([]))
    rc = scorer.main([str(corpus), "--resolution", str(tmp_path / "nope.json")])
    assert rc == 2


def test_cli_default_resolution_path_uses_config(tmp_path, monkeypatch, capsys):
    from graph_generator import config

    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "empty_out"))
    cases, _, _ = big_corpus(3)
    corpus, _ = write_corpus(tmp_path, mk_manifest(cases), mk_resolution([]))
    rc = scorer.main([str(corpus)])
    assert rc == 2  # <OUTPUT_DIR>/resolution.json does not exist


def test_cli_pins_merged_from_cases_py(tmp_path, capsys):
    cases, sites, syms = big_corpus()
    cases_py = (
        "DECL_PINS = {'%s': ('iryo-demo/src/main/java/iryo/demo/C0.java', 'x', 1)}\n"
        "STATIC_TARGET_PINS = {'iryo.kernel.Notifier.send(String)': ('g.java', 'x', 1)}\n"
        % syms[0]
    )
    corpus, res_path = write_corpus(
        tmp_path, mk_manifest(cases), mk_resolution(sites, decls_for(syms)), cases_py
    )
    # pins inventory = {C0.run, Notifier.send}: coverage 0.5 < 0.85 -> gate fails
    rc = scorer.main([str(corpus), "--resolution", str(res_path)])
    assert rc == 1
    # without cases.py the manifest inventory is fully covered -> pass
    os.remove(corpus / "ground_truth" / "cases.py")
    rc = scorer.main([str(corpus), "--resolution", str(res_path)])
    assert rc == 0


# ── real manifest schema sanity (read-only, no scoring) ───────


def test_real_manifest_schema_sanity():
    with open(REAL_MANIFEST, encoding="utf-8") as fh:
        m = json.load(fh)
    assert m["case_count"] == len(m["cases"]) > 0
    for case in m["cases"][:5] + m["cases"][-5:]:
        for key in ("id", "file", "line", "token", "site_kind", "expected", "distractors"):
            assert key in case, f"{case.get('id')}: missing {key}"
        assert "resolution" in case["expected"]
        assert "targets" in case["expected"]
    resolutions = {c["expected"]["resolution"] for c in m["cases"]}
    assert resolutions <= {"resolved", "external", "ambiguous", "runtime"}


def test_real_manifest_speaks_the_artifact_contract_when_v3():
    """Java manifest (version 3) must use the same `artifact` identity key and
    the source/maven/jdk via vocabulary the scorer compares against."""
    with open(REAL_MANIFEST, encoding="utf-8") as fh:
        m = json.load(fh)
    if m.get("version", 0) < 3:
        pytest.skip(f"manifest version {m.get('version')} — pre-Java ground truth")
    vias, keyed = set(), 0
    for case in m["cases"]:
        for target in case["expected"].get("targets") or []:
            if not target.get("symbol"):
                continue
            keyed += 1
            assert "artifact" in target, f"{case['id']}: target has no 'artifact' key"
            vias.add(target.get("via"))
    assert keyed > 0
    assert vias <= {"source", "maven", "jdk"}, vias


def test_two_same_token_sites_on_one_line_disambiguated_by_snippet(tmp_path):
    """NSP shape: two `locate()` calls on one line resolving to different
    symbols — each case must grade its own call site (matched via the manifest
    snippet's column), not the first token match."""
    from graph_generator.scorer import score

    target = tmp_path / "corpus"
    src = target / "reception-desk/src/main/java/iryo/reception"
    src.mkdir(parents=True)
    rel = "reception-desk/src/main/java/iryo/reception/Rounds.java"
    line_text = '        System.out.println(north.locate() + south.locate());'
    (target / rel).write_text(
        "class Rounds { void walk() {\n" + line_text + "\n} }\n", encoding="utf-8")

    def case(cid, snippet, symbol):
        return {
            "id": cid, "category": "namespaces/x", "file": rel, "line": 2,
            "token": "locate", "site_kind": "invocation", "snippet": snippet,
            "source_line": line_text.strip(),
            "expected": {"resolution": "resolved", "targets": [
                {"symbol": symbol, "kind": "method", "project": "iryo-domain",
                 "artifact": "iryo-domain", "via": "maven",
                 "file": "iryo-domain/src/main/java/iryo/domain/wards/Ward.java",
                 "line": 1}]},
            "distractors": [],
        }

    manifest = {"case_count": 2, "cases": [
        case("NSP-107", "north.locate()", "iryo.domain.north.Ward.locate()"),
        case("NSP-108", "south.locate()", "iryo.domain.south.Ward.locate()"),
    ]}

    north_col = line_text.index("north.locate") + len("north.")
    south_col = line_text.index("south.locate") + len("south.")

    def site(col, symbol):
        return {"file": rel, "line": 2, "col": col, "token": "locate",
                "site_kind": "invocation", "enclosing": "iryo.reception.Rounds.walk()",
                "enclosing_artifact": "reception-desk",
                "status": "resolved",
                "target": {"symbol": symbol, "artifact": "iryo-domain", "kind": "method",
                           "file": "iryo-domain/src/main/java/iryo/domain/wards/Ward.java",
                           "line": 1},
                "static_target": None, "resolved_via": "lsp", "candidate_count": None}

    resolution = {
        "version": 2, "target_dir": str(target), "lsp": {"enabled": True, "cmd": "jdtls"},
        "solutions": [{"sln": "reception-desk/pom.xml", "status": "resolved",
                       "reason": None, "files": [rel]}],
        "fallback_files": [],
        "declarations": [],
        "sites": [site(north_col, "iryo.domain.north.Ward.locate()"),
                  site(south_col, "iryo.domain.south.Ward.locate()")],
        "ambiguous_symbols": [],
        "stats": {},
    }

    report = score(str(target), resolution, manifest, pins=None)
    verdicts = {r["id"]: r["verdict"] for r in report["results"]}
    assert verdicts["NSP-107"] == "correct", report["results"]
    assert verdicts["NSP-108"] == "correct", report["results"]
    assert report["wrong"] == 0
