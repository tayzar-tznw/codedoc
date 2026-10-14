"""Phase 5: topic pages from structural evidence (leiden spec §5.2).

* `build_evidence` assembles one topic's bounded evidence package (default
  12,000 estimated input tokens, Codex §8.6): a header, hubs with call
  counts, entry points (members called only from other topics), the
  inter-topic dependencies, the ten strongest intra-topic call paths, for a
  parent its children's summaries, and the members table. Sections are
  filled in that order; rows inside a section are ordered by combined
  endpoint degree (or call count first, where named), and a row that does
  not fit the budget is omitted and recorded.
* `component_scaffold` / `flow_scaffold` are Mermaid diagrams built from real
  edges only; `diagram_problems` lists edges a page adds and
  `enforce_scaffolds` puts the scaffolds back (the LLM annotates, never adds
  edges, Codex §9.2).
* `build_report_prompt` is the siblings' Japanese page template (はじめに /
  アーキテクチャと設計 / コンポーネント関係図 / データフロー / 主要コンポーネント
  / 依存関係) plus 「構造的根拠」.
* `grounding_check(markdown, name_set)` checks that every code identifier in
  backticks names something in the graph (types, functions, methods,
  packages, files); the same check runs on codedoc_go's pages in
  compare-topics.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

from .community import Communities, all_topics, as_json, leaf_map, level_map
from .graph_builder import CALL_RELATIONS, PAIR_RELATIONS, Projection, atom_kind, first_line, relation_weight

BUDGET_TOKENS = 12000
HUBS = 5
ENTRY_POINTS = 15
DEPENDENCIES = 10  # per direction
CALL_PATHS = 10
EXAMPLES = 3
FUNCTIONS = 6  # per member row
DIAGRAM_NODES = 12
FLOW_MESSAGES = 8
PROMPT_VERSION = 1

GO_PREDECLARED = frozenset("""
any bool byte comparable complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune
string uint uint8 uint16 uint32 uint64 uintptr true false iota nil append cap clear close complex copy
delete imag len make max min new panic print println real recover
break case chan const continue default defer else fallthrough for func go goto if import interface
map package range return select struct switch type var
""".split())


def estimate_tokens(text: str) -> int:
    """A conservative estimate: 4 ASCII characters or 1 other character per
    token (Japanese summaries cost about a token per character)."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_chars / 4) + (len(text) - ascii_chars)


def _display(info: Mapping) -> str:
    """The identifier shown for an atom: a type's name, or a `<package>`
    pseudo-type's file path (both are in the grounding name set)."""
    if info.get("kind") == "package":
        return info.get("path") or "?"
    return info.get("name") or "?"


# ═══════════════════════════════════════════════════════════════════
# Evidence package
# ═══════════════════════════════════════════════════════════════════


