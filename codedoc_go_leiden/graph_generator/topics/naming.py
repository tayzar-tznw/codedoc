"""Phase 4b: topic names (leiden spec §5.1).

One batched call per at most 60 communities, in Japanese, 2–5 words,
functional rather than layered (「機能・特徴別に分類」). Per community the
prompt carries the top 12 members by in-community PageRank (name, kind,
package, first line of the Phase 2 summary, or its function names when it
has none), the hub, the packages and
modules touched, the three strongest neighbouring communities, the size,
and for a subtopic the parent's name. Level-1 topics are named first, so a
subtopic's prompt shows its parent's new name.

The answer is a JSON object `{topic_id: name}`. A name must be non-empty,
unique after normalisation, at most `MAX_NAME_CHARS` characters on one line
and not a placeholder. Invalid or missing names get one retry, then the hub
label (`label_source = hub`). A community carried over at Jaccard ≥ 0.6
(`inherited_from` in communities.json) keeps its previous name
(`label_source = inherited`) and is not asked again.

No API call happens here: `name_topics` takes the model as a callable
`(prompt: str) -> str`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping

from .community import Communities, all_topics, as_json, neighbours
from .graph_builder import Projection, atom_kind, atom_label, first_line

BATCH_SIZE = 60
TOP_MEMBERS = 12
NEIGHBOURS = 3
MAX_NAME_CHARS = 40
PROMPT_VERSION = 1


class AnswerCut(Exception):
    """A naming answer cut at the output budget, also after the one retry with
    a larger budget (R49): its batch falls back to hub labels at once, with no
    re-ask, so a batch never costs more than the estimate's two calls."""


_PLACEHOLDER_WORDS = {
    "名前", "名称", "モジュール名", "トピック名", "未分類", "不明", "その他", "なし", "無題", "共通",
    "n/a", "na", "none", "null", "unknown", "untitled", "tbd", "todo", "misc", "other", "name",
    "...", "…", "-", "—", "?", "？",
}
_PLACEHOLDER_RE = re.compile(
    r"^(topic|community|module|group|cluster|トピック|コミュニティ|モジュール|グループ|クラスタ|機能)"
    r"[#]?(?:[0-9]+|[a-z])?$")  # matched on normalize_name output (NFKC, no spaces)
_ID_RE = re.compile(r"^[a-z0-9]+_[0-9a-f]{16}$")
_STRIP = "「」『』\"'`“”‘’()（）[]［］ 　"


def normalize_name(name: str) -> str:
    """Comparison key for uniqueness: NFKC, lower case, no spaces or quotes."""
    text = unicodedata.normalize("NFKC", name).strip().strip(_STRIP).lower()
    return re.sub(r"\s+", "", text)


def name_problem(name: object, taken: set[str] = frozenset()) -> str | None:
    """Why a proposed name is invalid, or None."""
    if not isinstance(name, str):
        return "not a string"
    text = name.strip()
    key = normalize_name(text)
    if not key:
        return "empty"
    if "\n" in text or len(text) > MAX_NAME_CHARS:
        return "too long"
    if key in _PLACEHOLDER_WORDS or _PLACEHOLDER_RE.match(key) or _ID_RE.match(key) \
            or "{" in text or "}" in text:
        return "placeholder"
    if key in taken:
        return "duplicate"
    return None


