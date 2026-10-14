// Visual vocabulary of the graph explorer (spec R51): node kinds (colour,
// shape, Japanese label), edge tables (line style, Japanese hint) and the
// Cytoscape stylesheet built from them. Kinds and tables the backend reports
// but this file does not know get the "other" style, so new tables still draw.

export const NODE_KINDS = {
  file: { label: "ファイル", color: "#7aa2f7", shape: "round-rectangle", size: 30 },
  package: { label: "パッケージ", color: "#bb9af7", shape: "round-hexagon", size: 36 },
  directory: { label: "ディレクトリ", color: "#e0af68", shape: "round-tag", size: 30 },
  struct: { label: "構造体", color: "#9ece6a", shape: "round-diamond", size: 32 },
  interface: { label: "インタフェース", color: "#73daca", shape: "round-diamond", size: 32, hollow: true },
  named: { label: "名前付き型", color: "#c3e88d", shape: "round-pentagon", size: 30 },
  alias: { label: "型エイリアス", color: "#c3e88d", shape: "round-pentagon", size: 30, hollow: true, dashed: true },
  pseudo: { label: "<package>（ファイルの関数群）", color: "#565f89", shape: "round-octagon", size: 22 },
  func: { label: "関数", color: "#ff9e64", shape: "ellipse", size: 26 },
  method: { label: "メソッド", color: "#7dcfff", shape: "ellipse", size: 26 },
  interface_method: { label: "インタフェースのメソッド", color: "#73daca", shape: "ellipse", size: 26, hollow: true },
  closure: { label: "クロージャ", color: "#449dab", shape: "ellipse", size: 18 },
  init: { label: "init 関数", color: "#a9b1d6", shape: "ellipse", size: 22 },
  var_init: { label: "変数の初期化", color: "#a9b1d6", shape: "ellipse", size: 22 },
  topic: { label: "トピック", color: "#f7768e", shape: "round-octagon", size: 40 },
  other: { label: "その他", color: "#a9b1d6", shape: "ellipse", size: 24 },
};

// Edge tables: colour, line style, arrows. `dim` marks containment and
// definition edges, drawn thin and faded under the relations that matter.
export const EDGE_TABLES = {
  MethodCalls: { hint: "呼び出し", out: "呼び出し先", in: "呼び出し元", color: "#7dcfff", style: "solid", arrow: "triangle" },
  PossiblyCalls: { hint: "呼び出し候補", out: "呼び出し候補（先）", in: "候補として呼ぶ元", color: "#ff9e64", style: "dashed", arrow: "triangle" },
  ClassImplements: { hint: "実装", out: "実装するインタフェース", in: "実装する型", color: "#73daca", style: "dotted", arrow: "triangle", hollow: true },
  ClassEmbeds: { hint: "埋め込み", out: "埋め込む型", in: "埋め込み先の型", color: "#9ece6a", style: "solid", arrow: "none", sourceArrow: "diamond" },
  PackageImports: { hint: "インポート", out: "インポートするパッケージ", in: "インポート元のファイル", color: "#bb9af7", style: "solid", arrow: "vee" },
  FileDependsOn: { hint: "ファイル依存", out: "依存先のファイル", in: "依存元のファイル", color: "#7aa2f7", style: "solid", arrow: "triangle" },
  TopicDependsOn: { hint: "トピック間の依存", out: "依存先のトピック", in: "依存元のトピック", color: "#f7768e", style: "solid", arrow: "triangle" },
  CrossRepoCalls: { hint: "リポジトリ間の呼び出し", out: "呼び出し先（他リポジトリ）", in: "呼び出し元（他リポジトリ）", color: "#db4b4b", style: "dashed", arrow: "triangle" },
  CrossRepoRef: { hint: "リポジトリ間の型参照", out: "参照先（他リポジトリ）", in: "参照元（他リポジトリ）", color: "#db4b4b", style: "dashed", arrow: "triangle" },
  CrossRepoImports: { hint: "リポジトリ間のインポート", out: "インポート先（他リポジトリ）", in: "インポート元（他リポジトリ）", color: "#db4b4b", style: "dashed", arrow: "vee" },
  FileDefinesClass: { hint: "型の定義", out: "定義する型", in: "定義しているファイル", dim: true },
  ClassDefinesMethod: { hint: "メソッドの定義", out: "メソッド・関数", in: "属する型", dim: true },
  DirContainsFile: { hint: "ディレクトリの包含", out: "含むファイル", in: "ディレクトリ", dim: true },
  PackageContainsFile: { hint: "パッケージの包含", out: "含むファイル", in: "パッケージ", dim: true },
  FileBelongsToTopic: { hint: "トピックへの所属", out: "所属トピック", in: "所属ファイル", dim: true },
  ClassBelongsToTopic: { hint: "トピックへの所属（型）", out: "所属トピック", in: "所属する型", dim: true },
  TopicContainsTopic: { hint: "サブトピック", out: "サブトピック", in: "親トピック", dim: true },
  tree: { hint: "ディレクトリの親子", out: "子ディレクトリ", in: "親ディレクトリ", color: "#565f89", style: "solid", arrow: "none", length: 70 },
};

