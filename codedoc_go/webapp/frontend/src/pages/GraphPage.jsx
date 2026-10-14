import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import GraphCanvas from "../components/GraphCanvas";
import GraphDetails from "../components/GraphDetails";
import GraphSearch from "../components/GraphSearch";
import { fetchJson, useJson, useRepo, withRepo } from "../repo";
import { kindInfo, NODE_KINDS, orderTables, ROLE_LABELS, ROLE_SLICES, tableInfo, EDGE_TABLES } from "../graphStyle";
import "../graph.css";

// The graph explorer (spec R51): a capped neighbourhood around one node, or
// an aggregated map (packages, directories, topics), drawn from the repo's
// graph_rows/ through /api/graph/*. The whole graph is never drawn.

const MODES = [
  { key: "focus", label: "近傍" },
  { key: "packages", label: "パッケージ" },
  { key: "directories", label: "ディレクトリ" },
  { key: "topics", label: "トピック" },
];
const OVERVIEWS = ["packages", "directories", "topics"];
const LAYOUTS = { focus: "focus", packages: "fcose", directories: "fcose", topics: "topics" };
const MAX_SHOWN = 900; // nodes on screen at most; expanding stops there
const TRAIL_LENGTH = 8;

function graphUrl(path, params, selected) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== false) q.set(k, String(v));
  });
  const text = q.toString();
  return withRepo(text ? `${path}?${text}` : path, selected);
}

function omittedText(omitted) {
  const parts = Object.entries(omitted || {}).map(([t, n]) => `${t} ${n} 件`);
  return parts.length ? `（省略: ${parts.join("、")}）` : "";
}

// Names repeat (main, Run, internal, logging.go, ...). A repeated name gets
// context: a package or directory the last two segments of its path, a file
// its directory, a type or function its package.
function distinctLabels(graph) {
  const count = new Map();
  graph.nodes.forEach((n) => count.set(n.label, (count.get(n.label) || 0) + 1));
  if (![...count.values()].some((c) => c > 1)) return graph;
  const segments = (text) => (text || "").split("/").filter(Boolean);
  const rename = (n) => {
    if (count.get(n.label) < 2) return n;
    if (n.table === "Packages" && segments(n.sub).length) return { ...n, label: segments(n.sub).slice(-2).join("/") };
    if (n.table === "Directories" && segments(n.path).length > 1) return { ...n, label: segments(n.path).slice(-2).join("/") };
    if (n.table === "Files" && segments(n.sub).length) return { ...n, label: `${segments(n.sub).pop()}/${n.label}` };
    if ((n.table === "Methods" || n.table === "Classes") && segments(n.sub).length) {
      return { ...n, label: `${n.label} (${segments(n.sub).pop()})` };
    }
    return n;
  };
  return { ...graph, nodes: graph.nodes.map(rename) };
}

const MODULE_COLORS = ["#7aa2f7", "#9ece6a", "#e0af68", "#f7768e", "#7dcfff", "#ff9e64", "#bb9af7",
                       "#73daca", "#c3e88d", "#2ac3de", "#b4f9f8", "#ff007c"];

// The packages map coloured by module, when there are two or more: the
// largest modules get their own colour, the rest share grey. Returns the
// graph and the legend rows [label, colour] (labels without the common prefix).
function colorByModule(graph) {
  const count = new Map();
  graph.nodes.forEach((n) => n.module && count.set(n.module, (count.get(n.module) || 0) + 1));
  if (count.size < 2) return { graph, modules: [] };
  const ranked = [...count].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([m]) => m);
  const color = new Map(ranked.map((m, i) => [m, MODULE_COLORS[i] || "#565f89"]));
  let prefix = ranked[0];
  ranked.forEach((m) => {
    while (prefix && !m.startsWith(prefix)) prefix = prefix.slice(0, prefix.lastIndexOf("/", prefix.length - 2) + 1);
  });
  const short = (m) => m.slice(prefix.length) || m;
  const modules = ranked.slice(0, MODULE_COLORS.length).map((m) => [short(m), color.get(m)]);
  if (ranked.length > MODULE_COLORS.length) modules.push([`その他 ${ranked.length - MODULE_COLORS.length} モジュール`, "#565f89"]);
  return { graph: { ...graph, nodes: graph.nodes.map((n) => (n.module ? { ...n, color: color.get(n.module) } : n)) }, modules };
}

