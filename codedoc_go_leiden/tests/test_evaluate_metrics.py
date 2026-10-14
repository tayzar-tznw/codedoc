"""evaluate: node coverage, edge rate, QA, the F3 edge set and the F2 discovery check."""
from __future__ import annotations

import copy
import json

import pytest

from graph_generator import evaluate, pipeline
from graph_generator.go_model import BuildConfig, Discovery, GoUnit, Group, Replace
from tests.eval_worlds import (TEST_CODES, class_row, edge, entity, external, file_row, import_record,
                               method_row, package_row, record, run, target)

M = "example.com/iryo/billing"
P = "example.com/iryo/billing"
Q = "example.com/iryo/desk"


def _view_run() -> evaluate.RepoRun:
    """billing/fee.go: FeeCalculator{Calculate, Scaled, Calculate.func1}, NewFeeCalculator;
    desk/run.go: Run calls Calculate (cross module), Calculate calls Round; Notifier/Mail."""
    nodes = {
        "Files": [file_row("iryo", "billing/fee.go"), file_row("iryo", "desk/run.go")],
        "Classes": [class_row("iryo", "billing/fee.go", "FeeCalculator", module=M, package=P),
                    class_row("iryo", "billing/fee.go", "<package>", module=M, package=P, kind="package"),
                    class_row("iryo", "billing/fee.go", "Notifier", module=M, package=P, kind="interface"),
                    class_row("iryo", "billing/fee.go", "Mail", module=M, package=P),
                    class_row("iryo", "billing/fee.go", "Base", module=M, package=P),
                    class_row("iryo", "desk/run.go", "<package>", module=Q, package=Q, kind="package")],
        "Methods": [method_row("iryo", "billing/fee.go", "FeeCalculator.Calculate", module=M, package=P,
                               owner="FeeCalculator", member_kind="method"),
                    method_row("iryo", "billing/fee.go", "FeeCalculator.Scaled", module=M, package=P,
                               owner="FeeCalculator", member_kind="method"),
                    method_row("iryo", "billing/fee.go", "FeeCalculator.Calculate.func1", module=M,
                               package=P, owner="FeeCalculator", member_kind="closure"),
                    method_row("iryo", "billing/fee.go", "NewFeeCalculator", module=M, package=P),
                    method_row("iryo", "billing/fee.go", "Round", module=M, package=P),
                    method_row("iryo", "billing/fee.go", "Notifier.Send", module=M, package=P,
                               owner="Notifier", member_kind="interface_method"),
                    method_row("iryo", "billing/fee.go", "Mail.Send", module=M, package=P,
                               owner="Mail", member_kind="method"),
                    method_row("iryo", "desk/run.go", "Run", module=Q, package=Q)],
    }
    mid = {m["qualname"]: m["method_id"] for m in nodes["Methods"]}
    cid = {c["qualname"]: c["class_id"] for c in nodes["Classes"]}
    edges = {
        "MethodCalls": [edge("MethodCalls", mid["Run"], mid["FeeCalculator.Calculate"]),
                        edge("MethodCalls", mid["FeeCalculator.Calculate"], mid["Round"]),
                        edge("MethodCalls", mid["Run"], mid["Notifier.Send"])],
        "PossiblyCalls": [edge("PossiblyCalls", mid["Run"], mid["Mail.Send"])],
        "ClassImplements": [edge("ClassImplements", cid["Mail"], cid["Notifier"])],
        "ClassEmbeds": [edge("ClassEmbeds", cid["FeeCalculator"], cid["Base"])],
    }
    return run("iryo", ".", nodes=nodes, edges=edges)


@pytest.fixture
def view():
    return evaluate.build_view({"iryo": _view_run()})


def _pin(qualname, kind="method", file="billing/fee.go", module=M):
    return {"qualname": qualname, "module": module, "kind": kind, "file": file}


def test_node_coverage_matches_kind_qualname_module_and_file(view):
    pins = [_pin("FeeCalculator.Calculate"), _pin("FeeCalculator", "class"),
            _pin("<package>", "class", "desk/run.go", Q), _pin("Run", file="desk/run.go", module=Q),
            _pin("Missing"), _pin("FeeCalculator", "method"), _pin("Round", file="desk/run.go"),
            _pin("Round", module="example.com/other")]
    assert evaluate.node_coverage(pins, view) == (4, 8)


