"""trial (spec §9.6, R6, R7): the two arms, their comparison, the untouched-repo
check, the oracle records, the summary; orchestration with injected stages."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graph_generator import config, evaluate, trial
from scripts import verification_table
from tests.eval_worlds import entity, external, import_record, record, resolutions, target


# ═══════════════════════════════════════════════════════ the repo stays untouched


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "index").write_bytes(b"DIRC")
    (root / "go.mod").write_text("module example.com/r\n\ngo 1.26\n", encoding="utf-8")
    (root / "a.go").write_text("package r\n", encoding="utf-8")
    return root


def test_an_untouched_repo_has_no_changes(tmp_path):
    root = _repo(tmp_path)
    assert trial.tree_changes(trial.snapshot(str(root)), trial.snapshot(str(root))) == []


@pytest.mark.parametrize("touch, change", [
    (lambda r: (r / "a.go").write_text("package r // edited\n", encoding="utf-8"), "changed: a.go"),
    (lambda r: (r / "go.work.sum").write_text("x", encoding="utf-8"), "added: go.work.sum"),
    (lambda r: (r / "a.go").unlink(), "removed: a.go"),
    (lambda r: os.utime(r / ".git" / "index", ns=(1, 1)), "changed: .git/index"),
    (lambda r: (r / "sub").mkdir(), "added: sub/"),
])
def test_every_write_into_the_repo_is_reported(tmp_path, touch, change):
    root = _repo(tmp_path)
    before = trial.snapshot(str(root))
    touch(root)
    assert change in trial.tree_changes(before, trial.snapshot(str(root)))


def test_the_snapshot_sees_transient_files_modes_and_ctimes(tmp_path):
    """The writes follow the snapshot at once: it returns only after the file-time
    clock (a coarse kernel tick) has passed its newest ctime, so they cannot
    repeat a recorded time."""
    root = _repo(tmp_path)
    before = trial.snapshot(str(root))
    (root / "go.work.sum").write_text("x", encoding="utf-8")
    (root / "go.work.sum").unlink()                                   # created, then removed
    assert "changed: ./" in trial.tree_changes(before, trial.snapshot(str(root)))
    before = trial.snapshot(str(root))
    os.chmod(root / "a.go", 0o777)
    assert "changed: a.go" in trial.tree_changes(before, trial.snapshot(str(root)))
    before = trial.snapshot(str(root))
    st = os.stat(root / "a.go")
    (root / "a.go").write_text("package x\n", encoding="utf-8")       # same size, other bytes
    os.utime(root / "a.go", ns=(st.st_atime_ns, st.st_mtime_ns))     # mtime restored, ctime is not
    assert "changed: a.go" in trial.tree_changes(before, trial.snapshot(str(root)))


def test_the_snapshot_follows_a_git_file_to_the_worktree_gitdir(tmp_path):
    root = tmp_path / "wt"
    gitdir = tmp_path / "main" / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "index").write_bytes(b"DIRC")
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (root / "a.go").write_text("package r\n", encoding="utf-8")
    before = trial.snapshot(str(root))
    assert "gitdir:index" in before
    st = os.stat(gitdir / "index")
    (gitdir / "index").write_bytes(b"DIRC")                             # git rewrote the worktree index
    os.utime(gitdir / "index", ns=(st.st_atime_ns, st.st_mtime_ns))
    assert "changed: gitdir:index" in trial.tree_changes(before, trial.snapshot(str(root)))
    (root / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n", encoding="utf-8")
    assert "gitdir:index" in trial.snapshot(str(root))                  # a relative gitdir


# ═══════════════════════════════════════════════════════ arms and their comparison

T_A = target("a.go", "A")
T_B = target("a.go", "B")


def _res(**files) -> dict:
    return resolutions(files)


def test_compare_arms_counts_every_definite_other_download_answer_as_changed():
    ext = external("example.com/dep")
    offline = _res(**{"a.go": [record(1, 1, "resolved", target=T_A),          # same in both
                               record(2, 1, "resolved", target=T_A),          # another target
                               record(3, 1, "resolved", target=T_B),          # external in download
                               record(4, 1, "unresolved", reason="missing-module"),
                               record(5, 1, "external", external=external("std")),
                               record(6, 1, "resolved", target=T_A),          # download unresolved
                               record(7, 1, "resolved", target=T_A),          # no download record
                               record(8, 1, "resolved", target=T_A),          # builtin in download
                               record(9, 1, "resolved", target=T_A),          # dynamic in download
                               record(10, 1, "resolved", target=T_A)]})       # ambiguous in download
    download = _res(**{"a.go": [record(1, 1, "resolved", target=T_A),
                                record(2, 1, "resolved", target=T_B),
                                record(3, 1, "external", external=ext),
                                record(4, 1, "external", external=ext),
                                record(5, 1, "external", external=external("std")),
                                record(6, 1, "unresolved", reason="load-error"),
                                record(8, 1, "builtin"),
                                record(9, 1, "dynamic"),
                                record(10, 1, "ambiguous", candidates=[T_A, T_B])]})
    got = trial.compare_arms(offline, download)
    assert got["changed_answers"] == 5
    assert got["changed"] == [
        {"kind": "call", "site": "a.go:2:1:call", "offline": "a.go:A", "download": "a.go:B"},
        {"kind": "call", "site": "a.go:3:1:call", "offline": "a.go:B", "download": "external example.com/dep"},
        {"kind": "call", "site": "a.go:8:1:call", "offline": "a.go:A", "download": "builtin"},
        {"kind": "call", "site": "a.go:9:1:call", "offline": "a.go:A", "download": "dynamic"},
        {"kind": "call", "site": "a.go:10:1:call", "offline": "a.go:A", "download": "ambiguous"}]
    assert got["resolved_only_offline"] == [
        {"kind": "call", "site": "a.go:6:1:call", "offline": "a.go:A", "download": "unresolved (load-error)"},
        {"kind": "call", "site": "a.go:7:1:call", "offline": "a.go:A", "download": "no record"}]
    assert got["answered"] == {"offline": 9, "download": 5}
    assert got["recall_loss"] == 0.0


def test_compare_arms_requires_offline_candidates_to_be_download_candidates():
    sq, circle = target("b.go", "Sq.Area"), target("c.go", "Circle.Area")
    offline = _res(**{"a.go": [record(1, 1, "resolved", target=T_A, candidates=[sq, circle]),
                               record(2, 1, "resolved", target=T_A, candidates=[sq]),
                               record(3, 1, "ambiguous", candidates=[sq, circle]),
                               record(4, 1, "ambiguous", candidates=[sq])]})
    download = _res(**{"a.go": [record(1, 1, "resolved", target=T_A, candidates=[sq]),
                                record(2, 1, "resolved", target=T_A, candidates=[sq, circle]),
                                record(3, 1, "ambiguous", candidates=[circle]),
                                record(4, 1, "unresolved", reason="load-error")]})
    got = trial.compare_arms(offline, download)
    assert got["changed"] == [
        {"kind": "candidates", "site": "a.go:1:1:call", "offline": "c.go:Circle.Area", "download": "not a candidate"},
        {"kind": "candidates", "site": "a.go:3:1:call", "offline": "b.go:Sq.Area", "download": "not a candidate"}]
    assert got["resolved_only_offline"] == [
        {"kind": "candidates", "site": "a.go:4:1:call", "offline": "b.go:Sq.Area",
         "download": "unresolved (load-error)"}]


def _with_relations(res: dict, rel: str, *, imports=(), embeds=(), implements=()) -> dict:
    f = res["files"].setdefault(rel, {"sha256": "x", "calls": [], "imports": [], "embeds": [], "implements": []})
    f["imports"] = list(imports)
    f["embeds"] = list(embeds)
    f["implements"] = list(implements)
    return res


def _embed(type_q, end, status="resolved", pointer=False):
    return {"type": {"relpath": "a.go", "qualname": type_q}, "embedded": end, "status": status,
            "external": None, "pointer": pointer}


def _impl(type_q, end, pointer=False):
    return {"type": {"relpath": "a.go", "qualname": type_q}, "iface": end, "status": "resolved",
            "external": None, "pointer_receiver": pointer}


def test_compare_arms_requires_offline_imports_embeds_and_implements_in_download():
    lib = import_record(3, 2, "example.com/r/lib", directory="lib", clause="lib")
    lib2 = import_record(4, 2, "example.com/r/lib2", directory="lib2", clause="lib2")
    gone = import_record(5, 2, "example.com/r/gone", directory="gone", clause="gone")
    base, other = target("b.go", "Base", kind="class"), target("c.go", "Other", kind="class")
    shape = target("s.go", "Shape", kind="class")
    offline = _with_relations(_res(), "a.go", imports=[lib, lib2, gone],
                              embeds=[_embed("T", base), _embed("U", base)],
                              implements=[_impl("T", shape), _impl("U", shape)])
    download = _with_relations(_res(), "a.go",
                               imports=[lib, import_record(4, 2, "example.com/r/lib2", directory="v2", clause="lib2"),
                                        import_record(5, 2, "example.com/r/gone", status="unresolved")],
                               embeds=[_embed("T", base), _embed("U", other)],
                               implements=[_impl("T", shape)])
    got = trial.compare_arms(offline, download)
    assert got["changed"] == [
        {"kind": "import", "site": "a.go:4:2", "offline": "lib2|lib2", "download": "v2|lib2"},
        {"kind": "embed", "site": "a.go:U", "offline": "b.go:Base", "download": "c.go:Other"},
        {"kind": "implements", "site": "a.go:U", "offline": "s.go:Shape", "download": "not implemented"}]
    assert got["resolved_only_offline"] == [
        {"kind": "import", "site": "a.go:5:2", "offline": "gone|gone", "download": "unresolved"}]
    assert got["changed_answers"] == 3


def test_compare_arms_reports_external_module_and_version_changes():
    offline = _res(**{"a.go": [record(1, 1, "external", external=external("example.com/dep", version="v1.0.0")),
                               record(2, 1, "external", external=external("std"))]})
    download = _res(**{"a.go": [record(1, 1, "external", external=external("example.com/dep", version="v1.2.0")),
                                record(2, 1, "external", external=external("std"))]})
    got = trial.compare_arms(offline, download)
    assert got["changed_answers"] == 0
    assert got["external_changes"] == [{"kind": "call", "site": "a.go:1:1:call",
                                        "offline": "example.com/dep@v1.0.0", "download": "example.com/dep@v1.2.0"}]


def test_compare_arms_reports_offline_external_answers_and_skips_builtin_and_dynamic():
    """An offline external answer writes no MethodCalls edge: another answer in the
    download arm is reported, not failed. Builtin and dynamic sites answer nothing
    (no target), so only the recall counts them."""
    dep = external("example.com/dep", version="v1.0.0")
    offline = _res(**{"a.go": [record(1, 1, "external", external=dep),            # resolved in download
                               record(2, 1, "external", external=dep),            # another module
                               record(3, 1, "external", external=dep),            # download unresolved
                               record(4, 1, "builtin"),                           # dynamic in download
                               record(5, 1, "dynamic", reason="func-value")]})    # resolved in download
    download = _res(**{"a.go": [record(1, 1, "resolved", target=T_A),
                                record(2, 1, "external", external=external("example.com/fork", version="v1.0.0")),
                                record(3, 1, "unresolved", reason="load-error"),
                                record(4, 1, "dynamic"),
                                record(5, 1, "resolved", target=T_B)]})
    got = trial.compare_arms(offline, download)
    assert got["changed_answers"] == 0
    assert got["external_changes"] == [
        {"kind": "call", "site": "a.go:1:1:call", "offline": "example.com/dep@v1.0.0", "download": "a.go:A"},
        {"kind": "call", "site": "a.go:2:1:call", "offline": "example.com/dep@v1.0.0", "download": "example.com/fork@v1.0.0"}]
    assert got["resolved_only_offline"] == [
        {"kind": "call", "site": "a.go:3:1:call", "offline": "external example.com/dep",
         "download": "unresolved (load-error)"}]


def test_compare_arms_of_identical_runs():
    res = _res(**{"a.go": [record(1, 1, "resolved", target=T_A)]})
    got = trial.compare_arms(res, res)
    assert (got["changed_answers"], got["resolved_only_offline"], got["recall_loss"]) == (0, [], 0.0)


def _run(res: dict, *, nodes=None, edges=None, timings=None, scanned=("a.go",)) -> evaluate.RepoRun:
    return evaluate.RepoRun(name="r", dir=".", entities={}, resolutions=res, nodes=nodes or {},
                            edges=edges or {}, scanned=list(scanned), timings=timings or {})


def test_arm_summary_counts_files_packages_sites_and_edges():
    res = _res(**{"a.go": [record(1, 1, "resolved", target=T_A), record(2, 1, "builtin"),
                           record(3, 1, "unresolved", reason="missing-module")]})
    res["stats"] = {"by_class": {"func": 1, "builtin": 1, "unknown": 1}}
    res["groups"] = {"m0": {"status": "ok", "peak_rss_bytes": 123_000_000}}
    run = _run(res, nodes={"Packages": [{"package_id": "p"}], "Files": [{"file_id": "f"}]},
               edges={"MethodCalls": [{}, {}], "PossiblyCalls": []},
               timings={"phase1_scan": 0.5, "phase1c_resolve": 2.0})
    got = trial.arm_summary(run, seconds=3.25, peak_rss=99)
    assert got == {"files": 1, "packages": 1, "sites": 3,
                   "by_status": {"builtin": 1, "resolved": 1, "unresolved": 1},
                   "by_class": {"builtin": 1, "func": 1, "unknown": 1},
                   "by_reason": {"missing-module": 1},
                   "edges": {"MethodCalls": 2, "PossiblyCalls": 0}, "seconds": 3.25,
                   "phases": {"phase1_scan": 0.5, "phase1c_resolve": 2.0},
                   "peak_rss_bytes": 123_000_000,
                   "imports": {"records": 0, "mapped": 0, "unmapped": 0, "ambiguous": 0,
                               "unmapped_examples": [], "ambiguous_examples": []}}
    del res["groups"]
    assert trial.arm_summary(run, seconds=1.0, peak_rss=99)["peak_rss_bytes"] == 99


def test_arm_summary_reports_unmapped_imports_and_the_markdown_lists_the_rows():
    res = resolutions({}, "r", imports={"a.go": [import_record(3, 2, "example.com/r/lib", directory="lib",
                                                               name="lib")]})
    run = evaluate.RepoRun(name="r", dir=".", entities={"lib/lib.go": entity("example.com/r/lib", "lib")},
                           resolutions=res, edges={"ClassEmbeds": [{}], "PackageImports": []})
    got = trial.arm_summary(run, seconds=1.0, peak_rss=1)
    assert got["imports"] == {"records": 1, "mapped": 0, "unmapped": 1, "ambiguous": 0,
                              "unmapped_examples": ["a.go:3:2 example.com/r/lib: the record names no clause"],
                              "ambiguous_examples": []}
    md = trial.summary_markdown({"repo": "r", "date": "2026-10-14", "toolchain": "go1.27.2",
                                 "modes": {"offline": got}, "changed_answers": None,
                                 "resolved_only_offline": [], "recall_loss": None,
                                 "untouched": {"pass": True, "changes": []},
                                 "oracle": {"mode": "offline", "skipped": "--skip-oracle"}, "pass": True})
    assert ("- Rows (offline): PackageImports 0, ClassEmbeds 1, ClassImplements 0; in-repo import "
            "records mapped 0 of 1 (1 unmapped, 0 ambiguous)") in md


# ═══════════════════════════════════════════════════════ oracle records


ENTITIES = {"a.go": {"types": [{"qualname": "Shape", "methods": [{"qualname": "Shape.Area"}]}],
                     "funcs": [{"qualname": "A"}]}}


def test_match_records_are_the_methodcalls_edges():
    res = _res(**{"a.go": [
        record(3, 7, "resolved", name="A", target=target("a.go", "A", line=9)),
        record(4, 9, "resolved", name="Area", target=target("a.go", "Shape.Area", line=2),
               via="types", candidates=[target("b.go", "Sq.Area")]),
        record(5, 2, "resolved", name="T", target=target("a.go", "T", kind="class", line=1)),
        record(6, 2, "external", name="New", target=target("x.go", "New", repo="shared"),
               external=external("example.com/shared", peer=True)),
        record(7, 2, "dynamic", name="f"),
    ]})
    assert trial.match_records(res, ENTITIES) == [
        {"file": "a.go", "line": 3, "col": 7, "name": "A",
         "target": {"file": "a.go", "line": 9, "qualname": "A"}, "note": "types"},
        {"file": "a.go", "line": 4, "col": 9, "name": "Area",
         "target": {"file": "a.go", "line": 2, "qualname": "Shape.Area", "kind": "interface_method"},
         "note": "types", "candidates": [{"file": "b.go", "line": 1, "qualname": "Sq.Area"}]}]


def test_match_records_mark_candidates_reached_through_an_embedded_interface():
    """A via_embedded_iface candidate is an interface method: the second dispatch can run any
    implementer, so goracle lets the method name decide for that edge."""
    rec = record(4, 9, "resolved", name="Area", target=target("a.go", "Shape.Area", line=2),
                 candidates=[target("b.go", "Sq.Area"), target("c.go", "Holder.Area")])
    rec["candidates"][1]["via_embedded_iface"] = True
    assert trial.match_records(_res(**{"a.go": [rec]}), ENTITIES)[0]["candidates"] == [
        {"file": "b.go", "line": 1, "qualname": "Sq.Area"},
        {"file": "c.go", "line": 1, "qualname": "Holder.Area", "via_embedded_iface": True}]


def test_match_records_send_every_interface_candidate_list_unless_it_is_partial():
    """R50 as amended: an interface edge always carries its candidates, an empty list and the
    open world included, so an observed in-repo callee outside them is IFACE-OTHER. Only a
    record marked candidates_partial (G4 "suppressed", "via_embedded_external") carries none,
    and goracle then judges it by the method name alone."""
    def area(line, **kw):
        return record(line, 9, "resolved", name="Area", target=target("a.go", "Shape.Area", line=2), **kw)
    suppressed, external_embed = area(7, open_world=True), area(8, candidates=[target("b.go", "Sq.Area")])
    suppressed["candidates_partial"] = "suppressed"
    external_embed["candidates_partial"] = "via_embedded_external"
    got = trial.match_records(_res(**{"a.go": [area(4), area(5, open_world=True), suppressed, external_embed]}),
                              ENTITIES)
    assert [e.get("candidates") for e in got] == [[], [], None, None]
    assert "candidates" not in got[2] and "candidates" not in got[3]


def test_possibly_calls_recall_counts_observed_callees_among_candidates():
    res = _res(**{"a.go": [
        record(4, 9, "resolved", name="Area", target=target("a.go", "Shape.Area"),
               candidates=[target("b.go", "Sq.Area"), target("c.go", "Circle.Area")]),
        record(8, 3, "ambiguous", name="Level", candidates=[target("d.go", "rule.Level")]),
        record(9, 3, "resolved", name="A", target=target("a.go", "A")),
    ]})
    obs = [{"kind": "site", "file": "a.go", "line": 4, "col": 9, "name": "Area",
            "callees": [{"file": "b.go", "qualname": "Sq.Area"}, {"file": "e.go", "qualname": "Tri.Area"}]},
           {"kind": "site", "file": "a.go", "line": 8, "col": 3, "name": "Level",
            "callees": [{"file": "d.go", "qualname": "rule.Level"}]},
           {"kind": "site", "file": "a.go", "line": 9, "col": 3, "name": "A",
            "callees": [{"file": "a.go", "qualname": "A"}]},
           {"kind": "defer-group", "file": "a.go", "line": 0, "callees": []}]
    assert trial.possibly_calls_recall(obs, res) == {"observed": 3, "covered": 2}


def test_verdict_counts_split_correct_wrong_and_unscored():
    verdicts = [{"verdict": v} for v in ("CONFIRMED", "CONFIRMED", "CONFIRMED-NOEXEC", "IFACE-CONSISTENT",
                                          "DEFER-GROUP-CONSISTENT", "UNEXERCISED", "WRONG-OTHER",
                                          "IFACE-OTHER")]
    verdicts += [{"verdict": "NO-SITE", "file": "gen.go", "line": 3, "col": 2, "target": {"file": "a.go"}},
                 {"verdict": "NO-TARGET", "file": "a.go", "line": 9, "col": 2, "target": {"file": "lined.go"}},
                 {"verdict": "NO-TARGET", "file": "a.go", "line": 7, "col": 9, "target": {"file": "a.go"}},
                 {"verdict": "NO-SITE", "file": "b.go", "line": 2, "col": 5, "target": {"file": "a.go"}},
                 {"verdict": "NO-SITE", "file": "new.go", "line": 1, "col": 1, "target": {"file": "a.go"}}]
    counts = trial.count_verdicts(verdicts)
    assert counts == {"CONFIRMED": 2, "CONFIRMED-NOEXEC": 1, "DEFER-GROUP-CONSISTENT": 1,
                      "IFACE-CONSISTENT": 1, "IFACE-OTHER": 1, "NO-SITE": 3, "NO-TARGET": 2, "UNEXERCISED": 1,
                      "WRONG-OTHER": 1}
    assert trial.wrong_count(counts) == 2
    files = {"gen.go": "skipped:cgo", "lined.go": "skipped:line-directives", "a.go": "instrumented",
             "b.go": "instrumented"}
    got = trial.unscored(verdicts, files)
    assert (got["documented"], got["undocumented"]) == (2, 3)
    assert got["undocumented_examples"] == [
        "NO-TARGET a.go:7:9: target file a.go is instrumented",
        "NO-SITE b.go:2:5: site file b.go is instrumented",
        "NO-SITE new.go:1:1: site file new.go is not in goracle's site table"]
    assert trial.unscored([{"verdict": "NO-SITE", "file": "x.go", "line": 1, "col": 1}],
                          {"x.go": "skipped:no-module"})["undocumented"] == 0
    assert trial.unscored([{"verdict": "NO-TARGET", "file": "a.go", "line": 1, "col": 1,
                            "target": {"file": "p.go"}}], {"p.go": "skipped:parse"})["undocumented"] == 0


def test_the_oracle_block_fails_on_undocumented_unscored_verdicts_and_count_mismatches():
    ok = {"verdicts": {"CONFIRMED": 2}, "wrong": 0, "edges": 2, "error": None,
          "packages": {"p": {"status": "ok", "executed": 2}}, "unscored": {"documented": 0, "undocumented": 0}}
    assert trial._oracle_ok(ok) is True
    assert trial._oracle_ok({**ok, "unscored": {"documented": 1, "undocumented": 1}}) is False
    assert trial._oracle_ok({**ok, "edges": 3}) is False                     # a record got no verdict
    assert trial._oracle_ok({**ok, "edges": 0, "verdicts": {}}) is False      # nothing was scored
    assert trial._oracle_ok({**ok, "record_checks": {"problems": 0}}) is True
    assert trial._oracle_ok({**ok, "record_checks": {"problems": 1}}) is False


def test_executed_verdicts_are_counted_at_each_packages_own_sites():
    verdicts = [{"verdict": "CONFIRMED", "file": "pkg/adt/tree.go"},
                {"verdict": "IFACE-CONSISTENT", "file": "pkg/adt/tree_test.go"},
                {"verdict": "CONFIRMED-NOEXEC", "file": "pkg/adt/sub/x.go"},           # another package
                {"verdict": "UNEXERCISED", "file": "pkg/wait/wait.go"},
                {"verdict": "UNVERIFIABLE-TARGET", "file": "pkg/wait/wait.go"},
                {"verdict": "NO-SITE", "file": "pkg/wait/wait.go"},
                {"verdict": "DEFER-GROUP-CONSISTENT", "file": "main.go"},
                {"verdict": "WRONG-OTHER", "file": "main.go"}]
    assert trial.executed_at(verdicts, ["pkg/adt", "./pkg/wait/", "."]) == {"pkg/adt": 2, "./pkg/wait/": 0, ".": 2}


def _site_table(path: Path, files: dict[str, str], funcs: dict[int, str], sites) -> str:
    rows = [{"kind": "meta", "version": 1}]
    rows += [{"kind": "file", "file": f, "status": s} for f, s in files.items()]
    rows += [{"kind": "func", "fid": fid, "qualname": q} for fid, q in funcs.items()]
    rows += [{"kind": "site", "file": f, "line": line, "col": col, "stmt": "call", "fid": fid}
             for f, line, col, fid in sites]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def test_record_checks_apply_the_name_and_caller_check_to_every_call_record(tmp_path):
    """R50 6(b)(c) through evaluate.record_problems; a file goracle skips on
    purpose has no sites in its table, so its records get the name check only."""
    table = _site_table(tmp_path / "sites.jsonl", {"a.go": "instrumented", "lined.go": "skipped:line-directives"},
                        {1: "main", 2: "helper"}, [("a.go", 3, 2, 1), ("a.go", 4, 2, 1), ("a.go", 5, 2, 1)])
    res = _res(**{"a.go": [record(3, 2, "resolved", name="A", target=target("a.go", "A")),
                           record(4, 2, "resolved", name="B", target=target("a.go", "A")),
                           record(5, 2, "resolved", name="C", caller="helper", target=target("a.go", "C")),
                           record(9, 9, "unresolved", name="G")],
                  "lined.go": [record(2, 3, "resolved", name="D", caller="x", target=target("a.go", "D")),
                               record(3, 3, "resolved", name="E", caller="x", target=target("a.go", "F"))]})
    assert trial.record_checks(res, table) == {"records": 6, "name_only": 2, "problems": 4, "examples": [
        "a.go:4:2 call B: names A",
        "a.go:5:2 call C: caller helper, goracle's table says main",
        "a.go:9:9 call G: no such site in goracle's table",
        "lined.go:3:3 call E: names F"]}


def test_generic_receiver_observations_are_counted_and_not_wrong():
    note = " is a generic implementer, outside the candidate universe"
    verdicts = [{"verdict": "IFACE-CONSISTENT", "detail": "p.Box.Volume, p.Plain.Volume; p.Box.Volume" + note},
                {"verdict": "IFACE-CONSISTENT", "detail": "p.G.M, p.H.M; p.G.M" + note + "; p.H.M" + note},
                {"verdict": "CONFIRMED", "detail": "p.A"}, {"verdict": "UNEXERCISED"}]
    assert trial.generic_observations(verdicts) == 3
    assert trial.wrong_count(trial.count_verdicts(verdicts)) == 0


def test_a_failing_test_package_gets_one_retry_with_its_own_hits_and_log(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []

    def scripted(*codes):
        left = list(codes)

        def sh(argv, cwd, env, log, timeout):
            calls.append((env["GORACLE_OUT"], os.path.basename(log)))
            return left.pop(0)
        return sh

    hits, log = str(tmp_path / "hits" / "pkg"), str(tmp_path / "logs" / "test-pkg.log")
    argv, quiet = ["go", "test", "./pkg"], (lambda *a: None)
    monkeypatch.setattr(trial, "_sh", scripted(0))
    assert trial.run_tests(argv, ".", {}, hits, log, 60, quiet) == {"status": "ok", "log": log, "hits": hits}
    monkeypatch.setattr(trial, "_sh", scripted(1, 0))
    assert trial.run_tests(argv, ".", {}, hits, log, 60, quiet) == {
        "status": "flaky", "log": log, "retry_log": str(tmp_path / "logs" / "test-pkg-retry.log"),
        "hits": hits + "-retry"}                                         # the passing run's hits are reported
    assert calls[1:] == [(hits, "test-pkg.log"), (hits + "-retry", "test-pkg-retry.log")]
    monkeypatch.setattr(trial, "_sh", scripted(1, 1))
    assert trial.run_tests(argv, ".", {}, hits, log, 60, quiet)["status"] == "failed"
    monkeypatch.setattr(trial, "_sh", scripted(1, 124))
    assert trial.run_tests(argv, ".", {}, hits, log, 60, quiet)["status"] == "timeout"


def test_goracle_and_go_test_get_the_build_tags():
    assert trial.instrument_argv("/b/goracle", "/repo", "/o/copy", ("integration", "slicelabels")) == [
        "/b/goracle", "instrument", "--src", "/repo", "--out", "/o/copy", "--tags", "integration,slicelabels"]
    assert trial.instrument_argv("/b/goracle", "/repo", "/o/copy", ()) == [
        "/b/goracle", "instrument", "--src", "/repo", "--out", "/o/copy"]
    assert trial.test_argv("/go/bin/go", "./pkg/adt", ("integration",)) == [
        "/go/bin/go", "test", *trial.TEST_FLAGS, "-tags=integration", "./pkg/adt"]
    assert trial.test_argv("/go/bin/go", ".", ()) == ["/go/bin/go", "test", *trial.TEST_FLAGS, "."]


@pytest.mark.parametrize("repo, given, expected", [
    ("etcd", (), trial.DEFAULT_PACKAGES["etcd"]),
    ("Prometheus", (), trial.DEFAULT_PACKAGES["prometheus"]),
    ("etcd", ("pkg/adt",), ("pkg/adt",)),
    ("myrepo", (), ()),
])
def test_default_packages_come_from_the_oracle_report(repo, given, expected):
    assert trial.packages_for(repo, given) == tuple(expected)
    assert len(trial.DEFAULT_PACKAGES["etcd"]) == 6 and len(trial.DEFAULT_PACKAGES["prometheus"]) == 5


def test_go_test_runs_from_the_module_of_each_package(tmp_path):
    copy = tmp_path / "copy"
    (copy / "server" / "lease").mkdir(parents=True)
    (copy / "server" / "go.mod").write_text("module example.com/server\n", encoding="utf-8")
    (copy / "go.mod").write_text("module example.com/root\n", encoding="utf-8")
    (copy / "pkg" / "adt").mkdir(parents=True)
    assert trial.test_location(str(copy), "server/lease") == (str(copy / "server"), "./lease")
    assert trial.test_location(str(copy), "pkg/adt") == (str(copy), "./pkg/adt")
    assert trial.test_location(str(copy), ".") == (str(copy), ".")


# ═══════════════════════════════════════════════════════ orchestration


def _fake_graph(results: dict, calls: list):
    def graph(repo_root, repo, mode, arm_dir, printer):
        calls.append((mode, arm_dir))
        return results[mode]
    return graph


def _fake_oracle(result: dict, calls: list):
    def oracle(repo_root, repo, run, packages, work_dir, printer):
        calls.append((run.name, tuple(packages), work_dir))
        return result
    return oracle


def _arm(res: dict, *, groups=None, method_calls=1) -> tuple[evaluate.RepoRun, float, int]:
    res = dict(res, groups={"ws0": {"status": "ok"}} if groups is None else groups)
    return _run(res, nodes={"Packages": [{"package_id": "p"}]},
                edges={"MethodCalls": [{}] * method_calls}), 1.5, 1000


ORACLE_OK = {"mode": "download", "verdicts": {"CONFIRMED": 3, "UNEXERCISED": 2}, "wrong": 0, "edges": 5,
             "possibly_calls_recall": {"observed": 1, "covered": 1}, "unscored": {"documented": 0, "undocumented": 0},
             "record_checks": {"records": 9, "name_only": 1, "problems": 0, "examples": []},
             "packages": {"pkg/adt": {"status": "ok", "log": "x", "executed": 3}}, "error": None}


@pytest.fixture
def out(tmp_path, monkeypatch):
    d = tmp_path / "out"
    monkeypatch.setattr(config, "OUTPUT_DIR", str(d))
    return d


def _same() -> dict:
    return _res(**{"a.go": [record(1, 1, "resolved", target=T_A)]})


def test_a_clean_trial_passes_and_writes_the_summary(tmp_path, out):
    root = _repo(tmp_path)
    graph_calls, oracle_calls = [], []
    ok = trial.run_trial(str(root), "etcd", packages=["pkg/adt"], download=True,
                         printer=lambda *a: None,
                         graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same())}, graph_calls),
                         oracle=_fake_oracle(ORACLE_OK, oracle_calls))
    assert ok is True
    base = out.resolve() / "trial" / "etcd"
    assert [m for m, _d in graph_calls] == ["offline", "download"]
    assert graph_calls[0][1] == str(base / "offline") and graph_calls[1][1] == str(base / "download")
    assert oracle_calls == [("r", ("pkg/adt",), str(base / "oracle"))]
    summary = json.loads((base / "summary.json").read_text(encoding="utf-8"))
    assert set(summary) >= {"repo", "date", "toolchain", "modes", "changed_answers", "oracle", "untouched",
                            "pass", "recall_loss", "resolved_only_offline"}
    assert summary["repo"] == "etcd" and summary["changed_answers"] == 0 and summary["pass"] is True
    assert set(summary["modes"]) == {"offline", "download"}
    assert summary["modes"]["download"]["edges"] == {"MethodCalls": 1}
    assert summary["oracle"]["wrong"] == 0 and summary["untouched"] == {"pass": True, "changes": []}
    assert "# Trial: etcd" in (base / "summary.md").read_text(encoding="utf-8")
    rows = verification_table.format_trial_rows(summary)
    assert len(rows) == 2 and rows[1].endswith("| 0 | 3 | 0 |")


def test_a_changed_answer_fails_the_trial(tmp_path, out):
    root = _repo(tmp_path)
    other = _res(**{"a.go": [record(1, 1, "resolved", target=T_B)]})
    ok = trial.run_trial(str(root), "etcd", download=True, printer=lambda *a: None,
                         graph=_fake_graph({"offline": _arm(_same()), "download": _arm(other)}, []),
                         oracle=_fake_oracle(ORACLE_OK, []))
    summary = json.loads((out / "trial" / "etcd" / "summary.json").read_text(encoding="utf-8"))
    assert ok is False and summary["changed_answers"] == 1 and summary["pass"] is False


def test_a_wrong_oracle_verdict_or_a_failed_package_fails_the_trial(tmp_path, out):
    root = _repo(tmp_path)
    wrong = {**ORACLE_OK, "verdicts": {"CONFIRMED": 3, "WRONG-OTHER": 1}, "wrong": 1}
    assert trial.run_trial(str(root), "etcd", download=True, printer=lambda *a: None,
                           graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same())}, []),
                           oracle=_fake_oracle(wrong, [])) is False
    failed = {**ORACLE_OK, "packages": {"pkg/adt": {"status": "failed", "log": "x"}}}
    assert trial.run_trial(str(root), "etcd", download=True, printer=lambda *a: None,
                           graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same())}, []),
                           oracle=_fake_oracle(failed, [])) is False


def test_a_flaky_test_package_passes_and_both_it_and_generic_observations_are_reported(tmp_path, out):
    root = _repo(tmp_path)
    flaky = {**ORACLE_OK, "generic_observations": 2,
             "packages": {"pkg/adt": {"status": "flaky", "log": "x", "retry_log": "y", "executed": 3}}}
    lines: list[str] = []
    assert trial.run_trial(str(root), "etcd", download=True, printer=lines.append,
                           graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same())}, []),
                           oracle=_fake_oracle(flaky, [])) is True
    assert "- Test packages flaky (passed on retry): pkg/adt" in lines
    assert ("- Oracle (download records): WRONG 0; CONFIRMED 3, UNEXERCISED 2; "
            "generic-receiver observations 2 (allowed, not wrong)") in lines


@pytest.mark.parametrize("arm, groups", [
    ("offline", {"ws0": {"status": "error", "reason": "load-error", "message": "helper exited 2"}}),
    ("download", {"ws0": {"status": "toolchain", "reason": "toolchain"}}),
    ("download", {}),
])
def test_a_group_not_ok_after_the_load_fails_the_trial(tmp_path, out, arm, groups):
    root = _repo(tmp_path)
    results = {"offline": _arm(_same()), "download": _arm(_same())}
    results[arm] = _arm(_same(), groups=groups)
    lines: list[str] = []
    ok = trial.run_trial(str(root), "etcd", download=True, printer=lines.append,
                         graph=_fake_graph(results, []), oracle=_fake_oracle(ORACLE_OK, []))
    summary = json.loads((out / "trial" / "etcd" / "summary.json").read_text(encoding="utf-8"))
    assert ok is False and summary["pass"] is False
    assert any(f.startswith(f"{arm} arm:") for f in summary["failures"]), summary["failures"]


def test_an_arm_without_methodcalls_edges_fails_the_trial(tmp_path, out):
    root = _repo(tmp_path)
    ok = trial.run_trial(str(root), "etcd", download=True, printer=lambda *a: None,
                         graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same(), method_calls=0)},
                                           []),
                         oracle=_fake_oracle(ORACLE_OK, []))
    summary = json.loads((out / "trial" / "etcd" / "summary.json").read_text(encoding="utf-8"))
    assert ok is False and "download arm: 0 MethodCalls edges" in summary["failures"]


def test_a_verdict_count_different_from_the_record_count_fails_the_trial(tmp_path, out):
    root = _repo(tmp_path)
    short = {**ORACLE_OK, "edges": 6}
    assert trial.run_trial(str(root), "etcd", download=True, printer=lambda *a: None,
                           graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same())}, []),
                           oracle=_fake_oracle(short, [])) is False


def test_a_package_that_ran_without_an_executed_verdict_fails_the_trial(tmp_path, out):
    """The execution floor: all UNEXERCISED at a package whose tests passed means
    goracle's runtime recorded nothing for it."""
    root = _repo(tmp_path)
    silent = {**ORACLE_OK, "packages": {"pkg/adt": {"status": "ok", "log": "x", "executed": 0}}}
    assert trial.run_trial(str(root), "etcd", download=True, printer=lambda *a: None,
                           graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same())}, []),
                           oracle=_fake_oracle(silent, [])) is False
    summary = json.loads((out / "trial" / "etcd" / "summary.json").read_text(encoding="utf-8"))
    assert summary["failures"] == ["oracle: test package pkg/adt ran but yielded no executed verdict at its own sites"]