// Leiden topics: each topic's member roles as the percentages of its pie.
function rolePies(graph) {
  const known = new Set(ROLE_SLICES.map(([role]) => role));
  return {
    ...graph,
    nodes: graph.nodes.map((n) => {
      const roles = Object.entries(n.roles || {});
      const total = roles.reduce((sum, [, c]) => sum + c, 0);
      if (!total) return n;
      const pie = { pie: true };
      ROLE_SLICES.forEach(([role], i) => {
        const count = role === "other"
          ? roles.filter(([r]) => !known.has(r)).reduce((sum, [, c]) => sum + c, 0)
          : n.roles[role] || 0;
        pie[`p${i + 1}`] = (100 * count) / total;
      });
      return { ...n, ...pie };
    }),
  };
}

// A directory with subdirectories past the cap says how many on the canvas;
// `plainLabel` keeps its name for the trail and the details header.
function countHidden(graph) {
  return {
    ...graph,
    nodes: graph.nodes.map((n) => (n.more ? { ...n, label: `${n.label} +${n.more}`, plainLabel: n.label } : n)),
  };
}

// The URL search of a view, as useSearchParams prints it (the trail compares it).
function searchOf(params) {
  return new URLSearchParams(params).toString();
}

function emptyGraph(key, mode) {
  return { key, mode, center: null, roots: [], nodes: [], edges: [], anchor: null, topicMode: null };
}

function mergeInto(graph, data, anchor) {
  const have = new Set(graph.nodes.map((n) => n.id));
  const room = Math.max(MAX_SHOWN - graph.nodes.length, 0);
  const fresh = data.nodes.filter((n) => !have.has(n.id));
  const nodes = [...graph.nodes, ...fresh.slice(0, room)];
  const ids = new Set(nodes.map((n) => n.id));
  const haveEdges = new Set(graph.edges.map((e) => e.id));
  const edges = [...graph.edges, ...data.edges.filter((e) => !haveEdges.has(e.id) && ids.has(e.source) && ids.has(e.target))];
  return {
    graph: { ...graph, nodes, edges, anchor, roots: [...new Set([...(graph.roots || []), anchor])] },
    added: Math.min(fresh.length, room),
    capped: fresh.length > room,
  };
}

// The status line: the hovered node, else what the view shows. Hover state
// lives here, so moving the pointer over the graph re-renders only this line.
function StatusLine({ register, children }) {
  const [hover, setHover] = useState(null);
  useEffect(() => register(setHover), [register]);
  return (
    <div className="graph-status">
      {hover ? (
        <>
          <span className="graph-kind-dot" style={{ background: kindInfo(hover.kind).color }} />
          <strong>{hover.label}</strong>
          <span>{kindInfo(hover.kind).label}</span>
          {hover.path && <span className="mono">{hover.path}{hover.line ? `:${hover.line}` : ""}</span>}
        </>
      ) : (
        children
      )}
    </div>
  );
}

