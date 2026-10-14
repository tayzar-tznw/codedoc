"""Pure graph builders (spec §6, C6): node rows per kind, the node index and the
edge derivation rules of §6.3, on synthetic entities (C3) and resolution
records (C5)."""
from __future__ import annotations

import copy
import json

import pytest

from graph_generator import config, graph_rows, pipeline
from graph_generator.pipeline import _edge_id
from graph_generator.setup_spanner_graph import EDGE_SPEC, NODE_SPEC, write_columns
from tests.graph_world import (BAD, BASE, BILLING, EDGE_TABLES, EXTRA, FEE, FEE_TEST, KDOC,
                               MAIN, MOD, NODE_TABLES, REPO, XTEST, call, cand, cid, did,
                               embed_rec, entities, fid, fn, imp, implements_rec, import_rec,
                               make_data, mid, pid, record, recv, tgt)


@pytest.fixture
def data():
    return make_data()


@pytest.fixture
def nodes(data):
    return graph_rows.build_node_rows(data)


@pytest.fixture
def edges(data):
    return graph_rows.derive_edge_rows(data, graph_rows.node_index(data))


def by_id(rows, key):
    return {r[key]: r for r in rows}


def pairs(rows, src, dst):
    return {(r[src], r[dst]) for r in rows}


# ── node rows ───────────────────────────────────────────────────────

def test_node_rows_have_the_five_phase8_tables(nodes):
    assert list(nodes) == list(NODE_TABLES)
    assert set(NODE_TABLES) <= set(NODE_SPEC)


@pytest.mark.parametrize("table", NODE_TABLES)
def test_every_node_row_has_exactly_the_write_columns_in_order(nodes, table):
    assert nodes[table]
    for row in nodes[table]:
        assert list(row) == write_columns(table)


def test_files_rows_carry_the_c3_fields(nodes):
    files = by_id(nodes["Files"], "file_id")
    assert set(files) == {fid(rel) for rel in [*entities(), BAD]}
    fee = files[fid(FEE)]
    assert fee == {
        "file_id": fid(FEE), "file_name": "fee.go", "extension": ".go",
        "directory": "libs/billing", "path": FEE, "origin": "package", "generated": False,
        "package": BILLING, "package_name": "billing", "module": MOD, "build_constraint": "",
        "build_included": True, "summary": "Package billing computes fees.", "repo": REPO}
    assert files[fid(XTEST)]["origin"] == "xtest"
    assert files[fid(XTEST)]["package"] == f"{BILLING}_test"
    assert files[fid(EXTRA)]["summary"] == ""


def test_file_flags_are_python_bools(data):
    ents = data.extracted_entities
    ents[EXTRA].update(generated=True, build_included=False, build_constraint="windows")
    files = by_id(graph_rows.build_node_rows(data)["Files"], "file_id")
    extra = files[fid(EXTRA)]
    assert extra["generated"] is True and extra["build_included"] is False
    assert extra["build_constraint"] == "windows"


def test_a_file_without_a_record_is_a_bare_file_node(nodes):
    bad = by_id(nodes["Files"], "file_id")[fid(BAD)]
    assert bad["path"] == BAD and bad["directory"] == "tools" and bad["file_name"] == "bad.go"
    assert (bad["origin"], bad["package"], bad["package_name"], bad["module"]) == ("", "", "", "")
    assert bad["generated"] is False and bad["build_included"] is False
    assert not [c for c in nodes["Classes"] if c["file_id"] == fid(BAD)]


def test_packages_are_keyed_by_directory_and_clause(nodes):
    packages = by_id(nodes["Packages"], "package_id")
    assert set(packages) == {pid("libs/billing", "billing"), pid("libs/billing", "billing_test"),
                             pid("cmd/reception", "main"), pid("libs/kernel", "kernel")}
    billing = packages[pid("libs/billing", "billing")]
    assert billing == {
        "package_id": pid("libs/billing", "billing"), "import_path": BILLING, "name": "billing",
        "directory": "libs/billing", "module": MOD, "origin": "package", "file_count": 4,
        "summary": "Package billing computes fees.", "repo": REPO}
    xtest = packages[pid("libs/billing", "billing_test")]
    assert (xtest["origin"], xtest["file_count"], xtest["import_path"]) == (
        "xtest", 1, f"{BILLING}_test")
    assert packages[pid("cmd/reception", "main")]["origin"] == "command"
    assert packages[pid("libs/kernel", "kernel")]["summary"] == "Package kernel holds shared types."


def test_package_origin_and_summary_prefer_non_test_files(data):
    ents = data.extracted_entities
    ents[FEE_TEST]["types"][0]["doc"] = "Test helpers.\n"
    ents[BASE]["types"][0]["doc"] = "Second package comment.\n"
    packages = by_id(graph_rows.build_node_rows(data)["Packages"], "package_id")
    assert packages[pid("libs/billing", "billing")]["summary"] == (
        "Second package comment.\n\nPackage billing computes fees.")  # relpath order
    only_tests = make_data({FEE_TEST: record(origin="test", doc="Test helpers.\n")}, {})
    row = graph_rows.build_node_rows(only_tests)["Packages"][0]
    assert (row["origin"], row["summary"]) == ("test", "Test helpers.")