def test_node_coverage_under_a_repo_directory():
    r = run("web", "web", nodes={"Files": [file_row("web", "a.go")],
                                 "Methods": [method_row("web", "a.go", "F", module=Q, package=Q)]})
    view = evaluate.build_view({"web": r})
    assert evaluate.node_coverage([_pin("F", file="web/a.go", module=Q)], view) == (1, 1)


def test_edge_rate_counts_every_non_builtin_site():
    runs = {"fx": run(files={"a.go": [
        record(1, 1, "resolved", target=target("a.go", "F")),
        record(2, 1, "external", external=external("std"), kind="defer"),
        record(3, 1, "builtin"),
        record(4, 1, "dynamic"),
        record(5, 1, "unresolved", reason="missing-module", kind="go"),
        record(6, 1, "ambiguous", candidates=[target("a.go", "G")]),
    ]})}
    assert evaluate.edge_rate(runs) == (2, 5)


def test_edge_rate_counts_entity_sites_that_lost_their_record():
    # R50: the denominator is the entity sites minus conversions, notcalls and dropped range
    # candidates, so a site without a record is unanswered instead of invisible
    recs = [record(1, 1, "resolved", target=target("a.go", "F")), record(3, 1, "builtin")]
    sites = [{"kind": "call", "name": "f", "line": n, "col": 1, "offset": n, "caller": "main"}
             for n in range(1, 8)]
    r = run(files={"a.go": recs}, entities={"a.go": {"types": [], "funcs": [{"qualname": "main",
                                                                            "sites": sites}]}})
    r.resolutions["stats"] = {"conversions": 1, "notcalls": 1, "range_dropped": 1}
    assert evaluate.edge_rate({"fx": r}) == (1, 3)     # 7 sites - 3 dropped - 1 builtin
    assert evaluate.edge_rate({}) == (0, 0)


def test_conversions_by_syntax_count_on_both_sides():
    # R53: a call of a type literal is no entity site but a file conversion, and
    # resolutions.json counts it with the conversions: the denominator adds it back
    recs = [record(1, 1, "resolved", target=target("a.go", "F")), record(3, 1, "builtin")]
    sites = [{"kind": "call", "name": "f", "line": n, "col": 1, "offset": n, "caller": "main"}
             for n in range(1, 8)]
    r = run(files={"a.go": recs}, entities={"a.go": {"types": [], "conversions": [40, 52],
                                                     "funcs": [{"qualname": "main", "sites": sites}]}})
    r.resolutions["stats"] = {"conversions": 1 + 2, "notcalls": 1, "range_dropped": 1}
    assert evaluate.edge_rate({"fx": r}) == (1, 3)     # 7 sites + 2 - 5 dropped - 1 builtin


def test_loads_must_end_ok_and_join_every_site():
    ok = run("web", groups={"m0": {"status": "ok"}})
    bad = run("api", groups={"m0": {"status": "ok"}, "m1": {"status": "error", "reason": "load failed"}})
    bad.resolutions["stats"] = {"join_misses": 2}
    got = evaluate.load_check({"web": ok, "api": bad}, all_ok=True)
    assert got == {"groups": 3, "not_ok": ["api:m1 error (load failed)"], "join_misses": 2,
                   "site_mismatch": [], "pass": False}
    assert evaluate.load_check({"web": ok}, all_ok=True)["pass"] is True
    assert evaluate.load_check({"api": bad}, all_ok=False)["pass"] is False        # join misses still fail
    bad.resolutions["stats"] = {}
    assert evaluate.load_check({"api": bad}, all_ok=False)["pass"] is True         # F2: discovery decides
    assert evaluate.load_check({"web": run("web", groups={})}, all_ok=True)["pass"] is False  # nothing loaded


def test_every_entity_site_keeps_its_record():
    # a site whose record went missing after Phase 1.6 is caught, not merely unanswered
    r = run(files={"a.go": [record(1, 1, "resolved", target=target("a.go", "F")), record(2, 1, "dynamic")]})
    assert evaluate.load_check({"fx": r}, all_ok=True)["pass"] is True
    r.resolutions["files"]["a.go"]["calls"].pop()
    got = evaluate.load_check({"fx": r}, all_ok=True)
    assert got["site_mismatch"] == ["fx: 1 call records for 2 entity sites"] and got["pass"] is False