export default function GraphPage() {
  const { ready, selected } = useRepo();
  const [params, setParams] = useSearchParams();
  const focusId = params.get("node");
  const mode = focusId ? "focus" : OVERVIEWS.includes(params.get("mode")) ? params.get("mode") : "packages";
  const root = mode === "directories" ? params.get("root") || "" : "";

  const { data: stats, error: statsError } = useJson(ready ? withRepo("/api/graph/stats", selected) : null);

  const [depth, setDepth] = useState(1);
  const [hideTests, setHideTests] = useState(false);
  const [hideGenerated, setHideGenerated] = useState(false);
  const [hiddenTables, setHiddenTables] = useState(() => new Set());
  const [colorModules, setColorModules] = useState(true);
  const [loaded, setLoaded] = useState({ key: null, graph: null, error: null, notice: null });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [selectedId, setSelectedId] = useState(undefined);
  const [details, setDetails] = useState({ id: null, data: null, error: null });
  const hoverSetter = useRef(null);
  const registerHover = useCallback((set) => {
    hoverSetter.current = set;
  }, []);
  const [pathStart, setPathStart] = useState(null);
  const [pathIds, setPathIds] = useState([]);
  const [trail, setTrail] = useState([]);
  const [legendOpen, setLegendOpen] = useState(true);
  const [lastFocus, setLastFocus] = useState(null);
  const [reload, setReload] = useState(0);
  const canvasRef = useRef(null);
  const tablesRef = useRef(undefined);

  const statTables = useMemo(
    () => orderTables((stats?.edge_tables || []).filter((t) => t.rows > 0).map((t) => t.table)),
    [stats]
  );
  const enabledTables = useMemo(() => statTables.filter((t) => !hiddenTables.has(t)), [statTables, hiddenTables]);
  // ?tables= for the neighbourhood: absent when every table is on
  const tablesParam = enabledTables.length === statTables.length ? undefined : enabledTables.join(",");
  useEffect(() => {
    tablesRef.current = tablesParam;
  });

  const viewKey = JSON.stringify([selected, mode, focusId, root, mode === "focus" ? depth : 0, hideTests, hideGenerated, reload]);

  // Load the view: the neighbourhood of ?node=, or an overview map.
  useEffect(() => {
    if (!ready || !stats) return undefined;
    let live = true;
    const flags = { hide_tests: hideTests, hide_generated: hideGenerated };
    const url = mode === "focus"
      ? graphUrl(`/api/graph/neighbors/${encodeURIComponent(focusId)}`, { depth, tables: tablesRef.current, ...flags }, selected)
      : graphUrl(`/api/graph/overview/${mode}`, { ...flags, root: root || undefined }, selected);
    fetchJson(url)
      .then((data) => {
        if (!live) return;
        const graph = {
          key: viewKey,
          mode,
          center: mode === "focus" ? focusId : null,
          roots: mode === "focus" ? [focusId] : [],
          nodes: data.nodes,
          edges: data.edges,
          anchor: null,
          topicMode: data.mode,
        };
        const parts = [];
        if (data.truncated) parts.push(`上限で打ち切りました${omittedText(data.omitted)}`);
        setLoaded({ key: viewKey, graph, error: null, notice: parts.join(" ") || null });
      })
      // an empty graph, so the canvas does not keep the previous drawing
      .catch((e) => live && setLoaded({ key: viewKey, graph: emptyGraph(viewKey, mode), error: e.message, notice: null }));
    return () => {
      live = false;
    };
  }, [ready, stats, viewKey, mode, focusId, root, depth, hideTests, hideGenerated, selected]);

  const current = loaded.key === viewKey ? loaded : null;
  const loading = !statsError && (!current || busy);
  const baseGraph = current?.graph || null;
  const view = useMemo(() => {
    if (!baseGraph) return { graph: null, modules: [] };
    const named = distinctLabels(baseGraph);
    if (baseGraph.mode === "packages" && colorModules) return colorByModule(named);
    if (baseGraph.mode === "directories") return { graph: countHidden(named), modules: [] };
    if (baseGraph.topicMode === "levels") return { graph: rolePies(named), modules: [] };
    return { graph: named, modules: [] };
  }, [baseGraph, colorModules]);
  const graph = view.graph;
  const nodeById = useMemo(() => new Map((graph?.nodes || []).map((n) => [n.id, n])), [graph]);

  // The selection: what was clicked, else the focused node.
  const selectedNode =
    (selectedId && nodeById.get(selectedId)) || (selectedId === null ? null : mode === "focus" ? nodeById.get(focusId) : null) || null;
  const detailId = selectedNode && !selectedNode.virtual ? selectedNode.id : null;

  useEffect(() => {
    if (!detailId) return undefined;
    let live = true;
    fetchJson(withRepo(`/api/graph/node/${encodeURIComponent(detailId)}`, selected))
      .then((data) => live && setDetails({ id: detailId, data, error: null }))
      .catch((e) => live && setDetails({ id: detailId, data: null, error: e.message }));
    return () => {
      live = false;
    };
  }, [detailId, selected]);
  const shownDetails = details.id === detailId ? details : { data: null, error: null };

  const remember = (label, search) => {
    setTrail((t) =>
      [...t.filter((x) => x.search !== search || x.repo !== selected), { label, search, repo: selected }].slice(-TRAIL_LENGTH)
    );
  };
  const shownTrail = trail.filter((t) => t.repo === selected);

  const focus = (id, label) => {
    const known = nodeById.get(id);
    const name = label || known?.plainLabel || known?.label || id;
    if (id === focusId) setReload((r) => r + 1);
    else setParams({ node: id });
    setLastFocus({ id, label: name });
    setSelectedId(undefined);
    setPathIds([]);
    setNotice(null);
    remember(name, searchOf({ node: id }));
  };

  // The directory map from one directory down (its subtree past the cap).
  const showTree = (path) => {
    setParams(path ? { mode: "directories", root: path } : { mode: "directories" });
    setSelectedId(undefined);
    setNotice(null);
    remember(path ? `ディレクトリ: ${path}` : "ディレクトリ",
             searchOf(path ? { mode: "directories", root: path } : { mode: "directories" }));
  };

  const showOverview = (key) => {
    setParams({ mode: key });
    setSelectedId(undefined);
    setPathIds([]);
    setNotice(null);
    remember(MODES.find((m) => m.key === key).label, searchOf({ mode: key }));
  };

  // Adds fetched nodes and edges to the view; the message goes with it.
  const mergeData = (data, anchor, message) => {
    setNotice(null);
    setLoaded((prev) => {
      if (prev.key !== viewKey || !prev.graph || prev.error) return prev;
      const { graph: merged, added, capped } = mergeInto(prev.graph, data, anchor);
      const parts = [message ? message(added) : `${added} ノードを追加しました`];
      if (data.truncated) parts.push(`上限で打ち切りました${omittedText(data.omitted)}`);
      if (capped) parts.push(`画面のノードが ${MAX_SHOWN} に達しました。「中心にする」で絞ってください`);
      return { ...prev, graph: merged, notice: parts.join(" ") };
    });
  };

  // Double click or 「近傍を展開」: in the focus view add the node's neighbours
  // (all enabled tables, or one table and direction); on a map, focus on it.
  const expand = (id, opts = {}) => {
    const node = nodeById.get(id);
    if (mode !== "focus") {
      if (node && !node.virtual) focus(id, node.plainLabel || node.label);
      return;
    }
    let tables = tablesParam;
    if (opts.table) {
      tables = opts.table;
      if (hiddenTables.has(opts.table)) {
        setHiddenTables((h) => {
          const next = new Set(h);
          next.delete(opts.table);
          return next;
        });
      }
    }
    setBusy(true);
    const url = graphUrl(
      `/api/graph/neighbors/${encodeURIComponent(id)}`,
      { depth: 1, tables, direction: opts.direction || "both", hide_tests: hideTests, hide_generated: hideGenerated },
      selected
    );
    fetchJson(url)
      .then((data) => mergeData(data, id, (n) => `${node?.label || id} から ${n} ノードを追加しました`))
      .catch((e) => setNotice(e.message))
      .finally(() => setBusy(false));
  };

  const toggleTable = (table) => {
    const turningOn = hiddenTables.has(table);
    setHiddenTables((h) => {
      const next = new Set(h);
      if (next.has(table)) next.delete(table);
      else next.add(table);
      return next;
    });
    if (turningOn && mode === "focus" && graph && focusId) {
      setBusy(true);
      const url = graphUrl(
        `/api/graph/neighbors/${encodeURIComponent(focusId)}`,
        { depth, tables: table, hide_tests: hideTests, hide_generated: hideGenerated },
        selected
      );
      fetchJson(url)
        .then((data) => mergeData(data, focusId, (n) => `${table} の近傍 ${n} ノードを追加しました`))
        .catch((e) => setNotice(e.message))
        .finally(() => setBusy(false));
    }
  };

  const findPath = (target) => {
    if (!pathStart) return;
    setBusy(true);
    const base = { source: pathStart.id, target: target.id, tables: tablesParam, max_hops: 6 };
    const get = (direction) => fetchJson(graphUrl("/api/graph/path", { ...base, direction }, selected));
    get("out")
      .then((data) => ({ data, undirected: false }))
      .catch(() => get("both").then((data) => ({ data, undirected: true })))
      .then(({ data, undirected }) => {
        mergeData(data, pathStart.id, () =>
          `${pathStart.label} → ${target.label}: ${data.hops} 手の最短経路${undirected ? "（向きを無視）" : ""}`
        );
        setPathIds([...data.nodes.map((n) => n.id), ...data.edges.map((e) => e.id)]);
      })
      .catch((e) => setNotice(e.message))
      .finally(() => setBusy(false));
  };

  const exportPng = () => {
    const blob = canvasRef.current?.png();
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `codedoc-graph-${stats?.repo || "repo"}-${mode === "focus" ? "focus" : mode}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  // Chips: every table of the repo in the focus view (they also choose what an
  // expansion fetches), the tables drawn in an overview.
  const chipTables = useMemo(() => {
    if (!graph) return [];
    if (mode === "focus") return statTables;
    return orderTables([...new Set(graph.edges.map((e) => e.table))]);
  }, [graph, mode, statTables]);
  const edgeCounts = useMemo(() => {
    const c = {};
    (graph?.edges || []).forEach((e) => {
      c[e.table] = (c[e.table] || 0) + 1;
    });
    return c;
  }, [graph]);
  const kindsShown = useMemo(() => {
    const kinds = new Set((graph?.nodes || []).map((n) => n.kind));
    return Object.keys(NODE_KINDS).filter((k) => kinds.has(k)).concat([...kinds].filter((k) => !NODE_KINDS[k]));
  }, [graph]);

  if (statsError) {
    return (
      <main className="main-content">
        <div className="empty-state">
          <h3>グラフを読めません</h3>
          <p>{statsError}</p>
        </div>
      </main>
    );
  }

  const topicsMissing = mode === "topics" && graph && graph.topicMode === "none";
  const viewError = current?.error;
  const shownNotice = notice || current?.notice;
  const nodeTables = (stats?.node_tables || []).filter((t) => t.rows > 0).map((t) => t.table);

  return (
    <main className="main-content full-width graph-page">
      <div className="graph-toolbar">
        <GraphSearch
          selected={selected}
          hideTests={hideTests}
          hideGenerated={hideGenerated}
          nodeTables={nodeTables}
          onPick={(item) => focus(item.id, item.label)}
        />
        <div className="graph-tabs" role="tablist" aria-label="表示">
          {MODES.map((m) => (
            <button
              type="button"
              key={m.key}
              role="tab"
              aria-selected={mode === m.key}
              disabled={m.key === "focus" && !focusId && !lastFocus}
              title={m.key === "focus" && !focusId && !lastFocus ? "検索でノードを選ぶと近傍を表示します" : undefined}
              className={`graph-tab${mode === m.key ? " active" : ""}`}
              onClick={() => {
                if (m.key !== "focus") showOverview(m.key);
                else if (!focusId && lastFocus) focus(lastFocus.id, lastFocus.label);
              }}
            >
              {m.label}
              {m.key === "topics" && stats?.topics === "none" ? "（なし）" : ""}
            </button>
          ))}
        </div>
        <div className="graph-options">
          {mode === "focus" && (
            <label className="graph-option">
              深さ
              <select
                id="graph-depth"
                name="graph-depth"
                className="graph-select"
                value={depth}
                onChange={(e) => setDepth(Number(e.target.value))}
              >
                <option value={1}>1</option>
                <option value={2}>2</option>
              </select>
            </label>
          )}
          <label className="graph-option">
            <input id="graph-hide-tests" name="graph-hide-tests" type="checkbox" checked={hideTests}
                   onChange={(e) => setHideTests(e.target.checked)} />
            テストを隠す
          </label>
          <label className="graph-option">
            <input id="graph-hide-generated" name="graph-hide-generated" type="checkbox" checked={hideGenerated}
                   onChange={(e) => setHideGenerated(e.target.checked)} />
            生成コードを隠す
          </label>
          {mode === "packages" && (
            <label className="graph-option">
              <input id="graph-color-modules" name="graph-color-modules" type="checkbox" checked={colorModules}
                     onChange={(e) => setColorModules(e.target.checked)} />
              モジュールで色分け
            </label>
          )}
        </div>
      </div>

      <div className="graph-trail" aria-label="履歴">
        {shownTrail.length === 0 && <span className="graph-trail-hint">検索してノードを選ぶか、マップをダブルクリックして近傍を開きます。</span>}
        {shownTrail.map((t, i) => (
          <span key={t.search} className="graph-trail-item">
            {i > 0 && <span className="graph-trail-sep">›</span>}
            <button
              type="button"
              className={`graph-trail-btn${params.toString() === t.search ? " active" : ""}`}
              onClick={() => setParams(new URLSearchParams(t.search))}
            >
              {t.label}
            </button>
          </span>
        ))}
        {pathStart && (
          <span className="graph-path-start">
            経路の始点: {pathStart.label}
            <button type="button" className="graph-icon-btn" aria-label="始点を外す"
                    onClick={() => { setPathStart(null); setPathIds([]); }}>
              ×
            </button>
          </span>
        )}
      </div>

      <div className="graph-body">
        <div className="graph-stage">
          <GraphCanvas
            ref={canvasRef}
            graph={graph}
            layout={LAYOUTS[mode]}
            hiddenTables={hiddenTables}
            hideTests={hideTests}
            hideGenerated={hideGenerated}
            prune={mode === "focus"}
            selectedId={selectedNode?.id || null}
            pathIds={pathIds}
            onSelect={setSelectedId}
            onExpand={expand}
            onHover={(data) => hoverSetter.current?.(data)}
          />

          <div className="graph-controls">
            <button type="button" className="graph-icon-btn" onClick={() => canvasRef.current?.zoom(1.25)} aria-label="拡大" title="拡大">+</button>
            <button type="button" className="graph-icon-btn" onClick={() => canvasRef.current?.zoom(0.8)} aria-label="縮小" title="縮小">−</button>
            <button type="button" className="graph-btn small" onClick={() => canvasRef.current?.fit()}>全体表示</button>
            <button type="button" className="graph-btn small" onClick={() => canvasRef.current?.relayout()}>再配置</button>
            <button type="button" className="graph-btn small" onClick={exportPng}>PNG 保存</button>
          </div>

          {loading && <div className="graph-overlay"><div className="loading-dots"><span /><span /><span /></div></div>}
          {viewError && <div className="graph-overlay message"><p>{viewError}</p></div>}
          {topicsMissing && (
            <div className="graph-overlay message">
              <p>このリポジトリには Topics の行がありません。トピックは codedoc_go では --wiki、codedoc_go_leiden では --graph が作ります。</p>
            </div>
          )}

          {graph && kindsShown.length > 0 && (
            <div className={`graph-legend${legendOpen ? "" : " closed"}`}>
              <button type="button" className="graph-legend-head" onClick={() => setLegendOpen((o) => !o)}>
                凡例 {legendOpen ? "▾" : "▸"}
              </button>
              {legendOpen && (
                <div className="graph-legend-body">
                  {view.modules.length === 0 && kindsShown.map((kind) => {
                    const k = kindInfo(kind);
                    return (
                      <div key={kind} className="graph-legend-row">
                        <span className={`graph-swatch shape-${k.shape}${k.hollow ? " hollow" : ""}`}
                              style={{ "--swatch": k.color }} />
                        {k.label}
                      </div>
                    );
                  })}
                  {view.modules.length > 0 && <div className="graph-legend-row note">パッケージの色はモジュール</div>}
                  {view.modules.map(([label, color]) => (
                    <div key={label} className="graph-legend-row">
                      <span className="graph-swatch shape-round-hexagon" style={{ "--swatch": color }} />
                      <span className="mono">{label}</span>
                    </div>
                  ))}
                  {chipTables.filter((t) => edgeCounts[t] && !hiddenTables.has(t)).map((t) => (
                    <div key={t} className="graph-legend-row">
                      <span className={`graph-line line-${tableInfo(t).style}${tableInfo(t).dim ? " dim" : ""}`}
                            style={{ "--line": tableInfo(t).color }} />
                      {EDGE_TABLES[t]?.hint || t}
                    </div>
                  ))}
                  {mode === "topics" && graph.topicMode === "levels" && (
                    <>
                      <div className="graph-legend-row note">円グラフはメンバーの役割（ClassBelongsToTopic）</div>
                      {ROLE_SLICES.map(([role, color]) => (
                        <div key={role} className="graph-legend-row">
                          <span className="graph-swatch shape-ellipse" style={{ "--swatch": color }} />
                          {role === "other" ? "未分類ほか" : ROLE_LABELS[role]}
                        </div>
                      ))}
                      <div className="graph-legend-row note">枠は上位トピック、中はサブトピック。「未分類」は右に離して表示。</div>
                    </>
                  )}
                </div>
              )}
            </div>
          )}

          <StatusLine register={registerHover}>
            {graph && <span>ノード {graph.nodes.length} ・ エッジ {graph.edges.length}</span>}
            {shownNotice && <span className="graph-status-notice">{shownNotice}</span>}
            {!shownNotice && graph?.nodes.length > 0 && <span className="graph-status-hint">クリックで詳細、ダブルクリックで展開</span>}
          </StatusLine>
        </div>

        {selectedNode && (
          <GraphDetails
            node={selectedNode}
            details={shownDetails.data}
            error={shownDetails.error}
            loading={Boolean(detailId) && !shownDetails.data && !shownDetails.error}
            hiddenTables={hiddenTables}
            pathStart={mode === "focus" ? pathStart : null}
            onClose={() => setSelectedId(null)}
            onFocus={focus}
            onExpand={expand}
            onPathStart={(node) => setPathStart(node)}
            onPathTo={findPath}
            onTree={mode === "directories" ? showTree : null}
          />
        )}
      </div>

      {chipTables.length > 0 && (
        <div className="graph-chips" aria-label="エッジの表">
          {chipTables.map((t) => {
            const info = tableInfo(t);
            const off = hiddenTables.has(t);
            return (
              <button
                type="button"
                key={t}
                className={`graph-chip${off ? " off" : ""}`}
                aria-pressed={!off}
                onClick={() => toggleTable(t)}
                title={off ? `${t} を表示する` : `${t} を隠す`}
              >
                <span className={`graph-line line-${info.style}${info.dim ? " dim" : ""}`} style={{ "--line": info.color }} />
                <span className="graph-chip-name">{t}</span>
                {info.hint && <span className="graph-chip-hint">{info.hint}</span>}
                <span className="graph-chip-count">{edgeCounts[t] || 0}</span>
              </button>
            );
          })}
        </div>
      )}
    </main>
  );
}