def build_evidence(topic_id: str, communities: Communities | Mapping, projection: Projection,
                   names: Mapping[str, str] | None = None, summaries: Mapping[str, str] | None = None,
                   child_summaries: Mapping[str, str] | None = None,
                   method_names: Mapping[str, str] | None = None,
                   budget_tokens: int = BUDGET_TOKENS) -> dict:
    """The evidence package of one topic. `names` maps topic IDs to names
    (plain, names.json entries, or the whole names.json; hub labels
    otherwise), `summaries` class IDs to Phase 2 summaries
    (default the Classes `summary` column), `child_summaries` child topic IDs
    to their page summaries, `method_names` method IDs to qualnames (default:
    the projection's Methods rows)."""
    data = as_json(communities)
    topics = all_topics(data)
    if topic_id not in topics:
        raise ValueError(f"{topic_id} is not a topic with a page (「未分類」 is never sent to the LLM)")
    t = topics[topic_id]
    given = dict((names or {}).get("names", names or {}))
    given = {tid: e.get("name") if isinstance(e, Mapping) else e for tid, e in given.items()}
    names = {tid: given.get(tid) or tt.get("label") or tid for tid, tt in topics.items()}
    summaries = summaries or {}
    method_names = method_names or projection.method_names
    info = projection.info
    assign = level_map(data, 1) if t["level"] == 1 else leaf_map(data)
    inside = set(t["members"])
    eligible = projection.eligible
    degree = projection.degree

    calls_in: Counter = Counter()
    calls_out: Counter = Counter()
    callers_from: dict[str, Counter] = defaultdict(Counter)
    deps: dict[tuple[str, str], dict] = {}
    paths: list[dict] = []
    for (u, v), rels in projection.directed.items():
        if u not in eligible or v not in eligible:
            continue
        pairs = [p for rel in CALL_RELATIONS for p in rels.get(rel, [])]
        if pairs and (u in inside or v in inside):
            calls_out[u] += len(pairs)
            calls_in[v] += len(pairs)
            if v in inside:
                callers_from[v]["inside" if u in inside else (assign.get(u) or "unassigned")] += len(pairs)
        if u in inside and v in inside:
            if pairs:
                paths.append({"source": u, "target": v, "count": len(pairs),
                              "examples": [[method_names.get(a, a), method_names.get(b, b)]
                                           for a, b in pairs[:EXAMPLES]],
                              "_order": (-len(pairs), -(degree.get(u, 0) + degree.get(v, 0)), u, v)})
            continue
        if (u in inside) == (v in inside):
            continue
        other = assign.get(v if u in inside else u)
        if not other:
            continue
        key = ("out", other) if u in inside else ("in", other)
        entry = deps.setdefault(key, {"direction": key[0], "topic_id": other, "topic": names.get(other, other),
                                      "call_count": 0, "weight": 0.0, "examples": []})
        for rel, facts in rels.items():
            entry["weight"] += relation_weight(rel, 1 if rel in PAIR_RELATIONS else len(facts))
        entry["call_count"] += len(pairs)
        for a, b in pairs:
            if len(entry["examples"]) < 2:
                entry["examples"].append([method_names.get(a, a), method_names.get(b, b)])

    ranked_eligible = [a for a in t["ranked"] if a in eligible]
    connected = [a for a in ranked_eligible if degree.get(a, 0.0) > 0]
    hubs = [{"class_id": a, "name": _display(info[a]), "kind": atom_kind(info[a]),
             "calls_in": calls_in[a], "calls_out": calls_out[a]} for a in connected[:HUBS]]
    entries = []
    for a in ranked_eligible:
        callers = callers_from.get(a, Counter())
        if callers and "inside" not in callers:
            entries.append({"class_id": a, "name": _display(info[a]), "calls_in": sum(callers.values()),
                            "callers": [names.get(c, c) if c != "unassigned" else "未分類"
                                        for c, _n in sorted(callers.items(), key=lambda x: (-x[1], x[0]))],
                            "_order": (-sum(callers.values()), -degree.get(a, 0), a)})
    entries.sort(key=lambda r: r["_order"])
    dependencies = []
    over_cap = {"entry_points": max(0, len(entries) - ENTRY_POINTS), "call_paths": max(0, len(paths) - CALL_PATHS),
                "dependencies": 0}
    for direction in ("out", "in"):
        rows = [d for (dr, _o), d in deps.items() if dr == direction]
        rows.sort(key=lambda d: (-d["weight"], -d["call_count"], d["topic_id"]))
        over_cap["dependencies"] += max(0, len(rows) - DEPENDENCIES)
        for d in rows[:DEPENDENCIES]:
            d["weight"] = round(d["weight"], 4)
            dependencies.append(d)
    paths.sort(key=lambda r: r["_order"])
    for r in paths:
        r["source_name"], r["target_name"] = _display(info[r["source"]]), _display(info[r["target"]])
    members = sorted(t["members"], key=lambda a: (-degree.get(a, 0.0), a))
    member_rows = [{"class_id": a, "name": _display(info[a]), "kind": atom_kind(info[a]),
                    "file": info[a].get("path", ""), "role": t["roles"][a],
                    "functions": projection.functions.get(a, [])[:FUNCTIONS],
                    "summary": first_line(summaries.get(a) or info[a].get("summary", ""))} for a in members]
    children = [{"topic_id": c, "name": names.get(c, c), "summary": first_line((child_summaries or {}).get(c, ""))}
                for c in t.get("children") or []]
    roles = Counter(t["roles"].values())
    header = {"topic_id": topic_id, "name": names[topic_id], "level": t["level"],
              "parent": names.get(t["parent"]) if t.get("parent") else None,
              "size": t["size"], "file_count": t["file_count"], "hub": _display(info[t["hub"]]) if t.get("hub") else "",
              "roles": dict(sorted(roles.items())), "cohesion": t.get("cohesion"),
              "conductance": t.get("conductance")}
    evidence = {"topic_id": topic_id, "name": names[topic_id], "header": header,
                "sections": {"hubs": [], "entry_points": [], "dependencies": [], "call_paths": [],
                             "children": [], "members": []},
                "omitted": {}, "budget": budget_tokens}
    used = estimate_tokens(_render_header(header))
    for section, rows in (("hubs", hubs), ("entry_points", entries[:ENTRY_POINTS]),
                          ("dependencies", dependencies), ("call_paths", paths[:CALL_PATHS]),
                          ("children", children), ("members", member_rows)):
        for row in rows:
            row.pop("_order", None)
            cost = estimate_tokens(_ROW_RENDER[section](row))
            if used + cost <= budget_tokens:
                evidence["sections"][section].append(row)
                used += cost
            else:
                evidence["omitted"].setdefault(section, []).append(
                    row.get("class_id") or row.get("topic_id") or f"{row.get('source')}->{row.get('target')}")
    counts = Counter({s: len(ids) for s, ids in evidence["omitted"].items()})
    counts.update({s: n for s, n in over_cap.items() if n})
    evidence["omitted_counts"] = dict(sorted(counts.items()))
    evidence["tokens"] = used
    evidence["scaffolds"] = _scaffolds(t, projection, method_names, evidence)
    return evidence


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _code(text: str) -> str:
    return f"`{text}`" if text else ""