def test_a_record_failing_the_name_or_caller_check_fails_the_trial(tmp_path, out):
    root = _repo(tmp_path)
    bad = {**ORACLE_OK, "record_checks": {"records": 9, "name_only": 1, "problems": 1,
                                          "examples": ["a.go:4:2 call B: names A"]}}
    lines: list[str] = []
    assert trial.run_trial(str(root), "etcd", download=True, printer=lines.append,
                           graph=_fake_graph({"offline": _arm(_same()), "download": _arm(_same())}, []),
                           oracle=_fake_oracle(bad, [])) is False
    assert "- Name and caller check: 1 problems in 9 records (1 in files goracle skips, name check only)" in lines
    assert "  - a.go:4:2 call B: names A" in lines


def test_a_write_into_the_repo_fails_the_trial(tmp_path, out):
    root = _repo(tmp_path)

    def writing_graph(repo_root, repo, mode, arm_dir, printer):
        Path(repo_root, "go.work.sum").write_text("x", encoding="utf-8")
        return _arm(_same())

    ok = trial.run_trial(str(root), "etcd", printer=lambda *a: None, graph=writing_graph,
                         oracle=_fake_oracle(ORACLE_OK, []))
    summary = json.loads((out / "trial" / "etcd" / "summary.json").read_text(encoding="utf-8"))
    assert ok is False and summary["untouched"]["pass"] is False
    assert "added: go.work.sum" in summary["untouched"]["changes"]