const DIM_EDGE = { color: "#414868", style: "solid", arrow: "none" };
const OTHER_EDGE = { hint: "", color: "#565f89", style: "solid", arrow: "triangle" };

// "呼び出し元" etc.: the relation a table has in one direction, seen from a node.
export function relationLabel(table, direction) {
  const t = EDGE_TABLES[table];
  if (t && t[direction]) return t[direction];
  return `${table} ${direction === "out" ? "→" : "←"}`;
}

export const ROLE_LABELS = {
  primary: "主メンバー",
  "attached-file": "同じファイルから付随",
  "attached-test": "テストとして付随",
  "attached-generated": "生成コードとして付随",
  "attached-excluded": "除外対象として付随",
  unassigned: "未分類",
  member: "メンバー",
};

// Member roles of a leiden topic (ClassBelongsToTopic.role), drawn as the
// slices of the topic node's pie.
export const ROLE_SLICES = [
  ["primary", "#7aa2f7"],
  ["attached-file", "#9ece6a"],
  ["attached-test", "#e0af68"],
  ["attached-generated", "#565f89"],
  ["other", "#2f3050"],
];

export const ORIGIN_LABELS = { package: "パッケージ", command: "コマンド", test: "テスト", xtest: "外部テスト" };

export function kindInfo(kind) {
  return NODE_KINDS[kind] || NODE_KINDS.other;
}

export function tableInfo(table) {
  const info = EDGE_TABLES[table] || OTHER_EDGE;
  return info.dim ? { ...DIM_EDGE, ...info } : info;
}