def test_classes_include_one_package_pseudo_type_per_file(nodes):
    classes = by_id(nodes["Classes"], "class_id")
    pseudo = [c for c in nodes["Classes"] if c["kind"] == "package"]
    assert len(pseudo) == len(entities())  # every file record, none for BAD
    fee_pkg = classes[cid(FEE, "<package>")]
    assert (fee_pkg["name"], fee_pkg["qualname"], fee_pkg["fqcn"]) == (
        "<package>", "<package>", f"{BILLING}.<package>")
    assert fee_pkg["summary"] == "Package billing computes fees."
    calc = classes[cid(FEE, "FeeCalculator")]
    assert calc == {
        "class_id": cid(FEE, "FeeCalculator"), "name": "FeeCalculator",
        "qualname": "FeeCalculator", "package": BILLING, "fqcn": f"{BILLING}.FeeCalculator",
        "file_id": fid(FEE), "kind": "struct", "type_params": "",
        "embeds": '["Base", "*sync.Mutex"]', "start_line": 5, "end_line": 7, "origin": "package",
        "module": MOD, "summary": "FeeCalculator computes fees.", "repo": REPO}
    assert json.loads(calc["embeds"]) == ["Base", "*sync.Mutex"]
    assert classes[cid(FEE, "Totaler")]["kind"] == "interface"
    assert classes[cid(FEE, "Totaler")]["embeds"] == "[]"
    assert classes[cid(FEE, "Pair")]["type_params"] == "[K comparable, V any]"


def test_methods_rows_cover_every_member_kind(nodes):
    methods = by_id(nodes["Methods"], "method_id")
    kinds = {(m["file_id"], m["qualname"]): m["member_kind"] for m in nodes["Methods"]}
    assert kinds[(fid(FEE), "FeeCalculator.Calculate")] == "method"
    assert kinds[(fid(FEE), "Totaler.Total")] == "interface_method"
    assert kinds[(fid(FEE), "New")] == "func"
    assert kinds[(fid(FEE), "New.func1.func1")] == "closure"
    assert kinds[(fid(FEE), "init#1")] == "init"
    assert kinds[(fid(FEE), "<init>")] == "var_init"
    calc = methods[mid(FEE, "FeeCalculator.Calculate")]
    assert calc == {
        "method_id": mid(FEE, "FeeCalculator.Calculate"), "name": "Calculate",
        "class_id": cid(FEE, "FeeCalculator"), "file_id": fid(FEE),
        "qualname": "FeeCalculator.Calculate", "fqmn": f"{BILLING}.FeeCalculator.Calculate",
        "signature": "func (c *FeeCalculator) Calculate(p int) int", "member_kind": "method",
        "receiver": "*FeeCalculator", "type_params": "", "has_body": True, "start_line": 14,
        "end_line": 17, "origin": "package", "module": MOD, "entry_point": "",
        "summary": "Calculate returns the fee.", "repo": REPO}
    assert methods[mid(FEE, "FeeCalculator.Total")]["receiver"] == "FeeCalculator"
    total = methods[mid(FEE, "Totaler.Total")]
    assert (total["class_id"], total["has_body"], total["receiver"], total["signature"],
            total["summary"], total["start_line"]) == (
        cid(FEE, "Totaler"), False, "", "Total() int", "Total sums.", 11)


def test_owner_rule_for_functions_closures_and_pseudo_functions(nodes):
    owner = {(m["file_id"], m["qualname"]): m["class_id"] for m in nodes["Methods"]}
    package = cid(FEE, "<package>")
    for q in ("New", "New.func1", "New.func1.func1", "init#1", "<init>", "<init>.func1"):
        assert owner[(fid(FEE), q)] == package, q
    # A closure belongs to the owner of its outermost enclosing declaration.
    assert owner[(fid(EXTRA), "FeeCalculator.Round.func1")] == cid(FEE, "FeeCalculator")


def test_a_method_outside_its_types_file_keeps_its_file_and_points_at_the_type(nodes, edges):
    round_ = by_id(nodes["Methods"], "method_id")[mid(EXTRA, "FeeCalculator.Round")]
    assert round_["file_id"] == fid(EXTRA)
    assert round_["class_id"] == cid(FEE, "FeeCalculator")
    assert (cid(FEE, "FeeCalculator"), mid(EXTRA, "FeeCalculator.Round")) in pairs(
        edges["ClassDefinesMethod"], "class_id", "method_id")
    defined = pairs(edges["FileDefinesClass"], "file_id", "class_id")
    assert (fid(FEE), cid(FEE, "FeeCalculator")) in defined
    assert not any(c == cid(FEE, "FeeCalculator") for f, c in defined if f == fid(EXTRA))


def test_unresolved_receivers_attach_to_their_files_package(nodes):
    ghost = by_id(nodes["Methods"], "method_id")[mid(EXTRA, "Ghost.Do")]
    assert ghost["class_id"] == cid(EXTRA, "<package>")
    assert ghost["receiver"] == "*Ghost" and ghost["member_kind"] == "method"


