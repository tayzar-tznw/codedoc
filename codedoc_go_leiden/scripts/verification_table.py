"""The verification tables of docs/LSP_RESOLUTION.md, from real runs.

Run from codedoc_go/:  .venv/bin/python -m scripts.verification_table [--output-dir DIR] [--skip-evaluate]

It runs `evaluate_fixture` for every fixture in both modes (offline and
`--env`; neither needs the network or GCP) and prints the evaluate table,
then prints the trial table from `OUTPUT_DIR/trial/<repo>/summary.json`
(written by `python -m graph_generator trial`; missing files are noted on
stderr and skipped). It exits 1 if any evaluate run failed its gates or a
summary cannot be read. scripts.doccheck finds both tables by their headers.

Trial summary keys read (a missing key prints "—"):
  {"repo", "modes": {"offline" | "download": {"files", "packages", "sites",
    "by_status": {status: n}, "edges": {table: n}, "imports": {"records", "mapped"},
    "seconds", "peak_rss_bytes"}},
   "changed_answers", "oracle": {"mode", "verdicts": {VERDICT: n}}}
The インポート columns: evaluate shows mapped/records of resolved in-repo import
records (gated on iryo-kanri-system and multi_repo, 参考 elsewhere); the trial
shows the mapped share per arm. The オラクル column (evaluate) shows the edges
goracle judged / the WRONG-* verdicts among them (— on go_layouts, where the
oracle only checks the records against goracle's site table).
The oracle scores one arm's records: the download arm's (spec R7), or the
offline arm's when the trial ran offline only ("mode" says which).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EVAL_HEADER = ("| フィクスチャ | モード | ケース | 正解 | 代替 | 誤り | 未回答 | 完全性 | "
               "ノードカバレッジ | エッジ率 | QA | リポジトリ間 | インポート | オラクル | 結果 |")
EVAL_MODES = ("既定", "`--env`")
TRIAL_HEADER = ("| リポジトリ | モード | ファイル | パッケージ | サイト | resolved | external | "
                "unresolved | MethodCalls | PossiblyCalls | インポート | 秒 | 最大メモリ | "
                "変化した答え | 確認した辺 | 誤った辺 |")
TRIAL_MODES = {"offline": "オフライン", "download": "ダウンロード"}
# Oracle verdicts (spec §9.4): these count as correct; WRONG-* and *-OTHER as wrong.
CORRECT_VERDICTS = ("CONFIRMED", "CONFIRMED-NOEXEC", "IFACE-CONSISTENT", "DEFER-GROUP-CONSISTENT")
WRONG_VERDICTS = ("WRONG-OTHER", "WRONG-MULTI", "IFACE-OTHER", "DEFER-GROUP-OTHER")
NA = "—"


def _separator(header: str) -> str:
    return "|" + "---|" * (header.count("|") - 1)


def _count(x) -> int:
    return x if isinstance(x, int) else len(x)


def _metric(m: dict | None) -> str:
    if m is None:
        return NA
    v = m["value"]
    text = f"{v:.3f}" if isinstance(v, float) else str(v)
    return text if m["pass"] else f"{text}（未達）"


def _cross(c: dict | None) -> str:
    if c is None:
        return NA
    if c["pass"]:
        return "一致"
    return f"不一致（不足 {_count(c['missing'])}、余分 {_count(c['extra'])}）"


def _imports(i: dict | None) -> str:
    """Mapped of resolved in-repo import records (evaluate.imports_check); 参考 when
    the fixture is not gated on it."""
    if not i:
        return NA
    notes = [] if i.get("gated") else ["参考"]
    if i.get("ambiguous"):
        notes.append(f"曖昧 {i['ambiguous']}")
    if i.get("gated") and not i.get("pass"):
        notes.append("未達")
    return f"{i['mapped']}/{i['records']}" + (f"（{'、'.join(notes)}）" if notes else "")


def _oracle(o: dict | None) -> str:
    """The runtime oracle (evaluate.oracle_check): judged edges / WRONG-*; — where
    it only checks the records against goracle's site table."""
    if not o:
        return NA
    text = f"{o.get('edges', 0)} / {o.get('wrong', 0)}" if (o.get("edges") or o.get("verdicts")) else NA
    return text if o.get("pass", True) else f"{text}（未達）"