def naming_inputs(communities: Communities | Mapping, projection: Projection,
                  summaries: Mapping[str, str] | None = None, top: int = TOP_MEMBERS) -> list[dict]:
    """One entry per topic, level 1 first. `summaries` maps class_id to its
    Phase 2 summary (default: the Classes `summary` column)."""
    data = as_json(communities)
    summaries = summaries or {}
    links = {lv: neighbours(data, projection, int(lv)) for lv in data.get("levels", {})}
    entries = []
    for tid, t in sorted(all_topics(data).items(), key=lambda kv: (kv[1]["level"], kv[0])):
        members = []
        for cid in t["ranked"][:top]:
            a = projection.info.get(cid, {})
            summary = first_line(summaries.get(cid) or a.get("summary", ""), 120)
            if not summary and projection.functions.get(cid):
                funcs = projection.functions[cid]
                summary = "関数: " + ", ".join(funcs[:5]) + (" ほか" if len(funcs) > 5 else "")
            members.append({"name": atom_label(a), "kind": atom_kind(a), "package": a.get("package", ""),
                            "summary": summary})
        infos = [projection.info[c] for c in t["members"] if c in projection.info]
        entries.append({
            "topic_id": tid, "level": t["level"], "parent": t.get("parent"),
            "size": t["size"], "file_count": t["file_count"],
            "hub": t.get("label") or "", "members": members,
            "packages": sorted({a["package"] for a in infos if a.get("package")}),
            "modules": sorted({a["module"] for a in infos if a.get("module")}),
            "neighbours": [n for n, _w in links.get(str(t["level"]), {}).get(tid, [])[:NEIGHBOURS]],
            "inherited_from": t.get("inherited_from"),
        })
    return entries


def build_naming_prompt(entries: list[dict], known: Mapping[str, str] | None = None,
                        used: list[str] | None = None, problems: Mapping[str, str] | None = None) -> str:
    """The Japanese naming prompt for one batch. `known` maps topic IDs to
    the names to show for neighbours and parents (hub labels otherwise)."""
    known = known or {}
    lines = [
        "あなたはソフトウェアアーキテクトです。以下は Go コードベースの呼び出し・埋め込み・実装の関係から"
        "求めたトピック（型と関数のまとまり）です。各トピックに日本語の名前を付けてください。",
        "",
        "規則:",
        "- 機能・特徴別に名付けてください（アーキテクチャ層別ではなく）。",
        "- 2〜5語の短い名詞句にしてください。良い例: \"診療報酬計算\", \"患者受付と予約\"。"
        "悪い例: \"サービス層\", \"ユーティリティ\", \"トピック1\"。",
        "- すべての名前を互いに異なるものにしてください。",
        "- メンバーの名前と要約から読み取れる機能だけを根拠にしてください。",
        "",
        "### トピック一覧",
    ]
    for e in entries:
        lines += ["", f"#### {e['topic_id']}",
                  f"- 規模: 型・疑似型 {e['size']} 個、ファイル {e['file_count']} 個"]
        if e.get("parent"):
            lines.append(f"- 上位トピック: {known.get(e['parent'], e['parent'])}")
        lines.append(f"- ハブ: {e['hub']}")
        lines.append("- 主なメンバー（トピック内 PageRank 順）:")
        for m in e["members"]:
            summary = f": {m['summary']}" if m["summary"] else ""
            lines.append(f"  - {m['name']}（{m['kind']}, {m['package']}）{summary}")
        lines.append(f"- パッケージ: {', '.join(e['packages'][:8])}" + (" ほか" if len(e["packages"]) > 8 else ""))
        lines.append(f"- モジュール: {', '.join(e['modules'][:5])}" + (" ほか" if len(e["modules"]) > 5 else ""))
        if e["neighbours"]:
            lines.append("- 関係の強い隣接トピック: " + ", ".join(known.get(n, n) for n in e["neighbours"]))
    if used:
        lines += ["", "次の名前は使用済みです。重複しない名前にしてください: " + ", ".join(sorted(used))]
    if problems:
        lines += ["", "前回の回答の問題:"] + [f"- {tid}: {why}" for tid, why in sorted(problems.items())]
    ids = ", ".join(f'"{e["topic_id"]}": "名前"' for e in entries[:2])
    lines += ["", "次の JSON オブジェクトだけを返してください（キーは上の見出しの ID、値は名前）:",
              "{" + ids + (", ..." if len(entries) > 2 else "") + "}"]
    return "\n".join(lines)


def _json_object(text: str) -> dict:
    """The outermost `{…}` of an answer (code fences and prose around it are ignored)."""
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object")
    return json.loads(body[start:end + 1])