def test_a_receiver_type_missing_from_the_graph_falls_back_to_the_package(data):
    data.extracted_entities[EXTRA]["funcs"].append(
        fn("Elsewhere.M", "method", 30, recv("Elsewhere", "unscanned/x.go")))
    methods = by_id(graph_rows.build_node_rows(data)["Methods"], "method_id")
    assert methods[mid(EXTRA, "Elsewhere.M")]["class_id"] == cid(EXTRA, "<package>")


def test_build_twins_are_two_methods_of_one_type(data):
    twin = "libs/billing/fee_windows.go"
    data.file_hashes[twin] = "sha-twin"
    data.extracted_entities[twin] = {**record(build_included=False, build_constraint="windows",
                                              funcs=[fn("FeeCalculator.Calculate", "method", 3,
                                                        recv("FeeCalculator", FEE))]),
                                     "sha256": "sha-twin"}
    methods = graph_rows.build_node_rows(data)["Methods"]
    twins = [m for m in methods if m["qualname"] == "FeeCalculator.Calculate"]
    assert {m["file_id"] for m in twins} == {fid(FEE), fid(twin)}
    assert {m["class_id"] for m in twins} == {cid(FEE, "FeeCalculator")}


def test_entry_point_is_the_command_directory_name(nodes):
    eps = {m["qualname"]: m["entry_point"] for m in nodes["Methods"] if m["entry_point"]}
    assert eps == {"main": "reception"}


def test_root_command_entry_point_is_the_repo_directory_name():
    data = make_data({"main.go": record(package=MOD, name="main", origin="command",
                                        funcs=[fn("main", line=3)])}, {})
    (row,) = [m for m in graph_rows.build_node_rows(data)["Methods"] if m["qualname"] == "main"]
    assert row["entry_point"] == "iryo"


def test_directories_cover_the_root_and_every_ancestor(nodes):
    dirs = by_id(nodes["Directories"], "dir_id")
    expected = {"": "iryo", "cmd": "cmd", "cmd/reception": "reception", "libs": "libs",
                "libs/billing": "billing", "libs/kernel": "kernel", "tools": "tools"}
    assert {d: dirs[did(d)]["name"] for d in expected} == expected
    assert len(dirs) == len(expected)
    assert all(r["summary"] == "" and r["repo"] == REPO for r in nodes["Directories"])


def test_node_ids_are_unique_across_tables(nodes):
    ids = [r[NODE_SPEC[t][0]] for t in NODE_TABLES for r in nodes[t]]
    assert len(ids) == len(set(ids))


def test_a_stale_entity_record_gives_no_declarations(data):
    data.extracted_entities[BASE]["sha256"] = "an older hash"
    nodes = graph_rows.build_node_rows(data)
    assert fid(BASE) in {r["file_id"] for r in nodes["Files"]}
    assert not [c for c in nodes["Classes"] if c["file_id"] == fid(BASE)]
    assert not [m for m in nodes["Methods"] if m["file_id"] == fid(BASE)]
    idx = graph_rows.node_index(data)
    assert idx["stats"]["entities_stale"] == 1


def test_an_entity_record_of_an_unscanned_file_is_ignored(data):
    data.extracted_entities["gone/old.go"] = {**record(funcs=[fn("Old")]), "sha256": "x"}
    nodes = graph_rows.build_node_rows(data)
    assert "gone/old.go" not in {r["path"] for r in nodes["Files"]}
    assert "Old" not in {m["qualname"] for m in nodes["Methods"]}


def test_a_repeated_qualname_is_written_once(data):
    data.extracted_entities[BASE]["funcs"].append(fn("Base.ID", "method", 40,
                                                     recv("Base", BASE, pointer=False)))
    methods = [m for m in graph_rows.build_node_rows(data)["Methods"]
               if m["method_id"] == mid(BASE, "Base.ID")]
    assert len(methods) == 1 and methods[0]["start_line"] == 6  # the first one


def test_a_record_without_the_pseudo_type_still_owns_its_functions(data):
    ent = data.extracted_entities[BASE]
    ent["types"] = [t for t in ent["types"] if t["kind"] != "package"]
    nodes = graph_rows.build_node_rows(data)
    pkg = by_id(nodes["Classes"], "class_id")[cid(BASE, "<package>")]
    assert pkg["kind"] == "package" and pkg["file_id"] == fid(BASE)


def test_no_module_file_uses_the_bare_qualname():
    data = make_data({"loose.go": {**record(package="", name="main", origin="command",
                                            funcs=[fn("helper")]), "module": ""}}, {})
    nodes = graph_rows.build_node_rows(data)
    (helper,) = [m for m in nodes["Methods"] if m["qualname"] == "helper"]
    assert helper["fqmn"] == "helper" and helper["module"] == ""


# ── edge rows ───────────────────────────────────────────────────────

def test_edge_rows_have_the_ten_phase9_tables(edges):
    assert list(edges) == list(EDGE_TABLES)
    assert set(EDGE_TABLES) <= set(EDGE_SPEC)


@pytest.mark.parametrize("table", EDGE_TABLES)
def test_every_edge_row_has_exactly_the_write_columns_in_order(edges, table):
    assert edges[table]
    for row in edges[table]:
        assert list(row) == write_columns(table)


