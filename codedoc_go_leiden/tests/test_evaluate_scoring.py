"""evaluate: one verdict per case (spec §9.2 item 4, §9.5 "Wrong", master C10)."""
from __future__ import annotations

import copy

import pytest

from graph_generator import evaluate
from tests.eval_worlds import external, perfect_runs, record, run, target

F = "pkg/a.go"


def _t(qualname: str, file: str = F, kind: str = "method", module: str = "example.com/fx") -> dict:
    """An expected target (manifest shape)."""
    return {"qualname": qualname, "module": module, "kind": kind, "file": file, "line": 1, "col": 1}


def _expect(status: str, *, targets=(), candidates=(), static=None, runtime=(), open_world=None,
            module=None, reason=None, distractors=()) -> dict:
    return {"status": status, "targets": list(targets), "candidates": list(candidates),
            "static_target": static, "runtime_targets": list(runtime), "open_world": open_world,
            "external": {"module": module} if module else None, "reason": reason,
            "distractors": list(distractors)}


def _case(expect: dict, *, line: int = 10, col: int = 5, kind: str = "call", offline=None,
          file: str = F, category: str = "CAT") -> dict:
    out = {"id": f"{category}-001", "category": category, "file": file, "line": line, "col": col,
           "site_kind": kind, "expect": expect}
    if offline is not None:
        out["expect_offline"] = offline
    return out


def _score(case: dict, *recs: dict, mode: str = "env", runs=None) -> str:
    runs = runs or {"fx": run(files={F: list(recs)})}
    records = evaluate.fixture_records(runs)
    return evaluate.score_case(case, records, mode=mode)


T_SEND = target(F, "Notifier.Send")
T_MAIL = target(F, "MailNotifier.Send")
T_SMS = target(F, "SmsNotifier.Send")
IFACE = _expect("resolved", targets=[_t("Notifier.Send")],
                candidates=[_t("MailNotifier.Send"), _t("SmsNotifier.Send")],
                runtime=[_t("SmsNotifier.Send")], open_world=False,
                distractors=[_t("FaxNotifier.Send")])


@pytest.mark.parametrize("recs, verdict", [
    ([record(10, 5, "resolved", target=T_SEND, candidates=[T_MAIL, T_SMS])], "correct"),
    ([record(10, 5, "resolved", target=T_SEND, candidates=[T_SMS])], "correct"),        # subset
    ([record(10, 5, "resolved", target=T_SEND, candidates=[T_SMS, target(F, "Other.Send")])],
     "wrong"),                                                                         # outside set (R50)
    ([record(10, 5, "resolved", target=T_SEND,
             candidates=[target(F, f"Other{i}.Send") for i in range(6)])],
     "unanswered"),                                                                    # above the cap: no rows
    ([record(10, 5, "resolved", target=T_SEND, candidates=[target(F, "FaxNotifier.Send")])],
     "wrong"),                                                                         # distractor
    ([record(10, 5, "resolved", target=T_SEND, candidates=[T_SMS], open_world=True)],
     "unanswered"),                                                                    # flag differs
    ([record(10, 5, "resolved", target=T_SMS)], "wrong"),                               # runtime target (R50)
    ([record(10, 5, "resolved", target=T_SMS, candidates=[target(F, "FaxNotifier.Send")])],
     "wrong"),                                                                         # + distractor
    ([record(10, 5, "resolved", target=target(F, "FaxNotifier.Send"))], "wrong"),
    ([record(10, 5, "resolved", target=target(F, "Unrelated"))], "wrong"),
    ([record(10, 5, "external", external=external("std"))], "wrong"),                   # external, in-repo expected
    ([record(10, 5, "dynamic")], "unanswered"),
    ([record(10, 5, "unresolved", reason="missing-module")], "unanswered"),
    ([], "unanswered"),
    ([record(10, 6, "resolved", target=T_SEND)], "unanswered"),                         # other column
    ([record(10, 5, "resolved", kind="defer", target=T_SEND)], "unanswered"),           # other site kind
])
def test_interface_call(recs, verdict):
    assert _score(_case(IFACE), *recs) == verdict