def test_records_sit_on_entity_sites_one_each():
    # per file: a moved record and a doubled one cannot cancel out in the repo's count
    r = run(files={"a.go": [record(1, 1, "resolved", target=target("a.go", "F")), record(2, 1, "dynamic")]})
    calls = r.resolutions["files"]["a.go"]["calls"]
    calls[1] = record(1, 1, "dynamic")                         # two records at 1:1, none at 2:1
    assert evaluate.load_check({"fx": r}, all_ok=True)["site_mismatch"] == ["fx: a.go:1:1 call has two records"]
    calls[1] = record(9, 1, "dynamic")
    assert evaluate.load_check({"fx": r}, all_ok=True)["site_mismatch"] == [
        "fx: a.go:9:1 call record without an entity site"]


# ── in-repo imports: every resolved record has its PackageImports row ──


IMPORT_ENTITIES = {
    "app/main.go": entity("example.com/fx/app", "main", origin="command"),
    "tools/gen.go": entity("example.com/fx/tools", "main", origin="command", included=False),
    "lib/lib.go": entity("example.com/fx/lib", "lib"),
    "util/util.go": entity("example.com/fx/util", "util"),
    "two/a.go": entity("example.com/fx/two", "two"),
    "two/b.go": entity("example.com/fx/two", "other"),
    "gen/gen.go": entity("example.com/fx/gen", "gen"),
    "gen/make.go": entity("example.com/fx/gen", "main", origin="command", included=False),
    "gen/gen_ext_test.go": entity("example.com/fx/gen_test", "gen_test", origin="xtest"),
}


def _import_run(name: str = "fx", directory: str = ".") -> evaluate.RepoRun:
    """app/main.go imports lib (row present), util (record without a clause),
    two (two clauses), gen (a library beside an ignored `package main`
    generator, row missing) and gone (no such package), plus a peer, a
    stdlib and an unresolved import; tools/gen.go, which only an extra build
    compiles, imports gen, where both clauses count."""
    main_id = pipeline._make_id("file", name, "app/main.go", "")
    nodes = {"Packages": [package_row(name, d, c) for d, c in
                          (("lib", "lib"), ("util", "util"), ("two", "two"), ("two", "other"),
                           ("gen", "gen"), ("gen", "main"))]}
    edges = {"PackageImports": [edge("PackageImports", main_id,
                                     pipeline._make_id("package", name, "lib", "lib"))]}
    imports = {
        "app/main.go": [
            import_record(3, 2, "example.com/fx/lib", directory="lib", clause="lib"),
            import_record(4, 2, "example.com/fx/util", directory="util", name="util"),
            import_record(5, 2, "example.com/fx/two", directory="two", clause="two"),
            import_record(6, 2, "example.com/fx/gen", directory="gen", clause="gen"),
            import_record(7, 2, "example.com/fx/gone", directory="gone", clause="gone"),
            import_record(8, 2, "example.com/peer/x", status="external", directory="x", clause="x",
                          repo="peer", external=external("example.com/peer", peer=True)),
            import_record(9, 2, "fmt", status="external", external=external("std")),
            import_record(10, 2, "example.com/dep/y", status="unresolved"),
        ],
        "tools/gen.go": [import_record(3, 2, "example.com/fx/gen", directory="gen", clause="gen")],
    }
    return run(name, directory, entities=IMPORT_ENTITIES, nodes=nodes, edges=edges, imports=imports)


def test_imports_check_finds_resolved_records_without_a_row():
    got = evaluate.imports_check({"fx": _import_run()})
    assert (got["records"], got["mapped"], got["unmapped"], got["ambiguous"]) == (6, 1, 3, 2)
    assert got["pass"] is False
    assert got["unmapped_examples"] == [
        "app/main.go:4:2 example.com/fx/util: the record names no clause",
        "app/main.go:6:2 example.com/fx/gen: no PackageImports row to gen|gen",
        "app/main.go:7:2 example.com/fx/gone: no Packages node gone|gone",
    ]
    assert got["ambiguous_examples"] == [
        "app/main.go:5:2 example.com/fx/two: two has clauses other, two",
        "tools/gen.go:3:2 example.com/fx/gen: gen has clauses gen, main",
    ]