@pytest.mark.parametrize("table", EDGE_TABLES)
def test_edge_ids_hash_table_source_and_target(edges, table):
    _pk, _cols, (scol, _st), (dcol, _dt) = EDGE_SPEC[table]
    for row in edges[table]:
        assert row["edge_id"] == _edge_id(table, row[scol], row[dcol])
    assert len({r["edge_id"] for r in edges[table]}) == len(edges[table])


def test_method_calls_come_only_from_resolved_in_repo_method_targets(edges):
    calls = {(r["caller_method"], r["callee_method"]): r for r in edges["MethodCalls"]}
    round_ = mid(EXTRA, "FeeCalculator.Round")
    assert set(calls) == {
        (round_, mid(FEE, "FeeCalculator.Calculate")),
        (round_, mid(EXTRA, "FeeCalculator.Round.func1")),
        (round_, mid(FEE, "New")),
        (mid(MAIN, "main"), mid(FEE, "Totaler.Total")),
        (mid(MAIN, "main"), mid(FEE, "New")),
    }
    closure = calls[(round_, mid(EXTRA, "FeeCalculator.Round.func1"))]
    assert (closure["resolution"], closure["site_kind"], closure["call_line"],
            closure["callee_name"]) == ("convention:funclit", "go", 17, "func")
    assert calls[(round_, mid(FEE, "New"))]["resolution"] == "convention:func-alias"
    iface = calls[(mid(MAIN, "main"), mid(FEE, "Totaler.Total"))]
    assert (iface["resolution"], iface["callee_name"]) == ("types", "Total")


def test_dedupe_keeps_the_first_sites_line_and_kind(edges):
    calls = {(r["caller_method"], r["callee_method"]): r for r in edges["MethodCalls"]}
    first = calls[(mid(EXTRA, "FeeCalculator.Round"), mid(FEE, "FeeCalculator.Calculate"))]
    assert (first["call_line"], first["site_kind"]) == (11, "defer")


def test_records_that_are_not_resolved_are_counted_never_edged(edges):
    stats = edges.stats
    assert stats["calls_external"] == 2 and stats["calls_peer"] == 1
    assert stats["calls_builtin"] == 1 and stats["calls_dynamic"] == 1
    assert stats["calls_unresolved"] == 2
    assert stats["calls_ambiguous"] == 2
    assert stats["calls_resolved"] == 6  # sites edged, before the per-pair dedupe