_ROW_RENDER = {
    "hubs": lambda r: f"| {_code(r['name'])} | {r['kind']} | {r['calls_in']} | {r['calls_out']} |",
    "entry_points": lambda r: f"| {_code(r['name'])} | {_cell(', '.join(r['callers']))} | {r['calls_in']} |",
    "dependencies": lambda r: (f"| {'依存先' if r['direction'] == 'out' else '依存元'} | {_cell(r['topic'])} | "
                               f"{r['call_count']} | {r['weight']} | "
                               + ", ".join(f"{_code(a)} → {_code(b)}" for a, b in r["examples"]) + " |"),
    "call_paths": lambda r: (f"| {_code(r['source_name'])} | {_code(r['target_name'])} | {r['count']} | "
                             + ", ".join(f"{_code(a)} → {_code(b)}" for a, b in r["examples"]) + " |"),
    "children": lambda r: f"| {_cell(r['name'])} | {_cell(r['summary'])} |",
    "members": lambda r: (f"| {_code(r['name'])} | {r['kind']} | {_code(r['file'])} | "
                          + ", ".join(_code(f) for f in r["functions"]) + f" | {_cell(r['summary'])} | {r['role']} |"),
}
_SECTION_HEAD = {
    "hubs": ("### ハブ（トピック内 PageRank 上位、呼び出し数はメソッド対の数）",
             "| メンバー | 種類 | 呼ばれる | 呼ぶ |\n|---|---|---|---|"),
    "entry_points": ("### 入口（他トピックからだけ呼ばれるメンバー）",
                     "| メンバー | 呼び出し元 | 呼ばれる |\n|---|---|---|"),
    "dependencies": ("### トピック間の依存（呼び出し・埋め込み・実装）",
                     "| 方向 | トピック | 呼び出し | 重み | 例 |\n|---|---|---|---|---|"),
    "call_paths": ("### トピック内の主な呼び出し",
                   "| 呼び出し元 | 呼び出し先 | メソッド対 | 例 |\n|---|---|---|---|"),
    "children": ("### 子トピック", "| 子トピック | 要約 |\n|---|---|"),
    "members": ("### メンバー（型、またはファイルの関数をまとめた疑似型）",
                "| メンバー | 種類 | ファイル | 主な関数・メソッド | 要約 | 役割 |\n|---|---|---|---|---|---|"),
}