def test_imports_check_passes_when_every_record_has_its_row():
    r = _import_run()
    r.resolutions["files"]["app/main.go"]["imports"][1:5] = []
    r.resolutions["files"]["tools/gen.go"]["imports"] = []
    assert evaluate.imports_check({"fx": r}) == {
        "records": 1, "mapped": 1, "unmapped": 0, "ambiguous": 0,
        "unmapped_examples": [], "ambiguous_examples": [], "pass": True}
    assert evaluate.imports_check({})["pass"] is False          # an empty denominator fails (R50)


def test_imports_check_labels_files_under_the_repo_directory():
    got = evaluate.imports_check({"web": _import_run("web", "web")})
    assert got["records"] == 6 and got["mapped"] == 1
    assert got["unmapped_examples"][0].startswith("web/app/main.go:4:2 ")


@pytest.mark.parametrize("check, expected, match, ok", [
    ({"type": "node_exists", "subject": f"{P}.FeeCalculator.Calculate.func1"}, ["exists"], "equals", True),
    ({"type": "node_exists", "subject": f"{P}.Nope"}, ["exists"], "equals", False),
    ({"type": "class_file", "subject": f"{P}.FeeCalculator"}, ["billing/fee.go"], "equals", True),
    ({"type": "methods_of", "subject": f"{P}.FeeCalculator"}, ["Calculate", "Scaled"], "exact_set", True),
    ({"type": "callees_of", "subject": f"{Q}.Run"},
     [f"{P}.FeeCalculator.Calculate", f"{P}.Notifier.Send"], "exact_set", True),
    ({"type": "callers_of", "subject": f"{P}.FeeCalculator.Calculate"}, [f"{Q}.Run"], "exact_set", True),
    ({"type": "possibly_callees_of", "subject": f"{Q}.Run"}, [f"{P}.Mail.Send"], "exact_set", True),
    ({"type": "implementers_of", "subject": f"{P}.Notifier"}, [f"{P}.Mail"], "exact_set", True),
    ({"type": "embeds_of", "subject": f"{P}.FeeCalculator"}, [f"{P}.Base"], "exact_set", True),
    ({"type": "cross_module_callees_of", "subject": f"{Q}.Run"},
     [f"{P}.FeeCalculator.Calculate", f"{P}.Notifier.Send"], "exact_set", True),
    ({"type": "cross_module_callees_of", "subject": f"{P}.FeeCalculator.Calculate"}, [], "exact_set", True),
    ({"type": "callees_of", "subject": f"{Q}.Run"}, [f"{P}.FeeCalculator.Calculate"], "contains_all", True),
    ({"type": "callees_of", "subject": f"{Q}.Run"}, [f"{P}.FeeCalculator.Calculate"], "exact_set", False),
    ({"type": "callees_of", "subject": f"{Q}.Run"}, 2, "count_at_least", True),
    ({"type": "callees_of", "subject": f"{Q}.Run"}, [3], "count_at_least", False),
    ({"type": "no_such_check", "subject": "x"}, [], "exact_set", False),
])
def test_qa_checks(view, check, expected, match, ok):
    got, actual = evaluate.answer_question({"check": check, "expected": expected, "match": match}, view)
    assert got is ok, actual


def test_qa_rate_reads_the_fixture_file(tmp_path, view):
    gt = tmp_path / "ground_truth"
    gt.mkdir()
    (gt / "qa_questions.json").write_text(json.dumps({"questions": [
        {"id": "QA-1", "question": "q", "check": {"type": "class_file", "subject": f"{P}.FeeCalculator"},
         "expected": ["billing/fee.go"], "match": "equals"},
        {"id": "QA-2", "question": "q", "check": {"type": "class_file", "subject": f"{P}.Nope"},
         "expected": ["x.go"], "match": "equals"}]}), encoding="utf-8")
    qa = evaluate.qa_rate(str(tmp_path), view)
    assert (qa["value"], qa["pass"]) == (0.5, False)
    assert [m["id"] for m in qa["misses"]] == ["QA-2"]
    assert evaluate.qa_rate(str(tmp_path / "none"), view) == {"value": 0.0, "pass": False, "misses": []}