@pytest.mark.parametrize("target", [
    tgt(FEE, "FeeCalculator", kind="class"),           # a type, not a function
    tgt(FEE, "NoSuchFunc"),                            # no node at that name
    tgt("lib/x.go", "Shared", repo="shared"),          # a peer target is left to crossref
    tgt(FEE, "New") | {"kind": None},                  # no kind
    None,
])
def test_a_resolved_record_without_an_in_repo_method_target_writes_nothing(target):
    data = make_data(res={MAIN: {"calls": [call("main", "X", 9, "resolved", target)]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["MethodCalls"] == [] and edges["FileDependsOn"] == []
    assert edges.stats["calls_unmapped"] + edges.stats.get("calls_peer", 0) == 1


def test_a_caller_that_is_no_node_writes_nothing():
    data = make_data(res={MAIN: {"calls": [
        call("NotAFunc", "New", 9, "resolved", tgt(FEE, "New")),
        call("main", "Total", 10, "resolved", tgt(FEE, "Totaler.Total"),
             candidates=[cand(FEE, "FeeCalculator.Total")])]}})
    data.resolutions["files"][MAIN]["calls"][1]["site"]["caller"] = "NotAFunc"
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["MethodCalls"] == [] and edges["PossiblyCalls"] == []
    assert edges.stats["calls_unmapped"] == 2 and edges.stats["possibly_unmapped_caller"] == 1


def test_possibly_calls_for_interface_candidates_carry_open_world(edges):
    rows = {(r["callee_method"], r["callee_name"]): r for r in edges["PossiblyCalls"]
            if r["caller_method"] == mid(MAIN, "main")}
    total = rows[(mid(FEE, "FeeCalculator.Total"), "Total")]
    assert (total["reason"], total["candidate_count"], total["open_world"]) == (
        "implementation", 1, True)
    write = rows[(mid(BASE, "Base.Write"), "Write")]  # interface outside the repo
    assert (write["reason"], write["candidate_count"], write["open_world"]) == (
        "implementation", 1, True)
    assert not any(r["callee_method"] == mid(BASE, "Base.Write") for r in edges["MethodCalls"])


def test_ambiguous_records_give_possibly_calls_with_their_reason(edges):
    rows = {(r["callee_method"], r["reason"]): r for r in edges["PossiblyCalls"]}
    anon = rows[(mid(BASE, "Base.ID"), "implementation")]
    assert (anon["candidate_count"], anon["open_world"], anon["callee_name"]) == (
        2, False, "Describe")
    variant = rows[(mid(EXTRA, "FeeCalculator.Round"), "variant")]
    assert (variant["candidate_count"], variant["callee_name"]) == (2, "Calculate")
    assert (mid(FEE, "FeeCalculator.Calculate"), "variant") in rows


def test_the_first_site_wins_for_a_possibly_calls_pair(edges):
    # FeeCalculator.Total is a candidate at line 9 (count 1) and line 11 (count 2)
    total = [r for r in edges["PossiblyCalls"]
             if r["callee_method"] == mid(FEE, "FeeCalculator.Total")]
    assert len(total) == 1 and total[0]["candidate_count"] == 1
    assert total[0]["callee_name"] == "Total"


def test_unresolved_and_dynamic_records_never_give_possibly_calls(edges):
    assert not any(r["callee_name"] == "Gone" for r in edges["PossiblyCalls"])
    data = make_data(res={MAIN: {"calls": [
        call("main", "Dyn", 9, "dynamic", candidates=[cand(BASE, "Base.ID")]),
        call("main", "Blt", 10, "builtin", candidates=[cand(BASE, "Base.ID")])]}})
    assert graph_rows.derive_edge_rows(data, graph_rows.node_index(data))["PossiblyCalls"] == []


def test_above_the_cap_a_site_writes_no_rows_and_is_counted(data, monkeypatch):
    monkeypatch.setattr(config, "POSSIBLY_CALLS_MAX_CANDIDATES", 1)
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert {r["callee_name"] for r in edges["PossiblyCalls"]} == {"Total", "Write"}
    assert edges.stats["possibly_capped"] == 2  # Describe and Calculate had two each
    monkeypatch.setattr(config, "POSSIBLY_CALLS_MAX_CANDIDATES", 2)
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges.stats.get("possibly_capped", 0) == 0
    assert {r["callee_name"] for r in edges["PossiblyCalls"]} == {
        "Total", "Write", "Describe", "Calculate"}


def test_the_cap_counts_in_graph_candidates_only(monkeypatch):
    monkeypatch.setattr(config, "POSSIBLY_CALLS_MAX_CANDIDATES", 2)
    data = make_data(res={MAIN: {"calls": [call(
        "main", "Describe", 9, "ambiguous", reason="implementation",
        candidates=[cand(FEE, "FeeCalculator.Total"), cand(BASE, "Base.ID"),
                    cand("vendor/x.go", "X.Describe"), cand(BASE, "Base.ID")])]}})
    rows = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))["PossiblyCalls"]
    assert {r["callee_method"] for r in rows} == {mid(FEE, "FeeCalculator.Total"),
                                                  mid(BASE, "Base.ID")}
    assert {r["candidate_count"] for r in rows} == {2}


def test_an_unknown_ambiguous_reason_writes_no_rows():
    data = make_data(res={MAIN: {"calls": [call(
        "main", "X", 9, "ambiguous", reason="name-heuristic",
        candidates=[cand(BASE, "Base.ID")])]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["PossiblyCalls"] == [] and edges.stats["possibly_bad_reason"] == 1


def test_package_imports_only_to_in_repo_packages(edges):
    rows = {(r["source_file"], r["target_package"]): r for r in edges["PackageImports"]}
    billing = pid("libs/billing", "billing")
    assert set(rows) == {(fid(MAIN), billing), (fid(XTEST), billing)}
    main_import = rows[(fid(MAIN), billing)]
    assert (main_import["import_path"], main_import["alias"], main_import["kind"]) == (
        BILLING, "b", "normal")
    assert rows[(fid(XTEST), billing)]["alias"] == ""
    assert edges.stats["imports_external"] == 1


@pytest.mark.parametrize("rec,stat", [
    (import_rec(5, BILLING, dir="libs/billing", clause="billing", repo="shared"), "imports_peer"),
    (import_rec(5, BILLING, dir="libs/billing", clause="nope"), "imports_unmapped"),
    (import_rec(5, BILLING, status="unresolved"), "imports_unresolved"),
    # no entity import at that position
    (import_rec(4, BILLING, dir="libs/billing", clause="billing"), "imports_unmapped"),
    (import_rec(5, "example.com/other", dir="libs/billing", clause="billing"),
     "imports_unmapped"),  # the entity import at that position names another path
])
def test_imports_that_are_not_certain_write_nothing(rec, stat):
    data = make_data(res={MAIN: {"calls": [], "imports": [rec]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["PackageImports"] == [] and edges.stats[stat] == 1


def test_blank_and_dot_imports_keep_their_kind():
    ents = entities()
    ents[MAIN]["imports"] = [imp(BILLING, 5, alias="_", kind="blank"),
                             imp(f"{MOD}/libs/kernel", 6, alias=".", kind="dot")]
    data = make_data(ents, {MAIN: {"imports": [
        import_rec(5, BILLING, dir="libs/billing", clause="billing"),
        import_rec(6, f"{MOD}/libs/kernel", dir="libs/kernel", clause="kernel")]}})
    rows = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))["PackageImports"]
    assert {(r["alias"], r["kind"]) for r in rows} == {("_", "blank"), (".", "dot")}


def test_class_embeds_and_implements_need_both_ends_in_repo(edges):
    (embed,) = edges["ClassEmbeds"]
    assert embed == {"edge_id": embed["edge_id"], "child_class": cid(FEE, "FeeCalculator"),
                     "embedded_class": cid(BASE, "Base"), "pointer": False,
                     "resolution": "types"}
    (impl,) = edges["ClassImplements"]
    assert impl == {"edge_id": impl["edge_id"], "class_id": cid(FEE, "FeeCalculator"),
                    "interface_id": cid(FEE, "Totaler"), "pointer_receiver": False,
                    "resolution": "types"}
    assert edges.stats["embeds_external"] == 1 and edges.stats["implements_external"] == 1


def test_pointer_flags_are_python_bools():
    data = make_data(res={FEE: {
        "embeds": [embed_rec(FEE, "FeeCalculator", tgt(BASE, "Base", kind="class"),
                             pointer=1)],
        "implements": [implements_rec(FEE, "FeeCalculator", tgt(FEE, "Totaler", kind="class"),
                                      pointer_receiver="yes")]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["ClassEmbeds"][0]["pointer"] is True
    assert edges["ClassImplements"][0]["pointer_receiver"] is True


@pytest.mark.parametrize("embedded,status,stat", [
    (tgt("lib/x.go", "Base", kind="class", repo="shared"), "external", "embeds_peer"),
    (tgt(BASE, "Base", kind="class", repo="shared"), "resolved", "embeds_peer"),
    (tgt(BASE, "Base.ID"), "resolved", "embeds_unmapped"),       # a method is no type
    (tgt(BASE, "Nope", kind="class"), "resolved", "embeds_unmapped"),
    (tgt(BASE, "Base", kind="class"), "unresolved", "embeds_unresolved"),
])
def test_embeds_that_are_not_certain_write_nothing(embedded, status, stat):
    data = make_data(res={FEE: {"embeds": [embed_rec(FEE, "FeeCalculator", embedded, status)]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["ClassEmbeds"] == [] and edges.stats[stat] == 1
    assert edges["FileDependsOn"] == []


def test_an_implements_record_onto_the_type_itself_writes_nothing():
    data = make_data(res={FEE: {"implements": [
        implements_rec(FEE, "Totaler", tgt(FEE, "Totaler", kind="class"))]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["ClassImplements"] == [] and edges.stats["implements_unmapped"] == 1


def test_file_depends_on_follows_method_calls_and_class_embeds_only(edges):
    deps = {(r["source_file"], r["target_file"]): r["resolution"] for r in edges["FileDependsOn"]}
    assert deps == {
        (fid(EXTRA), fid(FEE)): "convention:func-alias",  # the first site (line 10) reaches it
        (fid(MAIN), fid(FEE)): "types",
        (fid(FEE), fid(BASE)): "types",                   # FeeCalculator embeds Base
    }
    # not the file itself (the closure call), not PossiblyCalls (MAIN → BASE), not imports
    # (XTEST → FEE) and not implements


def test_structural_edges(edges, nodes):
    classes, methods = nodes["Classes"], nodes["Methods"]
    assert pairs(edges["FileDefinesClass"], "file_id", "class_id") == {
        (c["file_id"], c["class_id"]) for c in classes}
    assert pairs(edges["ClassDefinesMethod"], "class_id", "method_id") == {
        (m["class_id"], m["method_id"]) for m in methods}
    assert pairs(edges["DirContainsFile"], "dir_id", "file_id") == {
        (did(rel.rpartition("/")[0]), fid(rel)) for rel in [*entities(), BAD]}
    billing = pid("libs/billing", "billing")
    assert pairs(edges["PackageContainsFile"], "package_id", "file_id") == {
        (billing, fid(FEE)), (billing, fid(EXTRA)), (billing, fid(BASE)),
        (billing, fid(FEE_TEST)), (pid("libs/billing", "billing_test"), fid(XTEST)),
        (pid("cmd/reception", "main"), fid(MAIN)), (pid("libs/kernel", "kernel"), fid(KDOC))}


def test_every_edge_end_is_a_node(edges, nodes):
    ids = {t: {r[NODE_SPEC[t][0]] for r in nodes[t]} for t in NODE_TABLES}
    for table in EDGE_TABLES:
        _pk, _cols, (scol, stab), (dcol, dtab) = EDGE_SPEC[table]
        for row in edges[table]:
            assert row[scol] in ids[stab] and row[dcol] in ids[dtab], (table, row)


def test_a_file_whose_resolutions_are_stale_writes_no_derived_edges(data):
    data.resolutions["files"][EXTRA]["sha256"] = "changed-during-load"
    del data.resolutions["files"][MAIN]["sha256"]
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert not any(r["caller_method"] in (mid(EXTRA, "FeeCalculator.Round"), mid(MAIN, "main"))
                   for r in edges["MethodCalls"] + edges["PossiblyCalls"])
    assert not any(r["source_file"] == fid(MAIN) for r in edges["PackageImports"])
    assert edges.stats["files_stale"] == 2
    assert edges["ClassDefinesMethod"] and edges["ClassEmbeds"]  # other files still edged


def test_resolutions_of_unscanned_files_are_ignored(data):
    data.resolutions["files"]["gone/old.go"] = {
        "sha256": "x", "calls": [call("Old", "New", 3, "resolved", tgt(FEE, "New"))]}
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges.stats["files_not_scanned"] == 1


def test_without_resolutions_only_structural_edges_are_written():
    data = make_data(res={})
    data.resolutions = {}
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert list(edges) == list(EDGE_TABLES)
    for table in ("PackageImports", "FileDependsOn", "ClassEmbeds", "ClassImplements",
                  "MethodCalls", "PossiblyCalls"):
        assert edges[table] == []
    assert edges["FileDefinesClass"] and edges["DirContainsFile"]


def test_derivation_is_deterministic():
    one = graph_rows.derive_edge_rows(make_data(), graph_rows.node_index(make_data()))
    two_data = make_data()
    for frec in two_data.resolutions["files"].values():
        frec["calls"].reverse()
    two = graph_rows.derive_edge_rows(two_data, graph_rows.node_index(two_data))
    assert one == two and one.stats == two.stats


def test_the_builders_do_not_change_their_inputs(data):
    before = copy.deepcopy((data.extracted_entities, data.resolutions, data.file_hashes))
    graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    graph_rows.build_node_rows(data)
    assert (data.extracted_entities, data.resolutions, data.file_hashes) == before


# ── pipeline delegation ─────────────────────────────────────────────

def test_pipeline_delegates_to_graph_rows(data):
    idx = pipeline._node_index(data)
    assert idx["files"] == graph_rows.node_index(data)["files"]
    assert pipeline.build_node_rows(data) == graph_rows.build_node_rows(data)
    derived = pipeline.derive_edge_rows(data, idx)
    assert derived == graph_rows.derive_edge_rows(data, idx)
    assert derived.stats["calls_resolved"] == 6


def test_a_site_without_a_name_takes_the_callees_simple_name():
    # paren and other shapes (`(f)(x)`, `x.(F)()`) have name "" (C3)
    data = make_data(res={MAIN: {"calls": [
        call("main", "", 9, "resolved", tgt(FEE, "New"), col=5),
        call("main", "", 10, "ambiguous", reason="variant",
             candidates=[cand(FEE, "FeeCalculator.Calculate"), cand(BASE, "Base.ID")])]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert [r["callee_name"] for r in edges["MethodCalls"]] == ["New"]
    assert {r["callee_name"] for r in edges["PossiblyCalls"]} == {"Calculate", "ID"}


def test_a_closure_parent_cycle_or_gap_falls_back_to_the_package(data):
    data.extracted_entities[BASE]["funcs"] += [
        fn("Loop.func1", "closure", 20, parent="Loop.func2"),
        fn("Loop.func2", "closure", 21, parent="Loop.func1"),
        fn("Orphan.func1", "closure", 22, parent="NoSuchParent"),
        {**fn("Nameless", line=23), "qualname": ""}]
    methods = by_id(graph_rows.build_node_rows(data)["Methods"], "method_id")
    for q in ("Loop.func1", "Loop.func2", "Orphan.func1"):
        assert methods[mid(BASE, q)]["class_id"] == cid(BASE, "<package>")
    assert "" not in {m["qualname"] for m in methods.values()}


def test_a_site_whose_candidates_are_all_out_of_graph_writes_nothing():
    data = make_data(res={MAIN: {"calls": [call(
        "main", "Total", 9, "resolved", tgt(FEE, "Totaler.Total"),
        candidates=[cand("vendor/x.go", "X.Total"), cand(FEE, "Nope.Total")])]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["PossiblyCalls"] == [] and edges.stats["possibly_no_candidates"] == 1
    assert len(edges["MethodCalls"]) == 1  # the interface method itself stays


# ── review fixes: stale ends, the importable clause, external imports ──

def test_no_edge_ends_in_a_file_whose_resolutions_are_stale(data):
    # BASE changed while the helper loaded it: positions mapped into it are
    # untrusted, so nothing may point there (spec §2.6, G8).
    data.resolutions["files"][BASE]["sha256"] = "changed-during-load"
    data.resolutions["files"][MAIN]["calls"].append(
        call("main", "ID", 20, "resolved", tgt(BASE, "Base.ID")))
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    base_methods = {mid(BASE, "Base.ID"), mid(BASE, "Base.Write")}
    assert not base_methods & {r["callee_method"] for r in edges["MethodCalls"]}
    assert not base_methods & {r["callee_method"] for r in edges["PossiblyCalls"]}
    assert edges["ClassEmbeds"] == []          # FeeCalculator → Base
    assert (fid(FEE), fid(BASE)) not in pairs(edges["FileDependsOn"], "source_file", "target_file")
    # candidates outside BASE stay (the variant site at line 12)
    assert (mid(MAIN, "main"), mid(EXTRA, "FeeCalculator.Round")) in pairs(
        edges["PossiblyCalls"], "caller_method", "callee_method")
    # nodes and structural edges of BASE stay
    assert (cid(BASE, "Base"), mid(BASE, "Base.ID")) in pairs(
        edges["ClassDefinesMethod"], "class_id", "method_id")
    assert edges.stats["files_stale"] == 1


def test_an_interface_in_a_stale_file_gets_no_implements_edge(data):
    data.resolutions["files"][FEE]["implements"] = []
    data.resolutions["files"][BASE]["implements"] = [
        implements_rec(BASE, "Base", tgt(FEE, "Totaler", kind="class"))]
    data.resolutions["files"][FEE]["sha256"] = "changed-during-load"
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["ClassImplements"] == [] and edges.stats["implements_unmapped"] == 1


def _billing_world(generator_included=False, billing_included=True):
    ents = entities()
    ents["libs/billing/codegen.go"] = record(name="main", origin="command",
                                             build_included=generator_included,
                                             build_constraint="ignore",
                                             funcs=[fn("main", line=5)])
    for rel in (FEE, EXTRA, BASE, FEE_TEST):
        ents[rel]["build_included"] = billing_included
    return ents


@pytest.mark.parametrize("clause,generator_included,billing_included,edged", [
    ("billing", False, True, True),    # the generator file is excluded by the build
    ("main", False, True, False),      # the producer named the excluded file's clause
    ("billing", True, True, False),    # two compiled clauses: no certain package
    ("billing", False, False, False),  # nothing compiled: billing and main both possible
])
def test_an_import_targets_the_one_importable_clause(clause, generator_included,
                                                     billing_included, edged):
    data = make_data(_billing_world(generator_included, billing_included), {MAIN: {"imports": [
        import_rec(5, BILLING, dir="libs/billing", clause=clause)]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert bool(edges["PackageImports"]) is edged
    if edged:
        assert edges["PackageImports"][0]["target_package"] == pid("libs/billing", "billing")
    else:
        assert edges.stats["imports_unmapped"] == 1


def test_a_package_compiled_by_no_build_but_with_one_clause_is_importable():
    ents = entities()
    for rel in (FEE, EXTRA, BASE, FEE_TEST):
        ents[rel]["build_included"] = False   # e.g. resolved by a GO_EXTRA_BUILDS load
    data = make_data(ents, {MAIN: {"imports": [
        import_rec(5, BILLING, dir="libs/billing", clause="billing")]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert len(edges["PackageImports"]) == 1


def test_external_imports_are_counted_per_module():
    std = {"module": "std", "version": "", "package": "fmt", "qualname": "", "peer": False}
    dep = {"module": "example.com/thirdparty/textfmt", "version": "v1.1.0",
           "package": "example.com/thirdparty/textfmt/wrap", "qualname": "", "peer": False}
    records = [import_rec(6, "fmt", status="external"),
               import_rec(7, "strings", status="external"),
               import_rec(8, dep["package"], status="external")]
    records[0]["external"], records[1]["external"] = std, {**std, "package": "strings"}
    records[2]["external"] = dep
    data = make_data(res={MAIN: {"imports": records}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges.external_imports == {"std": 2, "example.com/thirdparty/textfmt": 1}
    assert edges.stats["imports_external"] == 3


@pytest.mark.parametrize("clause", ["billing", "winbilling"])
def test_an_importer_outside_the_primary_build_needs_one_clause_in_every_file(clause):
    # libs/billing is `billing` on linux and `winbilling` on windows; the
    # importer is compiled only by a windows GO_EXTRA_BUILDS load, so the
    # primary build's clause says nothing about its import.
    ents = entities()
    ents["libs/billing/fee_windows.go"] = record(name="winbilling", build_included=False,
                                                 build_constraint="windows",
                                                 funcs=[fn("Fee", line=3)])
    ents[MAIN]["build_included"] = False
    data = make_data(ents, {MAIN: {"imports": [
        import_rec(5, BILLING, dir="libs/billing", clause=clause)]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["PackageImports"] == [] and edges.stats["imports_unmapped"] == 1


def test_an_importer_outside_the_primary_build_and_one_clause_is_edged():
    ents = entities()
    ents[MAIN]["build_included"] = False
    data = make_data(ents, {MAIN: {"imports": [
        import_rec(5, BILLING, dir="libs/billing", clause="billing")]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert len(edges["PackageImports"]) == 1


@pytest.mark.parametrize("record,stat", [
    (embed_rec(FEE, "FeeCalculator", None, status="unresolved"), "embeds_unresolved"),
    (embed_rec(FEE, "FeeCalculator", None), "embeds_unmapped"),    # resolved, but no end
    ({"type": None, "embedded": tgt(BASE, "Base", kind="class"), "status": "resolved",
      "external": None, "pointer": False}, "embeds_unmapped"),
    ({"type": {"relpath": FEE, "qualname": "FeeCalculator"}, "embedded": None}, "embeds_none"),
    (None, None),                                                   # not a record at all
])
def test_embed_records_without_an_end_are_skipped(record, stat):
    data = make_data(res={FEE: {"embeds": [record]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["ClassEmbeds"] == [] and edges["FileDependsOn"] == []
    if stat:
        assert edges.stats[stat] == 1


@pytest.mark.parametrize("record,stat", [
    (implements_rec(FEE, "FeeCalculator", None, status="unresolved"), "implements_unresolved"),
    (implements_rec(FEE, "FeeCalculator", None), "implements_unmapped"),
    ({"type": None, "iface": tgt(FEE, "Totaler", kind="class"), "status": "resolved",
      "external": None, "pointer_receiver": False}, "implements_unmapped"),
])
def test_implements_records_without_an_end_are_skipped(record, stat):
    data = make_data(res={FEE: {"implements": [record]}})
    edges = graph_rows.derive_edge_rows(data, graph_rows.node_index(data))
    assert edges["ClassImplements"] == [] and edges.stats[stat] == 1