def _render_header(h: Mapping) -> str:
    roles = "、".join(f"{r} {n}" for r, n in h["roles"].items())
    lines = [f"## トピック: {h['name']}", f"- レベル {h['level']}"]  # no topic ID: pages never need it
    if h.get("parent"):
        lines.append(f"- 上位トピック: {h['parent']}")
    lines += [f"- 規模: 型・疑似型 {h['size']} 個、ファイル {h['file_count']} 個（{roles}）",
              f"- ハブ: {_code(h['hub'])}",
              f"- 凝集度 {h['cohesion']}、コンダクタンス {h['conductance']}"]
    return "\n".join(lines)


def render_evidence(evidence: Mapping) -> str:
    """Japanese Markdown of an evidence package, for the report prompt."""
    parts = [_render_header(evidence["header"])]
    for section, rows in evidence["sections"].items():
        if rows:
            title, head = _SECTION_HEAD[section]
            parts.append("\n".join([title, head] + [_ROW_RENDER[section](r) for r in rows]))
    if evidence.get("omitted_counts"):
        parts.append("### 省略\n" + "\n".join(
            f"- {section}: {n} 行（予算 {evidence['budget']} トークン、または件数上限）"
            for section, n in sorted(evidence["omitted_counts"].items())))
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════
# Mermaid scaffolds (real edges only)
# ═══════════════════════════════════════════════════════════════════


def _mermaid_label(text: str) -> str:
    return text.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def _labels(atoms: list[str], info: Mapping[str, dict]) -> dict[str, str]:
    """Diagram labels, unique within a topic: the display name, plus the
    file when two members share it."""
    counts = Counter(_display(info[a]) for a in atoms)
    return {a: _display(info[a]) if counts[_display(info[a])] == 1 else f"{_display(info[a])} ({info[a].get('path')})"
            for a in atoms}


def _scaffolds(t: Mapping, projection: Projection, method_names: Mapping[str, str], evidence: Mapping) -> dict:
    info = projection.info
    label = _labels([a for a in t["ranked"] if a in projection.eligible], info)
    nodes = [a for a in t["ranked"] if projection.degree.get(a, 0.0) > 0][:DIAGRAM_NODES]
    ids = {a: f"n{i + 1}" for i, a in enumerate(nodes)}
    lines = ["graph LR"] + [f'    {ids[a]}["{_mermaid_label(label[a])}"]' for a in nodes]
    allowed: set[tuple[str, str]] = set()
    for (u, v), rels in projection.directed.items():
        if u in ids and v in ids:
            n_calls = sum(len(rels.get(r, [])) for r in CALL_RELATIONS)
            parts = ([f"呼び出し {n_calls}"] if n_calls else []) \
                + (["埋め込み"] if "embed" in rels else []) + (["実装"] if "implements" in rels else [])
            if parts:
                arrow = "-->" if n_calls else "-.->"
                lines.append(f"    {ids[u]} {arrow}|{' / '.join(parts)}| {ids[v]}")
                allowed.add((label[u], label[v]))
    component = "\n".join(lines)

    # Flow: from the entry points (else the hub) along intra-topic method calls.
    inside = set(t["members"]) & projection.eligible
    starts = [r["class_id"] for r in evidence["sections"]["entry_points"]] or ([t["hub"]] if t.get("hub") else [])
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (u, v), rels in projection.directed.items():  # built in sorted order
        if u in inside and v in inside:
            for rel in CALL_RELATIONS:
                for _a, b in rels.get(rel, []):
                    out[u].append((v, method_names.get(b, b).rsplit(".", 1)[-1]))
    seen, queue, messages, participants = set(starts), list(starts), [], []
    while queue and len(messages) < FLOW_MESSAGES:
        u = queue.pop(0)
        for v, method in out.get(u, []):
            if len(messages) >= FLOW_MESSAGES:
                break
            messages.append((u, v, method))
            if v not in seen:
                seen.add(v)
                queue.append(v)
    for u, v, _l in messages:
        for a in (u, v):
            if a not in participants:
                participants.append(a)
    pid = {a: f"p{i + 1}" for i, a in enumerate(participants)}
    flow = ["sequenceDiagram"] + [f"    participant {pid[a]} as {_seq_label(label[a])}" for a in participants]
    for u, v, method in messages:
        flow.append(f"    {pid[u]}->>{pid[v]}: {_seq_label(method)}")
        allowed.add((_seq_label(label[u]).strip(), _seq_label(label[v]).strip()))
    labels = {label[a] for a in nodes} | {_seq_label(label[a]).strip() for a in participants}
    return {"component": component, "flow": "\n".join(flow) if messages else "",
            "edges": sorted([a, b] for a, b in allowed), "labels": sorted(labels)}


