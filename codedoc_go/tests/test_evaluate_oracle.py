"""evaluate: goracle's site table and the runtime judge of every record (R50 6a-c)."""
from __future__ import annotations

import json

import pytest

from graph_generator import evaluate
from tests.eval_worlds import record, resolutions, target

TABLE = [
    {"kind": "file", "file": "app/run.go", "status": "instrumented"},
    {"kind": "file", "file": "app/native_cgo.go", "status": "skipped:cgo"},
    {"kind": "func", "fid": 0, "qualname": "Run", "file": "app/run.go"},
    {"kind": "func", "fid": 1, "qualname": "Run.func1", "file": "app/run.go"},
    {"kind": "site", "sid": 0, "file": "app/run.go", "line": 5, "col": 2, "name": "Send", "fid": 0, "stmt": "call"},
    {"kind": "site", "sid": 1, "file": "app/run.go", "line": 6, "col": 8, "name": "Close", "fid": 1,
     "stmt": "defer"},
    {"kind": "site", "sid": 2, "file": "app/run.go", "line": 2, "col": 9, "name": "New", "fid": -1, "stmt": "call"},
    {"kind": "site", "sid": 3, "file": "app/run.go", "line": 7, "col": 3, "name": "All", "fid": 0, "stmt": "range"},
    {"kind": "site", "sid": 4, "file": "app/run.go", "line": 7, "col": 3, "name": "All", "fid": 0, "stmt": "call"},
    {"kind": "site", "sid": 5, "file": "app/run.go", "line": 8, "col": 10, "name": "", "form": "other",
     "lparen_line": 8, "lparen_col": 28, "fid": 1, "stmt": "call"},
]


def _table(tmp_path) -> str:
    path = tmp_path / "sites.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in TABLE), encoding="utf-8")
    return str(path)


def test_the_site_table_maps_each_site_to_its_enclosing_function(tmp_path):
    table = evaluate.site_table(_table(tmp_path))
    assert table.callers == {
        ("app/run.go", 5, 2, "call"): "Run",
        ("app/run.go", 6, 8, "defer"): "Run.func1",
        ("app/run.go", 2, 9, "call"): "<init>",              # package-level initializer
        ("app/run.go", 7, 3, "range"): "Run",
        ("app/run.go", 7, 3, "call"): "Run",
        ("app/run.go", 8, 10, "call"): "Run.func1",
        ("app/run.go", 8, 28, "call"): "Run.func1",          # nameless callee: also keyed by its "("
    }
    assert table.files == {"app/run.go"} and table.skipped == {"app/native_cgo.go": "skipped:cgo"}


def test_a_callee_without_a_name_is_found_by_its_paren(tmp_path):
    # C3 places (*[n]byte)(p) and (*fp)(x) on the call's "(" (gohelper extract sites.go)
    rec = record(8, 28, "unresolved", name="", caller="Run.func1", reason="excluded-by-build")
    assert evaluate.record_problems(_res(rec), evaluate.site_table(_table(tmp_path)), prefix="app/") == []


def _res(*recs) -> dict:
    return resolutions({"run.go": list(recs)}, "app")


def test_records_agree_with_the_site_table_and_their_names(tmp_path):
    callers = evaluate.site_table(_table(tmp_path))
    good = [record(5, 2, "resolved", name="Send", caller="Run", target=target("x.go", "Notifier.Send"),
                   candidates=[target("x.go", "Mail.Send")]),
            record(6, 8, "external", kind="defer", name="Close", caller="Run.func1",
                   external={"module": "std", "version": "", "package": "os", "qualname": "File.Close",
                             "peer": False}),
            record(2, 9, "resolved", name="New", caller="<init>", target=target("x.go", "New"))]
    assert evaluate.record_problems(_res(*good), callers, prefix="app/") == []