def test_offline_only_trial_scores_the_offline_records(tmp_path, out):
    root = _repo(tmp_path)
    graph_calls, oracle_calls = [], []
    ok = trial.run_trial(str(root), "etcd", printer=lambda *a: None,
                         graph=_fake_graph({"offline": _arm(_same())}, graph_calls),
                         oracle=_fake_oracle({**ORACLE_OK, "mode": "offline"}, oracle_calls))
    summary = json.loads((out / "trial" / "etcd" / "summary.json").read_text(encoding="utf-8"))
    assert ok is True and [m for m, _d in graph_calls] == ["offline"]
    assert set(summary["modes"]) == {"offline"} and summary["changed_answers"] is None
    assert oracle_calls[0][1] == trial.DEFAULT_PACKAGES["etcd"]


def test_only_skip_oracle_skips_the_precision_check(tmp_path, out):
    root = _repo(tmp_path)
    calls: list = []
    graph = _fake_graph({"offline": _arm(_same())}, [])
    assert trial.run_trial(str(root), "etcd", skip_oracle=True, printer=lambda *a: None, graph=graph,
                           oracle=_fake_oracle(ORACLE_OK, calls)) is True
    summary = json.loads((out / "trial" / "etcd" / "summary.json").read_text(encoding="utf-8"))
    assert calls == [] and summary["oracle"]["skipped"] == "--skip-oracle"
    assert trial.run_trial(str(root), "myrepo", printer=lambda *a: None, graph=graph,
                           oracle=_fake_oracle(ORACLE_OK, calls)) is False     # no default packages
    summary = json.loads((out / "trial" / "myrepo" / "summary.json").read_text(encoding="utf-8"))
    assert calls == [] and "pass --packages or --skip-oracle" in summary["oracle"]["error"]
    assert trial.run_trial(str(root), "myrepo", skip_oracle=True, printer=lambda *a: None, graph=graph,
                           oracle=_fake_oracle(ORACLE_OK, calls)) is True