// Tables in a stable display order: the known ones as listed above, then the rest.
export function orderTables(tables) {
  const known = Object.keys(EDGE_TABLES);
  const rank = (t) => (known.includes(t) ? known.indexOf(t) : known.length);
  return [...tables].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

const BG = "#0f1117";
// Canvas labels: the app's font, then Japanese fonts for topic names. Cytoscape
// accepts only double quotes here.
const FONT = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans", "Noto Sans JP", ' +
  '"Noto Sans CJK JP", "Yu Gothic UI", Meiryo, sans-serif';

export function stylesheet() {
  const kinds = Object.entries(NODE_KINDS).map(([kind, k]) => ({
    selector: `node[kind = "${kind}"]`,
    style: {
      shape: k.shape,
      "background-color": k.hollow ? BG : k.color,
      "border-color": k.color,
      "border-width": k.hollow ? 3 : 0,
      "border-style": k.dashed ? "dashed" : "solid",
    },
  }));
  const tables = Object.keys(EDGE_TABLES).map((table) => {
    const t = tableInfo(table);
    return {
      selector: `edge[table = "${table}"]`,
      style: {
        "line-color": t.color,
        "line-style": t.style,
        "target-arrow-color": t.color,
        "source-arrow-color": t.color,
        "target-arrow-shape": t.arrow,
        "target-arrow-fill": t.hollow ? "hollow" : "filled",
        "source-arrow-shape": t.sourceArrow || "none",
        ...(t.style === "dashed" ? { "line-dash-pattern": [6, 4] } : {}),
        ...(t.dim ? { opacity: 0.45, width: 1 } : {}),
      },
    };
  });
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        width: "data(w)",
        height: "data(w)",
        "background-color": NODE_KINDS.other.color,
        color: "#a9b1d6",
        "font-family": FONT,
        "font-size": "mapData(w, 18, 110, 10, 17)", // larger nodes, larger labels
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 4,
        "text-wrap": "ellipsis",
        "text-max-width": 150,
        "text-outline-color": BG,
        "text-outline-width": 2,
        "min-zoomed-font-size": 6,
        "overlay-opacity": 0,
      },
    },
    ...kinds,
    {
      selector: "node:parent",
      style: {
        shape: "round-rectangle",
        "background-color": "#1a1b26",
        "background-opacity": 0.75,
        "border-width": 1,
        "border-color": "#292e42",
        "border-style": "solid",
        label: "data(label)",
        "text-valign": "top",
        "text-halign": "center",
        "text-margin-y": -4,
        "font-size": 14,
        "font-weight": 600,
        color: "#c0caf5",
        padding: 14,
      },
    },
    { selector: "node[color]", style: { "background-color": "data(color)", "border-color": "data(color)" } },
    {
      selector: "node[?pie]:childless",
      style: {
        "pie-size": "78%",
        ...Object.fromEntries(ROLE_SLICES.flatMap(([, color], i) => [
          [`pie-${i + 1}-background-color`, color],
          [`pie-${i + 1}-background-size`, `mapData(p${i + 1}, 0, 100, 0, 100)`],
        ])),
      },
    },
    { selector: 'node[kind = "topic"]', style: { "text-max-width": 260, "font-weight": 600, color: "#c0caf5" } },
    { selector: 'node:parent[kind = "topic"]', style: { "border-color": "#f7768e", "border-width": 1.5 } },
    {
      selector: "node[?unassigned]",
      style: { "background-color": "#3b4261", "border-color": "#565f89", "border-width": 2, "border-style": "dashed" },
    },
    { selector: "node[?virtual]", style: { "border-width": 2, "border-style": "dotted", "border-color": "#e0af68" } },
    { selector: "node.center", style: { "border-width": 4, "border-color": "#c0caf5", "border-style": "double", color: "#c0caf5", "font-weight": 600 } },
    { selector: "node:selected", style: { "border-width": 3, "border-color": "#ffffff", "border-style": "solid", color: "#ffffff" } },
    {
      selector: "edge",
      style: {
        width: "data(ew)",
        "curve-style": "bezier",
        "line-color": OTHER_EDGE.color,
        "target-arrow-color": OTHER_EDGE.color,
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.8,
        opacity: 0.85,
      },
    },
    ...tables,
    { selector: 'edge[table = "TopicDependsOn"]', style: { opacity: "mapData(ew, 1.5, 8, 0.3, 0.95)" } },
    { selector: "edge:loop", style: { "control-point-step-size": 40 } },
    { selector: ".faded", style: { opacity: 0.12 } },
    { selector: "edge.focused", style: { opacity: 1, width: "mapData(ew, 0, 8, 2, 9)", "z-index": 9 } },
    { selector: "node.focused", style: { color: "#c0caf5", "z-index": 9 } },
    { selector: "node.hovered", style: { "border-width": 3, "border-color": "#c0caf5", color: "#ffffff", "z-index": 9 } },
    { selector: "edge.path", style: { "line-color": "#e0af68", "target-arrow-color": "#e0af68", width: 4, opacity: 1, "z-index": 10 } },
    { selector: "node.path", style: { "border-width": 4, "border-color": "#e0af68", "border-style": "solid" } },
    { selector: ".off", style: { display: "none" } },
  ];
}

// Node diameter: the kind's size, scaled by an overview's weight (files,
// members) when there is one, and larger for the focused node.
export function nodeSize(node, { center } = {}) {
  const base = kindInfo(node.kind).size;
  const weight = node.total ?? node.size ?? node.files;
  const scaled = typeof weight === "number" && weight > 0 ? base * 0.8 + Math.sqrt(weight) * 5 : base;
  return Math.round(Math.min(center ? scaled * 1.5 : scaled, 120));
}

// Edge width: 1.5, or log-scaled by an aggregated edge's weight.
export function edgeWidth(edge) {
  const w = edge.attrs?.weight;
  return typeof w === "number" && w > 0 ? Math.min(1 + Math.log2(1 + w) * 1.3, 8) : 1.5;
}