def test_resolved_with_a_static_target():
    expect = _expect("resolved", targets=[_t("F")], static=_t("G"))
    assert _score(_case(expect), record(10, 5, "resolved", target=target(F, "F"),
                                        static=target(F, "G"))) == "correct"
    assert _score(_case(expect), record(10, 5, "resolved", target=target(F, "F"))) == "unanswered"
    assert _score(_case(expect), record(10, 5, "resolved", target=target(F, "G"))) == "alternate"
    # an alternate needs the candidate rule too; a candidate where none is expected is a wrong edge
    assert _score(_case(expect), record(10, 5, "resolved", target=target(F, "G"),
                                        candidates=[target(F, "H")])) == "wrong"


def test_interface_targets_known_from_the_entities_allow_no_alternates():
    # the manifest gives open_world only for interface calls; the entities say so too
    expect = _expect("resolved", targets=[_t("Shape.Area")], runtime=[_t("Sq.Area")])
    rec = record(10, 5, "resolved", target=target(F, "Sq.Area"))
    records = evaluate.fixture_records({"fx": run(files={F: [rec]})})
    assert evaluate.score_case(_case(expect), records) == "alternate"
    assert evaluate.score_case(_case(expect), records, iface={(F, "Shape.Area")}) == "wrong"


def test_target_identity_is_file_qualname_and_kind():
    expect = _expect("resolved", targets=[_t("Name", file="pkg/a_linux.go")],
                     distractors=[_t("Name", file="pkg/a_windows.go")])
    linux = record(10, 5, "resolved", target=target("pkg/a_linux.go", "Name"))
    windows = record(10, 5, "resolved", target=target("pkg/a_windows.go", "Name"))
    as_class = record(10, 5, "resolved", target=target("pkg/a_linux.go", "Name", kind="class"))
    assert _score(_case(expect), linux) == "correct"
    assert _score(_case(expect), windows) == "wrong"
    assert _score(_case(expect), as_class) == "unanswered"   # no MethodCalls edge, not the answer


def test_build_variants_never_count_as_naming_a_distractor():
    expect = _expect("resolved", targets=[_t("Name", file="pkg/a_linux.go")],
                     distractors=[_t("Name", file="pkg/a_windows.go")])
    rec = record(10, 5, "resolved", target=target("pkg/a_linux.go", "Name"))
    rec["build_variants"] = [target("pkg/a_windows.go", "Name")]
    assert _score(_case(expect), rec) == "correct"


@pytest.mark.parametrize("recs, verdict", [
    ([record(10, 5, "external", external=external("std"))], "correct"),
    ([record(10, 5, "external", external=external("example.com/thirdparty/textfmt"))], "wrong"),
    ([record(10, 5, "builtin")], "unanswered"),
    ([record(10, 5, "unresolved", reason="missing-module")], "unanswered"),
    ([record(10, 5, "resolved", target=target(F, "New"))], "wrong"),
    ([record(10, 5, "external", target=target("log/logger.go", "New", repo="shared"),
             external=external("std", peer=True))], "wrong"),                         # a peer edge
    ([record(10, 5, "external", external=external("std"), candidates=[target(F, "Writer.Write")])],
     "wrong"),                                                                         # rows where none expected
    ([record(10, 5, "external", external=external("std"), candidates=[target("pkg/b.go", "New")])],
     "wrong"),                                                                         # distractor, external path
])
def test_external_matches_the_module(recs, verdict):
    expect = _expect("external", module="std", distractors=[_t("New", file="pkg/b.go")])
    assert _score(_case(expect), *recs) == verdict


def _f3_runs(*recs: dict) -> dict:
    return {"web": run("web", "web", files={"orders/orders.go": list(recs)}),
            "shared": run("shared", "shared")}


@pytest.mark.parametrize("rec, verdict", [
    (record(14, 35, "external", target=target("log/logger.go", "New", repo="shared",
                                              unit="example.com/iryo/shared"),
            external=external("example.com/iryo/shared", peer=True)), "correct"),
    (record(14, 35, "external", target=target("log/other.go", "New", repo="shared"),
            external=external("example.com/iryo/shared", peer=True)), "wrong"),
    (record(14, 35, "external", external=external("example.com/iryo/shared")), "unanswered"),
    (record(14, 35, "external", target=target("log/logger.go", "New", repo="shared"),
            external=external("example.com/iryo/other", peer=True)), "wrong"),
])
def test_peer_target_maps_into_its_repo_directory(rec, verdict):
    expect = _expect("external", module="example.com/iryo/shared",
                     targets=[{**_t("New", file="shared/log/logger.go",
                                    module="example.com/iryo/shared"), "repo": "shared"}])
    case = _case(expect, line=14, col=35, file="web/orders/orders.go")
    assert _score(case, runs=_f3_runs(rec)) == verdict