def test_the_real_f1_questions_have_known_check_types():
    qs = json.loads((TEST_CODES / "iryo-kanri-system" / "ground_truth" / "qa_questions.json")
                    .read_text(encoding="utf-8"))["questions"]
    empty = evaluate.build_view({})
    for q in qs:
        _ok, actual = evaluate.answer_question(q, empty)
        assert not actual.startswith("(unknown check type"), q["id"]


# ═══════════════════════════════════════════════════════ F3 cross-repo edge set


def _xrepo_entities():
    return {
        "api": {"notify/mail/mail.go": {"package_name": "mail", "origin": "package",
                                        "types": [{"qualname": "<package>"}],
                                        "funcs": [{"qualname": "<init>"}]},
                "service/billing/billing.go": {"package_name": "billing", "origin": "package",
                                               "types": [{"qualname": "AuditedInvoice"}], "funcs": []}},
        "shared": {"log/logger.go": {"package_name": "log", "origin": "package",
                                     "types": [{"qualname": "Logger"}], "funcs": [{"qualname": "New"}]},
                   "contracts/audit.go": {"package_name": "contracts", "origin": "package",
                                          "types": [{"qualname": "Audit"}], "funcs": []}},
    }


EXPECTED_XREPO = [
    {"table": "CrossRepoCalls", "kind": None, "source": "api:notify/mail/mail.go:<init>",
     "target": "shared:log/logger.go:New"},
    {"table": "CrossRepoImports", "kind": None, "source": "api:notify/mail/mail.go",
     "target": "shared:log|log", "import_path": "example.com/iryo/shared/log"},
    {"table": "CrossRepoRef", "kind": "embeds", "source": "api:service/billing/billing.go:AuditedInvoice",
     "target": "shared:contracts/audit.go:Audit"},
]


def _derived(edges=EXPECTED_XREPO) -> dict:
    out = {"CrossRepoCalls": [], "CrossRepoImports": [], "CrossRepoRef": []}
    for e in edges:
        sid, did = evaluate.label_id(e["table"], "source", e["source"]), \
            evaluate.label_id(e["table"], "target", e["target"])
        if e["table"] == "CrossRepoCalls":
            out["CrossRepoCalls"].append({"caller_method": sid, "callee_method": did})
        elif e["table"] == "CrossRepoImports":
            out["CrossRepoImports"].append({"source_file": sid, "target_package": did,
                                            "import_path": e["import_path"]})
        else:
            out["CrossRepoRef"].append({"source_class": sid, "target_class": did, "kind": e["kind"]})
    return out


def _class_id(rel: str, qualname: str, repo: str = "iryo") -> str:
    return pipeline._make_id("class", repo, rel, qualname)


CLASS_EDGES = [
    {"table": "ClassImplements", "source": "iryo:kernel/notify.go:Mail", "target": "iryo:kernel/notify.go:Notifier",
     "pointer_receiver": False},
    {"table": "ClassEmbeds", "source": "iryo:domain/visit.go:Visit", "target": "iryo:domain/entity.go:Entity",
     "pointer": True},
    {"table": "ClassImplements", "source": "iryo:kernel/notify.go:Sms", "target": "iryo:ext/writer.go:Writer",
     "pointer_receiver": True, "modes": ["env"]},
]


def _class_rows(*, drop=(), extra=(), pointer=True) -> dict:
    rows = {"ClassImplements": [{"class_id": _class_id("kernel/notify.go", "Mail"),
                                 "interface_id": _class_id("kernel/notify.go", "Notifier"), "pointer_receiver": False},
                                {"class_id": _class_id("kernel/notify.go", "Sms"),
                                 "interface_id": _class_id("ext/writer.go", "Writer"), "pointer_receiver": True}],
            "ClassEmbeds": [{"child_class": _class_id("domain/visit.go", "Visit"),
                             "embedded_class": _class_id("domain/entity.go", "Entity"), "pointer": pointer}]}
    for table, i in drop:
        rows[table].pop(i)
    for table, row in extra:
        rows[table].append(row)
    return rows