def _seq_label(text: str) -> str:
    return re.sub(r"[;#\n]", " ", text)


def component_scaffold(evidence: Mapping) -> str:
    """`graph LR` over the top members and their real intra-topic edges."""
    return evidence["scaffolds"]["component"]


def flow_scaffold(evidence: Mapping) -> str:
    """`sequenceDiagram` of real method calls from the entry points ("" when
    the topic has no internal calls)."""
    return evidence["scaffolds"]["flow"]


_BLOCK = re.compile(r"```mermaid[ \t]*\n(.*?)```", re.S)
_ARROW = re.compile(r"\s*(?:-->|---|-\.->|-\.-|==>|--\s[^-|]+?\s-->)\s*(?:\|[^|]*\|)?\s*")
_NODE = re.compile(r"^(\w+)\s*(?:\[\[?\"?([^\]\"]*)\"?\]?\]|\(\(?\"?([^)\"]*)\"?\)?\)|\{\"?([^}\"]*)\"?\})?")
_PARTICIPANT = re.compile(r"^\s*(?:participant|actor)\s+(\w+)(?:\s+as\s+(.+?))?\s*$")
_MESSAGE = re.compile(r"^\s*(\w+)\s*(?:-->>|->>|-->|->|--x|-x|--\)|-\))\s*[+-]?\s*(\w+)\s*:")


def _unescape(text: str) -> str:
    return text.replace("#quot;", '"').replace("#lt;", "<").replace("#gt;", ">").strip()


