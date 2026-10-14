"""scripts.verification_table: evaluate reports and trial summaries become the rows
of the verification tables in docs/LSP_RESOLUTION.md."""
from __future__ import annotations

import json

import pytest

from scripts import verification_table as vt
from scripts.verification_table import format_row


def _report(**over) -> dict:
    base = {"fixture": "iryo-kanri-system", "mode": "no-env", "cases": 120,
            "verdicts": {"correct": 110, "alternate": 2, "wrong": 0, "unanswered": 8},
            "completeness": {"value": 112, "required": 103, "pass": True},
            "node_coverage": {"value": 0.967, "pass": True},
            "edge_rate": {"value": 0.9123, "pass": True},
            "qa": {"value": 0.95, "pass": True}, "cross_repo": None,
            "per_category": {}, "edges": {"precision": 1.0, "recall": 0.9}, "misses": [],
            "pass": True}
    base.update(over)
    return base


@pytest.mark.parametrize("report, expected", [
    (_report(),
     "| `iryo-kanri-system` | 既定 | 120 | 110 | 2 | 0 | 8 | 112（必要 103） | 0.967 | 0.912 | 0.950 | — | — | — | PASS |"),
    (_report(mode="env", edge_rate={"value": 0.8, "pass": False}, **{"pass": False}),
     "| `iryo-kanri-system` | `--env` | 120 | 110 | 2 | 0 | 8 | 112（必要 103） | 0.967 | 0.800（未達） | 0.950 | — | — | — | FAIL |"),
    (_report(fixture="multi_repo", cases=12, node_coverage=None, edge_rate=None, qa=None,
             verdicts={"correct": 12, "alternate": 0, "wrong": 0, "unanswered": 0},
             completeness={"value": 12, "required": 11, "pass": True},
             cross_repo={"expected": 9, "actual": 9, "missing": [], "extra": [], "pass": True}),
     "| `multi_repo` | 既定 | 12 | 12 | 0 | 0 | 0 | 12（必要 11） | — | — | — | 一致 | — | — | PASS |"),
    (_report(fixture="multi_repo", node_coverage=None, edge_rate=None, qa=None,
             cross_repo={"expected": 9, "actual": 8, "missing": ["a"], "extra": [], "pass": False}),
     "| `multi_repo` | 既定 | 120 | 110 | 2 | 0 | 8 | 112（必要 103） | — | — | — | 不一致（不足 1、余分 0） | — | — | PASS |"),
    ({"fixture": "go_layouts", "mode": "env", "error": "toolchain not found"},
     "| `go_layouts` | `--env` | エラー: toolchain not found |"),
])
def test_format_row(report, expected):
    assert format_row(report) == expected


def _imports(mapped: int, records: int, *, gated: bool = True, ambiguous: int = 0) -> dict:
    unmapped = records - mapped - ambiguous
    return {"records": records, "mapped": mapped, "unmapped": unmapped, "ambiguous": ambiguous,
            "unmapped_examples": [], "ambiguous_examples": [], "pass": unmapped == 0, "gated": gated}


@pytest.mark.parametrize("imports, cell", [
    (_imports(89, 89), "89/89"),
    (_imports(0, 89), "0/89（未達）"),
    (_imports(14, 14, gated=False), "14/14（参考）"),
    (_imports(12, 14, gated=False, ambiguous=2), "12/14（参考、曖昧 2）"),
    (_imports(87, 90, ambiguous=2), "87/90（曖昧 2、未達）"),
    (_imports(0, 0), "0/0"),
])
def test_the_imports_cell_shows_mapped_of_records(imports, cell):
    cells = _cells(format_row(_report(imports=imports)))
    assert cells[vt.EVAL_HEADER.split("|").index(" インポート ") - 1] == cell


def _oracle(edges: int, wrong: int = 0, *, ok: bool = True, verdicts: dict | None = None) -> dict:
    if verdicts is None:
        verdicts = {"CONFIRMED": edges - wrong, "WRONG-OTHER": wrong} if edges else {}
    return {"problems": 0, "edges": edges, "wrong": wrong, "verdicts": verdicts, "pass": ok}