def test_class_edges_are_an_exact_set_per_mode():
    labels = {_class_id("domain/visit.go", "Visit"): "iryo:domain/visit.go:Visit",
              _class_id("domain/entity.go", "Entity"): "iryo:domain/entity.go:Entity",
              _class_id("kernel/notify.go", "Mail"): "iryo:kernel/notify.go:Mail"}
    env = {"iryo": run("iryo", edges=_class_rows())}
    assert evaluate.class_edge_check(CLASS_EDGES, env, labels, "env") == {
        "expected": 3, "actual": 3, "missing": [], "extra": [], "pass": True}
    offline = {"iryo": run("iryo", edges=_class_rows(drop=[("ClassImplements", 1)]))}
    assert evaluate.class_edge_check(CLASS_EDGES, offline, labels, "offline")["pass"] is True   # env-only entry
    got = evaluate.class_edge_check(CLASS_EDGES, env, labels, "offline")
    assert got["pass"] is False and got["missing"] == [] and len(got["extra"]) == 1          # Sms: env only
    flipped = {"iryo": run("iryo", edges=_class_rows(pointer=False))}
    got = evaluate.class_edge_check(CLASS_EDGES, flipped, labels, "env")
    assert got["missing"] == ["ClassEmbeds iryo:domain/visit.go:Visit -> iryo:domain/entity.go:Entity (pointer=true)"]
    assert got["extra"] == ["ClassEmbeds iryo:domain/visit.go:Visit -> iryo:domain/entity.go:Entity (pointer=false)"]
    wrong = {"iryo": run("iryo", edges=_class_rows(drop=[("ClassImplements", 0)], extra=[(
        "ClassImplements", {"class_id": _class_id("kernel/notify.go", "Mail"),
                            "interface_id": _class_id("ext/writer.go", "Writer"), "pointer_receiver": False})]))}
    got = evaluate.class_edge_check(CLASS_EDGES, wrong, labels, "env")
    assert got["missing"] == ["ClassImplements iryo:kernel/notify.go:Mail -> iryo:kernel/notify.go:Notifier "
                              "(pointer_receiver=false)"] and len(got["extra"]) == 1 and got["pass"] is False


def test_label_ids_follow_make_id():
    assert evaluate.label_id("CrossRepoCalls", "source", "api:a.go:F") == \
        pipeline._make_id("method", "api", "a.go", "F")
    assert evaluate.label_id("CrossRepoRef", "target", "shared:c/a.go:T") == \
        pipeline._make_id("class", "shared", "c/a.go", "T")
    assert evaluate.label_id("CrossRepoImports", "source", "api:a/b.go") == \
        pipeline._make_id("file", "api", "a/b.go", "")
    assert evaluate.label_id("CrossRepoImports", "target", "shared:log|log") == \
        pipeline._make_id("package", "shared", "log", "log")
    assert evaluate.label_id("CrossRepoImports", "target", "shared:|shared") == \
        pipeline._make_id("package", "shared", "", "shared")


def test_cross_repo_set_is_exact():
    labels = evaluate.node_labels(_xrepo_entities())
    ok = evaluate.cross_repo_check(EXPECTED_XREPO, _derived(), labels)
    assert ok == {"expected": 3, "actual": 3, "missing": [], "extra": [], "pass": True}
    wrong_import = copy.deepcopy(EXPECTED_XREPO)
    wrong_import[1]["import_path"] = "example.com/iryo/shared/log/v2"
    extra = EXPECTED_XREPO + [{"table": "CrossRepoRef", "kind": "implements",
                               "source": "api:service/billing/billing.go:AuditedInvoice",
                               "target": "shared:log/logger.go:Logger"}]
    got = evaluate.cross_repo_check(EXPECTED_XREPO, _derived(wrong_import[:2] + extra[2:]), labels)
    assert got["pass"] is False and got["actual"] == 4
    assert got["missing"] == ["CrossRepoImports api:notify/mail/mail.go -> shared:log|log "
                              "(example.com/iryo/shared/log)"]
    assert got["extra"] == [
        "CrossRepoImports api:notify/mail/mail.go -> shared:log|log (example.com/iryo/shared/log/v2)",
        "CrossRepoRef[implements] api:service/billing/billing.go:AuditedInvoice -> "
        "shared:log/logger.go:Logger"]