def test_an_oracle_error_fails_the_trial(tmp_path, out):
    root = _repo(tmp_path)
    broken = {"mode": "download", "verdicts": {}, "wrong": 0, "possibly_calls_recall": None,
              "packages": {}, "error": "goracle is pinned to go1.27, the toolchain is go1.28.0"}
    assert trial.run_trial(str(root), "etcd", printer=lambda *a: None,
                           graph=_fake_graph({"offline": _arm(_same())}, []),
                           oracle=_fake_oracle(broken, [])) is False


@pytest.mark.parametrize("exc", [
    trial.go_toolchain.ToolchainError("no Go toolchain found"),
    trial.go_toolchain.HelperBuildError("building the helper failed"),
    NotImplementedError("derive_edge_rows is not implemented yet (WP-B3)"),
])
def test_a_graph_stage_that_cannot_run_fails_cleanly(tmp_path, out, exc):
    root = _repo(tmp_path)

    def graph(*_a):
        raise exc
    lines: list[str] = []
    assert trial.run_trial(str(root), "etcd", printer=lines.append, graph=graph,
                           oracle=_fake_oracle(ORACLE_OK, [])) is False
    assert any(str(exc) in l for l in lines)


def test_output_inside_the_repo_is_refused(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(root / "out"))
    lines: list[str] = []
    assert trial.run_trial(str(root), "etcd", printer=lines.append,
                           graph=_fake_graph({}, []), oracle=_fake_oracle(ORACLE_OK, [])) is False
    assert any("OUTPUT_DIR" in l and "outside" in l for l in lines)
    assert not (root / "out").exists()


def test_the_cli_passes_the_flags(monkeypatch, tmp_path):
    from graph_generator import __main__ as cli
    seen = {}

    def fake_run_trial(target_dir, repo, **kw):
        seen.update(target_dir=target_dir, repo=repo, **kw)
        return True
    monkeypatch.setattr(trial, "run_trial", fake_run_trial)
    monkeypatch.setattr("sys.argv", ["graph_generator", "trial", str(tmp_path), "--repo-name", "etcd",
                                     "--packages", "pkg/adt", "storage", "--download-modules",
                                     "--skip-oracle"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen == {"target_dir": str(tmp_path), "repo": "etcd", "packages": ["pkg/adt", "storage"],
                    "download": True, "skip_oracle": True}
