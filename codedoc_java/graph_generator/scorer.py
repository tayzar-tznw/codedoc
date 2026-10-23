"""受け入れ採点器 — resolution.json を正解マニフェストと突き合わせる決定的スコアラ。

LLM・Spanner・言語サーバーには一切依存しない。マニフェストは Java 版
(version 3、`artifact` = Maven artifactId / JDK モジュール名)を対象とするが、
判定はスキーマのフィールドのみに依存し version 値は読まない。判定基準:
  1. 完全性 (completeness)      : 正解数 > 全ケースの 85%
                                  (= floor(0.85 × case_count) + 1 件以上)
  2. 誤答ゼロ (zero-wrong)      : 誤答 0 件
  3. ノード被覆率 (node-coverage): >= 0.85
  4. エッジ解決率 (edge-resolution): >= 0.85
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

NODE_COVERAGE_MIN = 0.85
EDGE_RATE_MIN = 0.85

# 在リポジトリ(ソース)ターゲットの via — ノード被覆の分母に入る
_IN_REPO_VIA = ("source", "maven")

_EDGE_KINDS = ("invocation", "object_creation")
_ANSWERED = ("resolved", "external")

_VERDICT_COLUMN = {
    "correct": "correct",
    "documented_alternate": "alternate",
    "wrong": "wrong",
    "unanswered": "unanswered",
}


# ── helpers ───────────────────────────────────────────────────


def _alt_symbols(case: dict) -> set:
    """Documented alternate symbols: static_target / runtime_target, found either
    on the case root or under `expected`, as flat strings or {"symbol": ...}."""
    out = set()
    for holder in (case, case.get("expected") or {}):
        for key in ("static_target", "runtime_target"):
            value = holder.get(key)
            if isinstance(value, str):
                out.add(value)
            elif isinstance(value, dict) and value.get("symbol"):
                out.add(value["symbol"])
    return out


def _first_target(case: dict) -> dict:
    targets = (case.get("expected") or {}).get("targets") or []
    return targets[0] if targets else {}


def _is_correct(case: dict, site: dict) -> bool:
    expected = case.get("expected") or {}
    resolution = expected.get("resolution")
    t0 = _first_target(case)
    status = site.get("status")
    target = site.get("target") or {}
    if resolution == "resolved":
        if status != "resolved" or target.get("symbol") != t0.get("symbol"):
            return False
        want_artifact = t0.get("artifact")
        return want_artifact is None or target.get("artifact") == want_artifact
    if resolution == "external":
        return status == "external" and target.get("artifact") == t0.get("artifact")
    if resolution == "ambiguous":
        return status == "ambiguous" and site.get("candidate_count") == t0.get("candidate_count")
    if resolution == "runtime":
        return status == "runtime"
    return False


def _verdict(case: dict, site: dict | None) -> str:
    if site is None:
        return "unanswered"
    if _is_correct(case, site):
        return "correct"
    status = site.get("status")
    target = site.get("target") or {}
    if status == "resolved" and target.get("symbol") in _alt_symbols(case):
        return "documented_alternate"
    if status in _ANSWERED:
        return "wrong"
    return "unanswered"


def _expected_col(case: dict, target_dir: str) -> int | None:
    """UTF-16 column of the case's token, derived from its unique snippet in
    the raw source line — disambiguates two same-token sites on one line."""
    snippet = case.get("snippet") or ""
    token = case.get("token") or ""
    if not snippet or not token or token not in snippet:
        return None
    try:
        path = Path(target_dir) / case.get("file", "")
        raw = path.read_text(encoding="utf-8").splitlines()[case.get("line", 1) - 1]
    except (OSError, IndexError):
        return None
    at = raw.find(snippet)
    if at < 0:
        return None
    cp_index = at + snippet.index(token)
    return len(raw[:cp_index].encode("utf-16-le")) // 2


def _match_site(case: dict, index: dict, target_dir: str) -> dict | None:
    matches = index.get((case.get("file"), case.get("line"), case.get("token")), [])
    if not matches:
        return None
    if len(matches) > 1:
        col = _expected_col(case, target_dir)
        if col is not None:
            exact = [s for s in matches if s.get("col") == col]
            if exact:
                matches = exact
    for site in matches:
        if site.get("site_kind") == case.get("site_kind"):
            return site
    return matches[0]


def _manifest_inventory(cases: list) -> set:
    """Node inventory when no pins are supplied: in-corpus target symbols
    (via source/maven), documented alternates, and all distractor symbols.
    `via="jdk"` targets live outside the repo and are NOT expected as nodes."""
    inventory = set()
    for case in cases:
        expected = case.get("expected") or {}
        for target in expected.get("targets") or []:
            symbol = target.get("symbol")
            if symbol and target.get("via") in _IN_REPO_VIA:
                inventory.add(symbol)
        inventory |= _alt_symbols(case)
        for distractor in case.get("distractors") or []:
            if isinstance(distractor, str):
                inventory.add(distractor)
            elif isinstance(distractor, dict) and distractor.get("symbol"):
                inventory.add(distractor["symbol"])
    return inventory


def _expected_compact(case: dict) -> dict:
    expected = case.get("expected") or {}
    resolution = expected.get("resolution")
    t0 = _first_target(case)
    out = {"resolution": resolution}
    if resolution in ("resolved", "external"):
        out["symbol"] = t0.get("symbol")
        out["artifact"] = t0.get("artifact")
    elif resolution == "ambiguous":
        out["candidate_count"] = t0.get("candidate_count")
    return out


def _got_compact(site: dict | None) -> dict | None:
    if site is None:
        return None
    target = site.get("target") or {}
    return {
        "status": site.get("status"),
        "symbol": target.get("symbol"),
        "artifact": target.get("artifact"),
        "candidate_count": site.get("candidate_count"),
        "file": site.get("file"),
        "line": site.get("line"),
    }


# ── public API ────────────────────────────────────────────────


def score(target_dir: str, resolution: dict, manifest: dict, pins: dict | None = None) -> dict:
    cases = manifest.get("cases") or []
    sites = resolution.get("sites") or []

    index: dict = {}
    for site in sites:
        key = (site.get("file"), site.get("line"), site.get("token"))
        index.setdefault(key, []).append(site)

    counts = {"correct": 0, "alternate": 0, "wrong": 0, "unanswered": 0}
    per_category: dict = {}
    results = []
    wrong_details = []
    for case in cases:
        site = _match_site(case, index, target_dir)
        verdict = _verdict(case, site)
        counts[_VERDICT_COLUMN[verdict]] += 1
        category = str(case.get("id", "")).split("-")[0]
        row = per_category.setdefault(
            category, {"total": 0, "correct": 0, "alternate": 0, "unanswered": 0, "wrong": 0}
        )
        row["total"] += 1
        row[_VERDICT_COLUMN[verdict]] += 1
        expected_c = _expected_compact(case)
        results.append(
            {"id": case.get("id"), "verdict": verdict, "expected": expected_c, "got": _got_compact(site)}
        )
        if verdict == "wrong":
            target = (site or {}).get("target") or {}
            wrong_details.append(
                {
                    "id": case.get("id"),
                    "expected": expected_c.get("symbol") or expected_c.get("resolution"),
                    "got": target.get("symbol") or target.get("artifact") or site.get("status"),
                    "site": f"{site.get('file')}:{site.get('line')}",
                }
            )

    case_count = len(cases)
    correct = counts["correct"]
    completeness_pct = 100.0 * correct / case_count if case_count else 0.0
    # 「85% 超」= floor(0.85 × N) + 1。N が 85% で割り切れても境界が緩まない
    # (N=106 なら 91、N=20 なら 18)。空マニフェストでも最低 1 件を要求する。
    completeness_min = max(1, math.floor(0.85 * case_count) + 1)

    inventory = set(pins.keys()) if pins else _manifest_inventory(cases)
    declared = {d.get("symbol") for d in resolution.get("declarations") or []}
    node_covered = len(inventory & declared)
    node_coverage = node_covered / len(inventory) if inventory else 1.0

    edge_sites = [
        s for s in sites if s.get("site_kind") in _EDGE_KINDS and s.get("status") != "builtin"
    ]
    edge_resolved = sum(1 for s in edge_sites if s.get("status") in _ANSWERED)
    edge_rate = edge_resolved / len(edge_sites) if edge_sites else 1.0

    gates = {
        "completeness": correct >= completeness_min,
        "zero_wrong": counts["wrong"] == 0,
        "node_coverage": node_coverage >= NODE_COVERAGE_MIN,
        "edge_resolution": edge_rate >= EDGE_RATE_MIN,
    }
    return {
        "target_dir": target_dir,
        "case_count": case_count,
        "correct": correct,
        "alternate": counts["alternate"],
        "wrong": counts["wrong"],
        "unanswered": counts["unanswered"],
        "completeness_pct": completeness_pct,
        "completeness_min": completeness_min,
        "node_inventory": len(inventory),
        "node_covered": node_covered,
        "node_coverage": node_coverage,
        "edge_total": len(edge_sites),
        "edge_resolved": edge_resolved,
        "edge_rate": edge_rate,
        "gates": gates,
        "passed": all(gates.values()),
        "per_category": per_category,
        "wrong_details": wrong_details,
        "results": results,
    }


def format_report(result: dict) -> str:
    lines = []
    lines.append("=== 採点結果 (受け入れゲート) ===")
    lines.append(f"対象: {result['target_dir']}")
    lines.append("")
    lines.append("カテゴリ別集計:")
    header = f"  {'カテゴリ':<8} {'総数':>4} {'正解':>4} {'代替':>4} {'未回答':>4} {'誤答':>4}"
    lines.append(header)
    for category in sorted(result["per_category"]):
        row = result["per_category"][category]
        lines.append(
            f"  {category:<10} {row['total']:>4} {row['correct']:>4} "
            f"{row['alternate']:>4} {row['unanswered']:>6} {row['wrong']:>4}"
        )
    lines.append(
        f"  {'合計':<9} {result['case_count']:>4} {result['correct']:>4} "
        f"{result['alternate']:>4} {result['unanswered']:>6} {result['wrong']:>4}"
    )
    lines.append("")
    lines.append("ゲート判定:")
    gates = result["gates"]

    def mark(ok: bool) -> str:
        return "[PASS]" if ok else "[FAIL]"

    lines.append(
        f"  {mark(gates['completeness'])} 完全性 (completeness): "
        f"正解 {result['correct']}/{result['case_count']} ({result['completeness_pct']:.1f}%) "
        f"— 基準 {result['completeness_min']} 件以上 "
        f"(全 {result['case_count']} 件の 85% 超)"
    )
    lines.append(
        f"  {mark(gates['zero_wrong'])} 誤答ゼロ (zero-wrong): 誤答 {result['wrong']} 件"
    )
    for detail in result["wrong_details"]:
        lines.append(
            f"    - {detail['id']}: 期待={detail['expected']} "
            f"出力={detail['got']} @ {detail['site']}"
        )
    lines.append(
        f"  {mark(gates['node_coverage'])} ノード被覆率 (node-coverage): "
        f"{result['node_coverage']:.3f} ({result['node_covered']}/{result['node_inventory']}) "
        f"— 基準 {NODE_COVERAGE_MIN} 以上"
    )
    lines.append(
        f"  {mark(gates['edge_resolution'])} エッジ解決率 (edge-resolution): "
        f"{result['edge_rate']:.3f} ({result['edge_resolved']}/{result['edge_total']}) "
        f"— 基準 {EDGE_RATE_MIN} 以上"
    )
    lines.append("")
    passed_count = sum(1 for ok in gates.values() if ok)
    overall = "合格" if result["passed"] else "不合格"
    lines.append(f"総合判定: {overall} ({passed_count}/{len(gates)} ゲート通過)")
    return "\n".join(lines)


def _load_pins(cases_py: Path) -> dict | None:
    if not cases_py.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_scorer_ground_truth_cases", cases_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    merged = dict(getattr(module, "DECL_PINS", {}) or {})
    merged.update(getattr(module, "STATIC_TARGET_PINS", {}) or {})
    return merged or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scorer",
        description="resolution.json を正解マニフェストと照合し、受け入れゲートを判定します。",
    )
    parser.add_argument("corpus_dir", help="コーパスのルートディレクトリ")
    parser.add_argument(
        "--manifest", default=None,
        help="正解マニフェスト (既定: <corpus_dir>/ground_truth/expected_resolutions.json)",
    )
    parser.add_argument(
        "--resolution", default=None,
        help="解決結果 resolution.json (既定: <OUTPUT_DIR>/resolution.json)",
    )
    args = parser.parse_args(argv)

    corpus_dir = Path(args.corpus_dir)
    manifest_path = (
        Path(args.manifest)
        if args.manifest
        else corpus_dir / "ground_truth" / "expected_resolutions.json"
    )
    if args.resolution:
        resolution_path = Path(args.resolution)
    else:
        from graph_generator import config  # lazy: only needed for the default

        resolution_path = Path(config.OUTPUT_DIR) / "resolution.json"

    for path, label in ((manifest_path, "マニフェスト"), (resolution_path, "解決結果")):
        if not path.is_file():
            print(f"エラー: {label}ファイルが見つかりません: {path}", file=sys.stderr)
            return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    pins = _load_pins(corpus_dir / "ground_truth" / "cases.py")

    result = score(str(corpus_dir), resolution, manifest, pins)
    print(format_report(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