def format_row(report: dict) -> str:
    """One evaluate row: fixture, mode, cases, the four verdicts, the gates, result."""
    mode = EVAL_MODES[1] if report["mode"] == "env" else EVAL_MODES[0]
    if "error" in report:
        return f"| `{report['fixture']}` | {mode} | エラー: {report['error']} |"
    v = report["verdicts"]
    comp = report["completeness"]
    cells = [f"`{report['fixture']}`", mode, str(report["cases"]), str(v["correct"]),
             str(v["alternate"]), str(v["wrong"]), str(v["unanswered"]),
             f"{comp['value']}（必要 {comp['required']}）",
             _metric(report["node_coverage"]), _metric(report["edge_rate"]),
             _metric(report["qa"]), _cross(report["cross_repo"]), _imports(report.get("imports")),
             _oracle(report.get("oracle")), "PASS" if report["pass"] else "FAIL"]
    return "| " + " | ".join(cells) + " |"


def _num(value) -> str:
    return NA if value is None else str(value)


def _share(i: dict | None) -> str:
    """The share of an arm's resolved in-repo import records with a PackageImports row."""
    if not i or not i.get("records"):
        return NA
    return f"{100 * i['mapped'] / i['records']:.1f}%"


def format_trial_rows(summary: dict) -> list[str]:
    """One trial row per mode present in a summary, offline first; the oracle
    columns sit on the row of the arm the oracle scored."""
    modes = summary.get("modes") or {}
    oracle = summary.get("oracle") or {}
    scored = oracle.get("mode") or ("download" if "download" in modes else "offline")
    rows = []
    for key, label in TRIAL_MODES.items():
        m = modes.get(key)
        if m is None:
            continue
        status, edges = m.get("by_status") or {}, m.get("edges") or {}
        verdicts = oracle.get("verdicts") if key == scored else None
        seconds, rss = m.get("seconds"), m.get("peak_rss_bytes")
        cells = [f"`{summary.get('repo', '?')}`", label, _num(m.get("files")),
                 _num(m.get("packages")), _num(m.get("sites")),
                 _num(status.get("resolved")), _num(status.get("external")),
                 _num(status.get("unresolved")), _num(edges.get("MethodCalls")),
                 _num(edges.get("PossiblyCalls")), _share(m.get("imports")),
                 NA if seconds is None else f"{seconds:.1f}",
                 NA if rss is None else f"{rss / 1e9:.1f} GB",
                 _num(summary.get("changed_answers")) if key == "download" else NA,
                 NA if verdicts is None else str(sum(verdicts.get(v, 0) for v in CORRECT_VERDICTS)),
                 NA if verdicts is None else str(sum(verdicts.get(v, 0) for v in WRONG_VERDICTS))]
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def load_trial_summaries(output_dir: Path) -> list[dict]:
    """Every OUTPUT_DIR/trial/<repo>/summary.json, by repo name; none is fine."""
    out = []
    for path in sorted(Path(output_dir).glob("trial/*/summary.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read {path}: {e}") from None
    return out


def fixture_names() -> tuple[str, ...]:
    from graph_generator.evaluate import FIXTURES

    return tuple(FIXTURES)


def main(argv: list[str] | None = None, *, evaluate=None, fixtures=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.verification_table",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("--output-dir", help="the OUTPUT_DIR holding trial/<repo>/summary.json "
                                             "(default: OUTPUT_DIR from the root .env)")
    parser.add_argument("--skip-evaluate", action="store_true", help="print only the trial table")
    args = parser.parse_args(argv)
    ok = True
    if not args.skip_evaluate:
        if evaluate is None:
            from graph_generator.evaluate import evaluate_fixture as evaluate
        print(EVAL_HEADER)
        print(_separator(EVAL_HEADER))
        for fixture in fixture_names() if fixtures is None else fixtures:
            for env_mode in (False, True):
                report = evaluate(fixture, str(ROOT), env_mode=env_mode,
                                  printer=lambda *a, **k: None)
                print(format_row(report))
                ok = ok and bool(report.get("pass"))
    if args.output_dir is None:
        from graph_generator import config
        args.output_dir = config.OUTPUT_DIR
    try:
        summaries = load_trial_summaries(Path(args.output_dir))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if not summaries:
        print(f"no trial outputs under {args.output_dir}/trial/*/summary.json; run "
              "`python -m graph_generator trial <dir> --repo-name N` (offline and "
              "--download-modules) first", file=sys.stderr)
    else:
        print()
        print(TRIAL_HEADER)
        print(_separator(TRIAL_HEADER))
        for summary in summaries:
            for row in format_trial_rows(summary):
                print(row)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