def diagram_edges(markdown: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Directed label pairs of every Mermaid block on a page, and the
    blocks whose diagram type is neither `graph`/`flowchart` nor
    `sequenceDiagram`."""
    edges: list[tuple[str, str]] = []
    unsupported: list[str] = []
    for block in _BLOCK.findall(markdown):
        lines = [ln for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("%%")]
        if not lines:
            continue
        kind = lines[0].split()[0]
        labels: dict[str, str] = {}
        if kind in ("graph", "flowchart"):
            raw_edges = []
            for ln in lines[1:]:
                segments = _ARROW.split(ln.strip())
                refs = []
                for seg in segments:
                    m = _NODE.match(seg.strip())
                    if not m:
                        continue
                    label = next((g for g in m.groups()[1:] if g), None)
                    if label is not None:
                        labels[m.group(1)] = _unescape(label)
                    refs.append(m.group(1))
                if len(segments) > 1:
                    raw_edges += list(zip(refs, refs[1:]))
            edges += [(labels.get(a, a), labels.get(b, b)) for a, b in raw_edges]
        elif kind == "sequenceDiagram":
            raw_edges = []
            for ln in lines[1:]:
                if m := _PARTICIPANT.match(ln):
                    labels[m.group(1)] = _unescape(m.group(2) or m.group(1))
                elif m := _MESSAGE.match(ln):
                    raw_edges.append((m.group(1), m.group(2)))
            edges += [(labels.get(a, a), labels.get(b, b)) for a, b in raw_edges if a != b]
        else:
            unsupported.append(kind)
    return edges, unsupported


def _resolve(label: str, known: list[str]) -> str:
    """The scaffold label a page label names: itself, or the longest scaffold
    label it starts with when an annotation follows (`Fee（料金計算）`,
    `Fee: 料金`), never a longer identifier (`FeeX`, `Fee.Total`)."""
    if label in known:
        return label
    best = ""
    for k in known:
        if len(k) > len(best) and label.startswith(k):
            ch = label[len(k)]
            if not (ch.isascii() and (ch.isalnum() or ch in "_.")):
                best = k
    return best or label


def diagram_problems(markdown: str, evidence: Mapping) -> list[str]:
    """Edges on the page that the scaffolds do not hold, and unsupported
    diagram types. Node labels may carry annotations."""
    allowed = {tuple(e) for e in evidence["scaffolds"]["edges"]}
    known = evidence["scaffolds"].get("labels", [])
    edges, unsupported = diagram_edges(markdown)
    problems = [f"edge {a} -> {b} is not in the graph" for a, b in edges
                if (_resolve(a, known), _resolve(b, known)) not in allowed]
    problems += [f"diagram type {k} is not allowed" for k in unsupported]
    return problems


def enforce_scaffolds(markdown: str, evidence: Mapping) -> tuple[str, list[str]]:
    """When the page's diagrams add edges, replace each Mermaid block with the
    scaffold of its type (`sequenceDiagram` → flow, anything else →
    component). Returns the page and the problems found."""
    problems = diagram_problems(markdown, evidence)
    if not problems:
        return markdown, []
    scaffolds = evidence["scaffolds"]

    def swap(m: re.Match) -> str:
        first = next((ln.split()[0] for ln in m.group(1).splitlines() if ln.strip()), "")
        body = scaffolds["flow"] if first == "sequenceDiagram" and scaffolds["flow"] else scaffolds["component"]
        return f"```mermaid\n{body}\n```"

    return _BLOCK.sub(swap, markdown), problems


# ═══════════════════════════════════════════════════════════════════
# Report prompt
# ═══════════════════════════════════════════════════════════════════


def build_report_prompt(evidence: Mapping) -> str:
    """The Japanese page prompt for one topic."""
    name = evidence["name"]
    scaffolds = evidence["scaffolds"]
    flow = scaffolds["flow"] or "（トピック内の呼び出しがないため、データフローの図は作らないでください。）"
    flow_block = f"```mermaid\n{flow}\n```" if scaffolds["flow"] else flow
    return "\n".join([
        "あなたはテクニカルライターです。以下のトピックについて、下の「構造的根拠」に書かれた事実だけに基づいて、"
        "日本語のドキュメントを作成してください。",
        "このトピックのメンバーは、Go コードの呼び出し・埋め込み・実装の構造から決まりました。",
        "",
        "## 構造的根拠",
        render_evidence(evidence),
        "",
        "## 図の雛形（グラフの実際の関係から生成）",
        "コンポーネント関係図:",
        f"```mermaid\n{scaffolds['component']}\n```",
        "データフロー:",
        flow_block,
        "",
        "以下のセクションを含む Markdown ドキュメントを作成してください:",
        "",
        f"# {name}",
        "## はじめに",
        "このトピックの役割とアプリケーション内での位置づけ。",
        "## アーキテクチャと設計",
        "### コンポーネント関係図",
        "上の雛形をそのまま使ってください。ノードと矢印を追加・削除しないでください。ラベルの追記だけできます。",
        "## データフロー",
        "上の雛形を使ってください（同じ規則）。",
        "## 主要コンポーネント",
        "主なメンバーごとの説明。",
        "## 依存関係",
        "他トピックとの依存（根拠の表にあるものだけ）。",
        "## 構造的根拠",
        "所属が呼び出し・埋め込み・実装の構造から決まったこと、主な根拠になった呼び出し。",
        "",
        "規則:",
        "- コード識別子（型・関数・メソッド・パッケージ・ファイル）はバッククォートで囲み、上の根拠にある名前だけを使ってください。",
        "- 根拠にない機能や関係を推測で書かないでください。",
        "- 日本語で記述してください。識別子はそのまま使用してください。",
    ])


# ═══════════════════════════════════════════════════════════════════
# Grounding check
# ═══════════════════════════════════════════════════════════════════


_FENCE = re.compile(r"```.*?```", re.S)
_SPAN = re.compile(r"`([^`\n]+)`")
_IDENT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_PSEUDO = re.compile(r"^(?:[A-Za-z_]\w*\.)*(?:<package>|<init>|init#\d+|_#\d+)(?:\.func\d+)*$")
_PATHLIKE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)+$|^[\w.\-]+\.go$", re.ASCII)  # import paths are ASCII
_RECV = re.compile(r"^\(\*?(\w+)\)\.(\w+)$")


# Topic vocabulary a page may quote (roles, label sources) and the shape of a
# node or topic ID (`<ID_PREFIX>_<16 hex>`): never code identifiers.
TOPIC_WORDS = frozenset({"primary", "secondary", "attached-file", "attached-test", "attached-generated",
                         "attached-excluded", "unassigned", "hub", "llm", "inherited", "fixed"})
_NODE_ID = re.compile(r"^\w+_[0-9a-f]{16}$")


def identifier_of(span: str) -> str | None:
    """The identifier a backtick span names, or None when it is not one
    (commands, expressions, numbers, Go keywords and predeclared names)."""
    s = span.strip().lstrip("*&").strip()
    if m := _RECV.match(s):
        s = f"{m.group(1)}.{m.group(2)}"
    s = re.sub(r"\((?:[^()]*)\)$", "", s)          # call parentheses
    s = re.sub(r"\[[^\[\]]*\]$", "", s)            # type arguments
    if not s or s in GO_PREDECLARED:
        return None
    if _IDENT.match(s) or _PSEUDO.match(s) or _PATHLIKE.match(s):
        return s
    return None


def build_name_set(nodes: Mapping[str, list[dict]]) -> set[str]:
    """Names a page may cite: type, function and method names and qualnames
    (plain, package-qualified, directory-qualified such as
    `model/labels.Labels`, and fully qualified), packages (import path,
    name, directory), module paths, directories (name, and every ancestor
    path of a file, also those that hold no .go file) and files (path, file
    name, directory)."""
    names: set[str] = set()
    pkg_of_file: dict[str, str] = {}
    dir_of_file: dict[str, str] = {}
    for f in nodes.get("Files", []):
        dir_of_file[f.get("file_id", "")] = f.get("directory") or ""
        names.update(filter(None, (f.get("path"), f.get("file_name"), f.get("directory"), f.get("package"),
                                   f.get("package_name"), f.get("module"))))
        parts = (f.get("path") or "").split("/")
        names.update("/".join(parts[:i]) for i in range(1, len(parts)))
        pkg_of_file[f.get("file_id", "")] = f.get("package_name") or (f.get("package") or "").rsplit("/", 1)[-1]
    for p in nodes.get("Packages", []):
        names.update(filter(None, (p.get("import_path"), p.get("name"), p.get("directory"), p.get("module"))))
    for d in nodes.get("Directories", []):
        names.update(filter(None, (d.get("name"),)))
    for c in nodes.get("Classes", []):
        pkg = pkg_of_file.get(c.get("file_id", "")) or (c.get("package") or "").rsplit("/", 1)[-1]
        names.update(filter(None, (c.get("name"), c.get("qualname"), c.get("fqcn"), c.get("package"))))
        for prefix in (pkg, dir_of_file.get(c.get("file_id", ""), "")):
            if prefix and c.get("qualname"):
                names.add(f"{prefix}.{c['qualname']}")
    for m in nodes.get("Methods", []):
        names.update(filter(None, (m.get("name"), m.get("qualname"), m.get("fqmn"))))
        for prefix in (pkg_of_file.get(m.get("file_id", ""), ""), dir_of_file.get(m.get("file_id", ""), "")):
            if prefix and m.get("qualname"):
                names.add(f"{prefix}.{m['qualname']}")
    names.discard("")
    return names


GROUNDING_MARKER = "<!-- codedoc:grounding -->"
GROUNDING_HEADING = "## 識別子の確認"
# A section written before the marker existed: the heading, then the count line.
_LEGACY_SECTION = re.compile(rf"^{re.escape(GROUNDING_HEADING)}[ \t]*\n"
                             r"本文のコード識別子 \d+ 個のうち \d+ 個がグラフに存在します。", re.M)


def strip_grounding(markdown: str) -> str:
    """The page without the section `append_grounding` added: everything from
    its marker, or, on a page without one, from a final legacy section (the
    heading directly followed by the count line). A `## 識別子の確認`
    section the LLM wrote itself stays, so its identifiers are checked."""
    i = markdown.find(GROUNDING_MARKER)
    if i < 0:
        heads = [m.start() for m in re.finditer(r"^## .*$", markdown, re.M)]
        legacy = [m.start() for m in _LEGACY_SECTION.finditer(markdown)]
        if legacy and heads[-1] == legacy[-1]:
            i = legacy[-1]
    return markdown if i < 0 else markdown[:i]


def grounding_check(markdown: str, name_set: set[str] | frozenset[str], skip: Iterable[str] = ()) -> dict:
    """Identifier existence on one page: every backtick span outside fenced
    blocks that names an identifier must be in `name_set`. The section that
    `append_grounding` adds is ignored, so a page scores the same before and
    after it. Topic names given in `skip` (both runs' topic names, which
    pages quote), topic vocabulary (`TOPIC_WORDS`) and node or topic IDs are
    skipped unless the graph has such a name."""
    skip = set(skip)
    checked = found = skipped = 0
    missing: Counter = Counter()
    for span in _SPAN.findall(_FENCE.sub("", strip_grounding(markdown))):
        ident = identifier_of(span)
        if ident is None or (ident not in name_set and (span.strip() in skip or ident in skip
                                                         or ident in TOPIC_WORDS or _NODE_ID.match(ident))):
            skipped += 1
            continue
        checked += 1
        if ident in name_set:
            found += 1
        else:
            missing[ident] += 1
    return {"checked": checked, "found": found, "missing": sorted(missing),
            "missing_count": sum(missing.values()), "skipped": skipped,
            "rate": found / checked if checked else None}


def append_grounding(markdown: str, result: Mapping) -> str:
    """The page with its grounding result at the bottom, after
    `GROUNDING_MARKER` (an HTML comment, invisible when rendered); an
    earlier grounding section is replaced."""
    body = strip_grounding(markdown)
    if body != markdown:  # drop the old section's separator too
        body = body.rstrip()
        body = body[:-3] if body.endswith("---") else body
    markdown = body.rstrip("\n")
    lines = ["", "---", "", GROUNDING_MARKER, GROUNDING_HEADING,
             f"本文のコード識別子 {result['checked']} 個のうち {result['found']} 個がグラフに存在します。"]
    if result["missing"]:
        lines.append("グラフに見つからない識別子: " + ", ".join(f"`{m}`" for m in result["missing"]))
    return markdown.rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def aggregate_grounding(results: Iterable[Mapping]) -> dict:
    """Totals over several pages."""
    results = list(results)
    checked = sum(r["checked"] for r in results)
    found = sum(r["found"] for r in results)
    missing = Counter()
    for r in results:
        missing.update(r["missing"])
    return {"pages": len(results), "checked": checked, "found": found,
            "rate": found / checked if checked else None,
            "missing_distinct": len(missing), "top_missing": [m for m, _n in missing.most_common(20)]}