@pytest.mark.parametrize("cands, open_world, verdict", [
    ([target(F, "Clock.Now")], None, "correct"),
    ([], None, "correct"),                                                             # subset rule
    ([target(F, "Clock.Now"), target(F, "Logger.Info")], None, "wrong"),               # outside the set
    ([target(F, "Clock.Now")], True, "unanswered"),                                    # open_world differs
])
def test_external_interface_calls_check_candidates_and_open_world(cands, open_world, verdict):
    expect = _expect("external", module="example.com/thirdparty/clock", candidates=[_t("Clock.Now")],
                     open_world=False)
    rec = record(10, 5, "external", external=external("example.com/thirdparty/clock"), candidates=cands,
                 open_world=bool(open_world))
    assert _score(_case(expect), rec) == verdict


def test_an_ambiguous_record_naming_a_distractor_as_its_target_is_wrong():
    expect = _expect("ambiguous", candidates=[_t("rule.Level")], distractors=[_t("Other.Level")])
    rec = record(10, 5, "ambiguous", candidates=[target(F, "rule.Level")])
    rec["target"] = target(F, "Other.Level")
    assert _score(_case(expect), rec) == "wrong"


def test_published_external_must_not_name_a_peer():
    expect = _expect("external", module="example.com/iryo/textkit")
    case = _case(expect, line=14, col=35, file="web/orders/orders.go")
    peer = record(14, 35, "external", target=target("textkit/textkit.go", "Banner", repo="shared"),
                  external=external("example.com/iryo/textkit", peer=True))
    plain = record(14, 35, "external", external=external("example.com/iryo/textkit"))
    assert _score(case, runs=_f3_runs(peer)) == "wrong"
    assert _score(case, runs=_f3_runs(plain)) == "correct"


@pytest.mark.parametrize("recs, verdict", [
    ([], "correct"),
    ([record(10, 5, "unresolved", reason="no-declaration")], "unanswered"),
    ([record(10, 5, "resolved", target=target(F, "Yen.Add"))], "wrong"),
    ([record(10, 5, "ambiguous", candidates=[target(F, "Other")])], "wrong"),
    ([record(10, 5, "external", external=external("std", qualname="Duration"))], "wrong"),   # R50
])
def test_conversion_is_no_record(recs, verdict):
    expect = _expect("conversion", distractors=[_t("Yen.Add")])
    assert _score(_case(expect), *recs) == verdict


@pytest.mark.parametrize("recs, verdict", [
    ([record(10, 5, "ambiguous", candidates=[target(F, "rule.Level"),
                                             target("pkg/a_test.go", "Policy.Level")])], "correct"),
    ([record(10, 5, "ambiguous", candidates=[target(F, "rule.Level")])], "correct"),
    ([record(10, 5, "ambiguous", candidates=[target(F, "Elsewhere.Level")])], "wrong"),   # R50
    ([record(10, 5, "resolved", target=target(F, "rule.Level"))], "wrong"),
    ([record(10, 5, "dynamic")], "unanswered"),
])
def test_ambiguous_uses_the_subset_rule(recs, verdict):
    expect = _expect("ambiguous", candidates=[_t("rule.Level"), _t("Policy.Level", file="pkg/a_test.go")])
    assert _score(_case(expect), *recs) == verdict


@pytest.mark.parametrize("status, recs, verdict", [
    ("dynamic", [record(10, 5, "dynamic")], "correct"),
    ("dynamic", [record(10, 5, "resolved", target=target(F, "g"))], "wrong"),
    ("dynamic", [record(10, 5, "ambiguous", candidates=[target(F, "g")])], "wrong"),   # R50: rows
    ("dynamic", [record(10, 5, "dynamic", candidates=[target(F, "g")])], "correct"),  # no rows for dynamic
    ("builtin", [record(10, 5, "builtin")], "correct"),
    ("builtin", [record(10, 5, "external", external=external("std"))], "unanswered"),
    ("builtin", [record(10, 5, "resolved", target=target(F, "len"))], "wrong"),
])
def test_dynamic_and_builtin(status, recs, verdict):
    assert _score(_case(_expect(status)), *recs) == verdict