def test_the_real_f3_edge_labels_parse():
    rows = json.loads((TEST_CODES / "multi_repo" / "ground_truth" / "manifest.json")
                      .read_text(encoding="utf-8"))["cross_repo_edges"]
    got = evaluate.cross_repo_check(rows, _derived(rows), {})
    assert got["pass"] and got["expected"] == len(rows) == got["actual"]


# ═══════════════════════════════════════════════════════ F2 discovery facts

ROOT = "/repo/ws"


def _discovery(**over) -> Discovery:
    app = GoUnit(key="example.com/layouts/ws/app", root="app", go="1.26", toolchain=None,
                 requires=(("example.com/thirdparty/textfmt", "v1.0.0", False),),
                 replaces=(Replace("example.com/outside/extlib", "", "/repo/_outside/extlib", "", True),),
                 vendor=False, in_repo_deps=("example.com/layouts/ws/lib",))
    lib = GoUnit(key="example.com/layouts/ws/lib", root="lib", go="1.26", toolchain=None, requires=(),
                 replaces=(), vendor=False, in_repo_deps=())
    ws = Group(id="ws0", kind="workspace", dir="/out/ctl", modules=[app.key, lib.key], gowork="/out/go.work",
               modfile=None, vendor=False, degraded=False, status="ok", pattern="work",
               missing_modules=["example.com/thirdparty/textfmt@v1.0.0"], warnings=["textfmt is missing"],
               source=f"{ROOT}/go.work")
    fields = dict(repo="ws", repo_root=ROOT, units=[app, lib],
                  unit_of_file={"app/main.go": app.key, "lib/l.go": lib.key}, groups=[ws],
                  builds=[BuildConfig("linux-amd64", "linux", "amd64", (), False, True)],
                  toolchain="go1.27.2", mode="offline", module_source="none", peers=[],
                  skipped={"testdata": 1, "vendor": 0}, warnings=["group ws0: textfmt is missing"])
    fields.update(over)
    return Discovery(**fields)


def _expected() -> dict:
    return {
        "units": {
            "example.com/layouts/ws/app": {"root": "app", "go": "1.26", "toolchain": None, "vendor": False,
                                           "group": "workspace:.",
                                           "requires": [["example.com/thirdparty/textfmt", "v1.0.0", False]],
                                           "replaces": [{"old": "example.com/outside/extlib", "old_version": "",
                                                         "new": "../_outside/extlib", "new_version": "",
                                                         "local": True}],
                                           "in_repo_deps": ["example.com/layouts/ws/lib"]},
            "example.com/layouts/ws/lib": {"root": "lib", "go": "1.26", "toolchain": None, "vendor": False,
                                           "group": "workspace:.", "requires": [], "replaces": [],
                                           "in_repo_deps": []}},
        "groups": {"workspace:.": {"kind": "workspace", "modules": ["example.com/layouts/ws/lib",
                                                                    "example.com/layouts/ws/app"],
                                   "vendor": False, "degraded": False, "status": "ok", "pattern": "work",
                                   "warnings_contain": ["textfmt"]}},
        "files": {"app/main.go": "example.com/layouts/ws/app", "lib/l.go": "example.com/layouts/ws/lib",
                  "loose.go": None},
        "scanned_files": 3, "not_scanned": ["testdata/t.go"], "skipped": {"testdata": 1},
        "warnings_contain": ["textfmt"],
        "modes": {"offline": {"module_source": "none",
                              "missing_modules": {"workspace:.": ["example.com/thirdparty/textfmt@v1.0.0"]}},
                  "env": {"module_source": "proxy", "missing_modules": {},
                          "status_after_load": {"workspace:.": "error"}}},
    }


SCANNED = ["app/main.go", "lib/l.go", "loose.go"]


def test_matching_discovery_has_no_problems():
    assert evaluate.discovery_problems(_expected(), _discovery(), SCANNED, {"ws0": {"status": "ok"}},
                                       "offline") == []


def test_each_group_keeps_its_discovery_status_after_the_load():
    # R50: unless status_after_load names this mode's status for the group
    disc = _discovery()
    assert evaluate.discovery_problems(_expected(), disc, SCANNED, {"ws0": {"status": "error"}},
                                       "offline") == ["status after load of workspace:.: error, expected ok"]
    assert evaluate.discovery_problems(_expected(), disc, SCANNED, {}, "offline") == [
        "status after load of workspace:.: None, expected ok"]