@pytest.mark.parametrize("oracle, cell", [
    (_oracle(244), "244 / 0"),
    (_oracle(244, 1, ok=False), "244 / 1（未達）"),
    (_oracle(244, ok=False), "244 / 0（未達）"),                 # NO-TARGET, a failed entry, ...
    (_oracle(0), "—"),                                          # go_layouts: the site table only
    (_oracle(0, ok=False), "—（未達）"),                         # a record problem, or no goracle
    (None, "—"),
])
def test_the_oracle_cell_shows_judged_edges_and_wrong(oracle, cell):
    cells = _cells(format_row(_report(oracle=oracle)))
    assert cells[vt.EVAL_HEADER.split("|").index(" オラクル ") - 1] == cell


def test_trial_rows_show_the_share_of_mapped_imports():
    summary = _summary()
    summary["modes"]["offline"]["imports"] = {"records": 2095, "mapped": 2095, "unmapped": 0, "ambiguous": 0}
    summary["modes"]["download"]["imports"] = {"records": 8, "mapped": 7, "unmapped": 1, "ambiguous": 0}
    column = vt.TRIAL_HEADER.split("|").index(" インポート ") - 1
    offline, download = (_cells(r)[column] for r in vt.format_trial_rows(summary))
    assert (offline, download) == ("100.0%", "87.5%")
    summary["modes"]["download"]["imports"] = {"records": 0, "mapped": 0, "unmapped": 0, "ambiguous": 0}
    assert _cells(vt.format_trial_rows(summary)[1])[column] == "—"


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def test_rows_have_as_many_cells_as_the_headers():
    assert len(_cells(format_row(_report()))) == len(_cells(vt.EVAL_HEADER))
    rows = vt.format_trial_rows(_summary())
    assert all(len(_cells(r)) == len(_cells(vt.TRIAL_HEADER)) for r in rows)


def _summary(**over) -> dict:
    """The trial's summary.json (WP-B6): the oracle scores the download-mode records."""
    base = {
        "repo": "etcd", "date": "2026-10-14", "toolchain": "go1.27.2",
        "modes": {
            "offline": {"files": 1840, "packages": 183, "sites": 72623,
                        "by_status": {"resolved": 30000, "external": 38000, "unresolved": 1200,
                                      "builtin": 3000, "dynamic": 400},
                        "edges": {"MethodCalls": 25000, "PossiblyCalls": 900},
                        "seconds": 12.34, "peak_rss_bytes": 1_700_000_000},
            "download": {"files": 1840, "packages": 183, "sites": 72623,
                         "by_status": {"resolved": 30100, "external": 39000, "unresolved": 100},
                         "edges": {"MethodCalls": 25100, "PossiblyCalls": 910},
                         "seconds": 15.0, "peak_rss_bytes": 1_650_000_000}},
        "changed_answers": 0,
        "oracle": {"verdicts": {"CONFIRMED": 2500, "CONFIRMED-NOEXEC": 300, "IFACE-CONSISTENT": 80,
                                "UNEXERCISED": 9000, "WRONG-OTHER": 1},
                   "wrong": 1, "possibly_calls_recall": {"observed": 10, "covered": 9}}}
    base.update(over)
    return base


def test_trial_rows_per_mode():
    assert vt.format_trial_rows(_summary()) == [
        "| `etcd` | オフライン | 1840 | 183 | 72623 | 30000 | 38000 | 1200 | 25000 | 900 | — "
        "| 12.3 | 1.7 GB | — | — | — |",
        "| `etcd` | ダウンロード | 1840 | 183 | 72623 | 30100 | 39000 | 100 | 25100 | 910 | — "
        "| 15.0 | 1.6 GB | 0 | 2880 | 1 |",
    ]


def test_trial_oracle_of_an_offline_only_trial_goes_on_the_offline_row():
    summary = _summary(oracle={"mode": "offline", "verdicts": {"CONFIRMED": 5}, "wrong": 0})
    del summary["modes"]["download"]
    assert vt.format_trial_rows(summary)[0].endswith("| — | 5 | 0 |")