@pytest.mark.parametrize("expected_reason, recs, verdict", [
    ("excluded-by-build", [record(10, 5, "unresolved", reason="excluded-by-build")], "correct"),
    ("excluded-by-build", [record(10, 5, "unresolved", reason="load-error")], "unanswered"),
    (None, [record(10, 5, "unresolved", reason="load-error")], "correct"),
    ("toolchain", [record(10, 5, "resolved", target=target(F, "local"))], "wrong"),
    ("toolchain", [record(10, 5, "external", external=external("std"))], "unanswered"),
    ("toolchain", [record(10, 5, "dynamic")], "unanswered"),
])
def test_unresolved_reasons(expected_reason, recs, verdict):
    expect = _expect("unresolved", reason=expected_reason)
    assert _score(_case(expect), *recs) == verdict


def test_offline_runs_use_expect_offline_when_given():
    env = _expect("resolved", targets=[_t("F")])
    off = _expect("unresolved", reason="missing-module")
    case = _case(env, offline=off)
    unresolved = record(10, 5, "unresolved", reason="missing-module")
    resolved = record(10, 5, "resolved", target=target(F, "F"))
    assert _score(case, unresolved, mode="offline") == "correct"
    assert _score(case, resolved, mode="offline") == "wrong"
    assert _score(case, resolved, mode="env") == "correct"
    assert _score(_case(env, offline=None), resolved, mode="offline") == "correct"
    assert evaluate.case_expect({"expect": env}, "offline") is env


def test_offline_third_party_unresolved_is_unanswered_not_wrong():
    expect = _expect("external", module="example.com/thirdparty/textfmt")
    rec = record(10, 5, "unresolved", reason="missing-module")
    assert _score(_case(expect), rec, mode="offline") == "unanswered"


def test_records_are_found_by_fixture_relative_file():
    runs = {"badvendor": run("badvendor", "badvendor",
                             files={"main.go": [record(10, 20, "unresolved", reason="load-error")]})}
    records = evaluate.fixture_records(runs)
    assert list(records) == ["badvendor/main.go"]
    case = _case(_expect("unresolved", reason="load-error"), line=10, col=20, file="badvendor/main.go")
    assert evaluate.score_case(case, records, mode="env") == "correct"


@pytest.mark.parametrize("fixture", ["iryo-kanri-system", "go_layouts", "multi_repo"])
@pytest.mark.parametrize("offline", [False, True])
def test_perfect_runs_of_the_real_manifests_are_all_correct(fixture, offline):
    m, runs = perfect_runs(fixture, offline=offline)
    records = evaluate.fixture_records(runs)
    mode = "offline" if offline else "env"
    verdicts = {c["id"]: evaluate.score_case(c, records, mode=mode) for c in m["cases"]}
    assert set(verdicts.values()) == {"correct"}, {k: v for k, v in verdicts.items() if v != "correct"}


def test_a_planted_distractor_in_a_real_manifest_is_wrong():
    m, runs = perfect_runs("iryo-kanri-system")
    case = next(c for c in m["cases"] if c["id"] == "BLD-001")
    distractor = case["expect"]["distractors"][0]
    rel = distractor["file"]
    recs = runs["iryo-kanri-system"].resolutions["files"][case["file"]]["calls"]
    rec = next(r for r in recs if (r["site"]["line"], r["site"]["col"]) == (case["line"], case["col"]))
    rec["target"] = target(rel, distractor["qualname"], unit=distractor["module"])
    records = evaluate.fixture_records(runs)
    assert evaluate.score_case(case, records, mode="env") == "wrong"


def test_completeness_and_rates():
    assert [evaluate.completeness_min(n) for n in (0, 1, 20, 100, 149)] == [1, 1, 18, 86, 127]
    assert evaluate.meets_rate(85, 100) and not evaluate.meets_rate(84, 100)
    assert not evaluate.meets_rate(0, 0)                    # an empty denominator fails (R50)


def test_per_category_counts():
    results = [{"category": "IFC", "verdict": v} for v in ("correct", "alternate", "wrong", "unanswered")]
    assert evaluate.per_category(results) == {"IFC": {"total": 4, "complete": 2, "sound": 3}}


def test_scoring_does_not_mutate_the_records():
    runs = {"fx": run(files={F: [record(10, 5, "resolved", target=T_SEND)]})}
    before = copy.deepcopy(runs["fx"].resolutions)
    evaluate.score_case(_case(IFACE), evaluate.fixture_records(runs), mode="env")
    assert runs["fx"].resolutions == before