def parse_names(text: str, expected: list[str], taken: set[str] = frozenset()) -> tuple[dict[str, str], dict[str, str]]:
    """(valid names, problems) for one answer. `taken` holds normalized names
    already in use; within the answer, the first of two equal names wins."""
    try:
        obj = _json_object(text)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        return {}, {tid: f"unparsable answer ({exc})" for tid in expected}
    valid: dict[str, str] = {}
    problems: dict[str, str] = {}
    used = set(taken)
    for tid in expected:
        if tid not in obj:
            problems[tid] = "missing"
            continue
        why = name_problem(obj[tid], used)
        if why:
            problems[tid] = why
            continue
        valid[tid] = obj[tid].strip()
        used.add(normalize_name(valid[tid]))
    return valid, problems


def _unique_label(label: str, taken: set[str]) -> str:
    name, n = label or "?", 2
    while normalize_name(name) in taken:
        name, n = f"{label} ({n})", n + 1
    return name


def name_topics(entries: list[dict], call: Callable[[str], str],
                previous_names: Mapping[str, object] | None = None,
                batch_size: int = BATCH_SIZE) -> dict:
    """Names for every entry → the `names.json` document:
    `{"version", "prompt_version", "names": {topic_id: {"name", "label_source"[, "inherited_from"]}},
    "stats": {...}}`. `previous_names` is the previous names.json (its
    `names` map, or a plain {topic_id: name})."""
    prev = dict((previous_names or {}).get("names", previous_names or {}))
    names: dict[str, dict] = {}
    taken: set[str] = set()
    stats = {"calls": 0, "retries": 0, "llm": 0, "fallbacks": 0, "inherited": 0, "errors": []}
    for e in entries:
        old = prev.get(e.get("inherited_from") or "")
        old_name = old.get("name") if isinstance(old, dict) else old
        source = old.get("label_source", "llm") if isinstance(old, dict) else "llm"
        if old_name and source in ("llm", "inherited") and not name_problem(old_name, taken):
            names[e["topic_id"]] = {"name": old_name, "label_source": "inherited",
                                    "inherited_from": e["inherited_from"]}
            taken.add(normalize_name(old_name))
            stats["inherited"] += 1
    labels = {e["topic_id"]: e["hub"] for e in entries}

    def ask(batch: list[dict], problems: Mapping[str, str] | None) -> tuple[dict[str, str], dict[str, str]]:
        known = {**labels, **{t: n["name"] for t, n in names.items()}}
        prompt = build_naming_prompt(batch, known, [n["name"] for n in names.values()] if problems else None,
                                     problems)
        stats["calls"] += 1
        try:
            text = call(prompt)
        except AnswerCut as exc:  # no problems, so no re-ask: hub labels now
            stats["errors"].append(f"{type(exc).__name__}: {exc}")
            return {}, {}
        except Exception as exc:  # recorded; the topic falls back to its hub label
            stats["errors"].append(f"{type(exc).__name__}: {exc}")
            return {}, {e["topic_id"]: "call failed" for e in batch}
        return parse_names(text, [e["topic_id"] for e in batch], taken)

    for level in sorted({e["level"] for e in entries}):
        todo = [e for e in entries if e["level"] == level and e["topic_id"] not in names]
        for i in range(0, len(todo), batch_size):
            batch = todo[i:i + batch_size]
            valid, problems = ask(batch, None)
            for tid, name in valid.items():
                names[tid] = {"name": name, "label_source": "llm"}
                taken.add(normalize_name(name))
            if problems:
                stats["retries"] += 1
                retry = [e for e in batch if e["topic_id"] in problems]
                valid, problems = ask(retry, problems)
                for tid, name in valid.items():
                    names[tid] = {"name": name, "label_source": "llm"}
                    taken.add(normalize_name(name))
            for e in batch:
                if e["topic_id"] not in names:
                    names[e["topic_id"]] = {"name": _unique_label(e["hub"], taken), "label_source": "hub"}
                    taken.add(normalize_name(names[e["topic_id"]]["name"]))
                    stats["fallbacks"] += 1
    stats["llm"] = sum(1 for n in names.values() if n["label_source"] == "llm")
    return {"version": 1, "prompt_version": PROMPT_VERSION, "names": dict(sorted(names.items())),
            "stats": stats}