def test_trial_rows_tolerate_missing_keys_and_modes():
    rows = vt.format_trial_rows({"repo": "prometheus", "modes": {"offline": {"files": 10}}})
    assert rows == ["| `prometheus` | オフライン | 10 | — | — | — | — | — | — | — | — | — | — | — | — | — |"]
    assert vt.format_trial_rows({"repo": "x"}) == []


def test_load_trial_summaries_reads_each_repo(tmp_path):
    assert vt.load_trial_summaries(tmp_path / "nothing") == []
    for repo in ("prometheus", "etcd"):
        (tmp_path / "trial" / repo).mkdir(parents=True)
        (tmp_path / "trial" / repo / "summary.json").write_text(
            json.dumps({"repo": repo}), encoding="utf-8")
    (tmp_path / "trial" / "empty").mkdir()
    assert [s["repo"] for s in vt.load_trial_summaries(tmp_path)] == ["etcd", "prometheus"]


def test_load_trial_summaries_names_a_broken_file(tmp_path):
    (tmp_path / "trial" / "etcd").mkdir(parents=True)
    (tmp_path / "trial" / "etcd" / "summary.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="trial/etcd/summary.json"):
        vt.load_trial_summaries(tmp_path)


def _fake_evaluate(fail: set[tuple[str, bool]] = frozenset()):
    calls = []

    def evaluate(fixture, root, *, env_mode, printer):
        calls.append((fixture, env_mode))
        return _report(fixture=fixture, mode="env" if env_mode else "no-env",
                       **{"pass": (fixture, env_mode) not in fail})
    return evaluate, calls


def test_main_prints_both_tables(tmp_path, capsys):
    (tmp_path / "trial" / "etcd").mkdir(parents=True)
    (tmp_path / "trial" / "etcd" / "summary.json").write_text(json.dumps(_summary()),
                                                              encoding="utf-8")
    evaluate, calls = _fake_evaluate()
    code = vt.main(["--output-dir", str(tmp_path)], evaluate=evaluate,
                   fixtures=("iryo-kanri-system", "go_layouts"))
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert calls == [("iryo-kanri-system", False), ("iryo-kanri-system", True),
                     ("go_layouts", False), ("go_layouts", True)]
    assert out[0] == vt.EVAL_HEADER and out[1].startswith("|---")
    assert sum(line.startswith("| `") for line in out) == 4 + 2
    assert vt.TRIAL_HEADER in out


def test_main_fails_when_a_gate_fails_and_notes_missing_trials(tmp_path, capsys):
    evaluate, _calls = _fake_evaluate(fail={("go_layouts", True)})
    code = vt.main(["--output-dir", str(tmp_path)], evaluate=evaluate, fixtures=("go_layouts",))
    captured = capsys.readouterr()
    assert code == 1
    assert "no trial outputs" in captured.err and "python -m graph_generator trial" in captured.err
    assert vt.TRIAL_HEADER not in captured.out


def test_main_can_skip_evaluate(tmp_path, capsys):
    (tmp_path / "trial" / "etcd").mkdir(parents=True)
    (tmp_path / "trial" / "etcd" / "summary.json").write_text(json.dumps(_summary()),
                                                              encoding="utf-8")
    evaluate, calls = _fake_evaluate()
    assert vt.main(["--output-dir", str(tmp_path), "--skip-evaluate"], evaluate=evaluate,
                   fixtures=("go_layouts",)) == 0
    out = capsys.readouterr().out
    assert calls == [] and vt.EVAL_HEADER not in out and vt.TRIAL_HEADER in out


def test_main_reports_a_broken_summary(tmp_path, capsys):
    (tmp_path / "trial" / "etcd").mkdir(parents=True)
    (tmp_path / "trial" / "etcd" / "summary.json").write_text("{", encoding="utf-8")
    evaluate, _calls = _fake_evaluate()
    assert vt.main(["--output-dir", str(tmp_path), "--skip-evaluate"], evaluate=evaluate,
                   fixtures=()) == 1
    assert "trial/etcd/summary.json" in capsys.readouterr().err