def test_a_wrong_name_a_wrong_caller_and_an_unknown_site_are_problems(tmp_path):
    callers = evaluate.site_table(_table(tmp_path))
    recs = [record(5, 2, "resolved", name="Send", caller="Run", target=target("x.go", "Notifier.Close")),
            record(5, 2, "ambiguous", name="Send", caller="Run", candidates=[target("x.go", "Mail.Sendx")]),
            record(5, 2, "resolved", name="Send", caller="Run.func1", target=target("x.go", "Notifier.Send")),
            record(9, 1, "dynamic", name="f", caller="Run"),
            record(5, 2, "resolved", kind="defer", name="Send", caller="Run", target=target("x.go", "N.Send")),
            record(7, 3, "resolved", name="All", caller="Run", via="convention:range",
                   target=target("x.go", "Seq"))]                       # not via=types: name not checked
    assert evaluate.record_problems(_res(*recs), callers, prefix="app/") == [
        "app/run.go:5:2 call Send: names Notifier.Close",
        "app/run.go:5:2 call Send: names Mail.Sendx",
        "app/run.go:5:2 call Send: caller Run.func1, goracle's table says Run",
        "app/run.go:9:1 call f: no such site in goracle's table",
        "app/run.go:5:2 defer Send: no such site in goracle's table",
    ]


def test_files_goracle_skips_for_a_reason_are_not_compared_and_unknown_files_are(tmp_path):
    table = evaluate.site_table(_table(tmp_path))
    res = resolutions({"native_cgo.go": [record(8, 17, "external", name="ToUpper", caller="Version",
                                                external={"module": "std", "qualname": "ToUpper"})],
                       "other.go": [record(3, 1, "dynamic", name="f", caller="G")]}, "app")
    assert evaluate.record_problems(res, table, prefix="app/") == ["app/other.go: not in goracle's table"]


def test_the_judge_gets_every_resolved_method_record_with_its_candidates():
    ents = {"x.go": {"types": [{"qualname": "Notifier", "methods": [{"qualname": "Notifier.Send"}]}],
                     "funcs": []}}
    iface_call = record(5, 2, "resolved", name="Send", caller="Run", target=target("x.go", "Notifier.Send", line=3),
                        candidates=[target("y.go", "Mail.Send", line=9), target("z.go", "Fax.Send", line=4)])
    iface_call["candidates"][1]["via_embedded_iface"] = True
    res = _res(iface_call,
               record(6, 8, "resolved", name="New", caller="Run", target=target("x.go", "New", line=7)),
               record(7, 1, "resolved", name="T", caller="Run", target=target("x.go", "T", kind="class")),
               record(8, 1, "external", name="P", caller="Run",
                      target=target("p.go", "P", repo="shared"), external={"module": "m", "peer": True}),
               record(9, 1, "dynamic", name="f", caller="Run"))
    assert evaluate.oracle_edges(res, ents, prefix="app/") == [
        {"file": "app/run.go", "line": 5, "col": 2, "name": "Send", "note": "types",
         "target": {"file": "app/x.go", "line": 3, "qualname": "Notifier.Send", "kind": "interface_method"},
         "candidates": [{"file": "app/y.go", "line": 9, "qualname": "Mail.Send"},
                        {"file": "app/z.go", "line": 4, "qualname": "Fax.Send", "via_embedded_iface": True}]},
        {"file": "app/run.go", "line": 6, "col": 8, "name": "New", "note": "types",
         "target": {"file": "app/x.go", "line": 7, "qualname": "New"}},                 # no interface: no key
    ]


@pytest.mark.parametrize("open_world, cands, partial, sent", [
    (False, [], None, []),                 # closed world, no implementers: any in-repo callee is wrong
    (False, ["Mail.Send"], None, [{"file": "app/y.go", "line": 9, "qualname": "Mail.Send"}]),
    (True, [], None, None),                # p8: io.NopCloser(R{}) forwards to R.Read, so the name rule
    (True, ["Mail.Send"], None, None),     # open world: an outside implementer may forward anywhere
    (True, [], "suppressed", None),        # G4: the search did not run
    (False, ["Mail.Send"], "via_embedded_external", None),   # a candidate outside the repo was dropped
])
def test_only_closed_world_interface_edges_carry_their_candidates(open_world, cands, partial, sent):
    ents = {"x.go": {"types": [{"qualname": "Notifier", "methods": [{"qualname": "Notifier.Send"}]}], "funcs": []}}
    rec = record(5, 2, "resolved", name="Send", caller="Run", target=target("x.go", "Notifier.Send", line=3),
                 candidates=[target("y.go", q, line=9) for q in cands], open_world=open_world)
    if partial:
        rec["candidates_partial"] = partial
    edge = evaluate.oracle_edges(_res(rec), ents, prefix="app/")[0]
    assert edge.get("candidates") == sent and ("candidates" in edge) is (sent is not None)