def test_env_mode_compares_its_own_facts():
    disc = _discovery(mode="download", module_source="proxy")
    disc.groups[0].missing_modules = []
    groups = {"ws0": {"status": "error"}}
    assert evaluate.discovery_problems(_expected(), disc, SCANNED, groups, "env") == []
    assert evaluate.discovery_problems(_expected(), disc, SCANNED, {"ws0": {"status": "ok"}}, "env") == [
        "status after load of workspace:.: ok, expected error"]


@pytest.mark.parametrize("change, problem", [
    (lambda d, s: d.units.pop(), "unit example.com/layouts/ws/lib: missing"),
    (lambda d, s: d.units.append(GoUnit("example.com/x", "x", None, None, (), (), False, ())),
     "unit example.com/x: not expected"),
    (lambda d, s: d.groups[0].__setattr__("status", "toolchain"),
     "group workspace:. status: toolchain, expected ok"),
    (lambda d, s: d.groups[0].__setattr__("source", f"{ROOT}/sub/go.work"),
     "group workspace:sub: not expected"),
    (lambda d, s: d.groups[0].__setattr__("warnings", []),
     "group workspace:. warnings lack 'textfmt'"),
    (lambda d, s: d.unit_of_file.__setitem__("loose.go", "example.com/layouts/ws/app"),
     "file loose.go: owner example.com/layouts/ws/app, expected none"),
    (lambda d, s: s.append("testdata/t.go"), "testdata/t.go: scanned, expected not"),
    (lambda d, s: d.skipped.__setitem__("dot", 2), "skipped: {'dot': 2, 'testdata': 1}, expected {'testdata': 1}"),
    (lambda d, s: d.__setattr__("module_source", "file-proxy"),
     "module source: file-proxy, expected none"),
    (lambda d, s: d.groups[0].missing_modules.clear(),
     "missing modules of workspace:.: [], expected ['example.com/thirdparty/textfmt@v1.0.0']"),
    (lambda d, s: d.__setattr__("warnings", []), "warnings lack 'textfmt'"),
])
def test_each_discovery_fact_is_checked(change, problem):
    disc, scanned = _discovery(), list(SCANNED)
    change(disc, scanned)
    assert problem in evaluate.discovery_problems(_expected(), disc, scanned, {}, "offline")


def test_unit_fields_are_compared_one_by_one():
    disc = _discovery()
    app = disc.units[0]
    disc.units[0] = GoUnit(app.key, app.root, "1.25", app.toolchain, (), app.replaces, True, ())
    problems = evaluate.discovery_problems(_expected(), disc, SCANNED, {}, "offline")
    assert "unit example.com/layouts/ws/app go: 1.25, expected 1.26" in problems
    assert "unit example.com/layouts/ws/app vendor: True, expected False" in problems
    assert any(p.startswith("unit example.com/layouts/ws/app requires:") for p in problems)
    assert any(p.startswith("unit example.com/layouts/ws/app in_repo_deps:") for p in problems)


def test_module_groups_are_keyed_by_their_module_root():
    unit = GoUnit("example.com/layouts/extra", "extra", "1.26", None, (), (), False, ())
    g = Group(id="m0", kind="module", dir=f"{ROOT}/extra", modules=[unit.key], gowork=None,
              modfile="/out/go.mod", vendor=False, degraded=False, status="ok", pattern="./...",
              source=f"{ROOT}/extra/go.mod")
    assert evaluate.group_key(g, ROOT) == "module:extra"
    g.source = f"{ROOT}/go.mod"
    assert evaluate.group_key(g, ROOT) == "module:."


def test_the_real_expected_units_cover_every_layout():
    doc = json.loads((TEST_CODES / "go_layouts" / "ground_truth" / "expected_units.json")
                     .read_text(encoding="utf-8"))
    m = json.loads((TEST_CODES / "go_layouts" / "ground_truth" / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(doc["repos"]) == sorted(r["name"] for r in m["repos"])
    assert evaluate.discovery_problems(doc["repos"]["ws"], None, [], {}, "offline") == [
        "no discovery (the pipeline did not run Phase 1)"]