def _fake_oracle(monkeypatch, verdicts: dict, failed=()) -> list:
    from graph_generator import go_toolchain
    calls = []
    monkeypatch.setattr(go_toolchain, "find_toolchain", lambda: "tc")
    monkeypatch.setattr(evaluate, "build_goracle", lambda tc, work: calls.append("build") or "goracle")
    table = evaluate.SiteTable({("run.go", 5, 2, "call"): "Run"}, frozenset({"run.go"}), {})
    monkeypatch.setattr(evaluate, "scan_sites", lambda g, root, out: table)
    monkeypatch.setattr(evaluate, "observe", lambda *a: calls.append("observe") or ("obs", list(failed)))
    monkeypatch.setattr(evaluate, "match", lambda g, obs, edges, work: calls.append(len(edges)) or verdicts)
    return calls


def _one_edge_runs(n: int = 1) -> dict:
    recs = [record(5, 2, "resolved", name="Send", caller="Run", target=target("x.go", "Send")) for _ in range(n)]
    return {"fx": evaluate.RepoRun(name="fx", dir=".", entities={}, resolutions=_res(*recs))}


@pytest.mark.parametrize("verdicts, failed, ok", [
    ({"CONFIRMED": 2, "UNEXERCISED": 1, "UNVERIFIABLE-TARGET": 1}, (), True),
    ({"CONFIRMED": 2, "WRONG-OTHER": 1, "UNEXERCISED": 1}, (), False),
    ({"CONFIRMED": 2, "NO-TARGET": 1, "UNEXERCISED": 1}, (), False),     # unplaced: a bug to look at (A1)
    ({"CONFIRMED": 2, "NO-SITE": 1, "UNEXERCISED": 1}, (), False),
    ({"CONFIRMED": 4}, ("web-demo",), False),
])
def test_the_judge_fails_on_wrong_unplaced_or_failed_entries(monkeypatch, tmp_path, verdicts, failed, ok):
    calls = _fake_oracle(monkeypatch, verdicts, failed)
    got = evaluate.oracle_check(str(tmp_path), _one_edge_runs(4), str(tmp_path / "eval"), judge=True)
    assert calls == ["build", "observe", 4]
    assert got["pass"] is ok and got["verdicts"] == verdicts and got["seconds"] >= 0
    assert got["unplaced"] == verdicts.get("NO-TARGET", 0) + verdicts.get("NO-SITE", 0)
    assert got["problems"] == 0


def test_a_verdict_count_other_than_the_edge_count_fails(monkeypatch, tmp_path):
    _fake_oracle(monkeypatch, {"CONFIRMED": 3})
    got = evaluate.oracle_check(str(tmp_path), _one_edge_runs(4), str(tmp_path / "eval"), judge=True)
    assert got["pass"] is False and got["error"] == "goracle match returned 3 verdicts for 4 edges"


@pytest.mark.parametrize("verdicts, executed, ok", [
    ({"CONFIRMED": 1, "UNEXERCISED": 3}, 1, True),
    ({"UNEXERCISED": 4}, 0, False),                                        # the runtime recorded nothing
    ({"UNEXERCISED": 2, "UNVERIFIABLE-TARGET": 2}, 2, False),              # ran, but nothing was judged
])
def test_the_judge_needs_at_least_one_executed_edge(monkeypatch, tmp_path, verdicts, executed, ok):
    _fake_oracle(monkeypatch, verdicts)
    got = evaluate.oracle_check(str(tmp_path), _one_edge_runs(4), str(tmp_path / "eval"), judge=True)
    assert got["executed"] == executed and got["pass"] is ok
    if not ok:
        assert got["error"] == "goracle judged no executed edge (every verdict UNEXERCISED or UNVERIFIABLE-TARGET)"
