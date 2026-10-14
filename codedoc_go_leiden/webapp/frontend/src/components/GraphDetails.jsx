import { Link } from "react-router-dom";
import MarkdownView from "./MarkdownView";
import { kindInfo, ORIGIN_LABELS, relationLabel, ROLE_LABELS, tableInfo } from "../graphStyle";

const TOPIC_FIELDS = [
  ["level", "レベル"],
  ["size", "メンバー数"],
  ["file_count", "ファイル数"],
  ["cohesion", "凝集度"],
  ["conductance", "コンダクタンス"],
  ["label_source", "名前の出所"],
];

// Summaries are Markdown; a topic's is its whole page, so the panel shows its
// first part without diagrams and links to the page.
function summaryText(text, isTopic) {
  const plain = text.replace(/```mermaid[\s\S]*?```/g, "").trim();
  if (!isTopic || plain.length <= 1200) return plain;
  const cut = plain.lastIndexOf("\n\n", 1200);
  return `${plain.slice(0, cut > 400 ? cut : 1200)}\n\n…`;
}

function value(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3);
  return String(v);
}

function Field({ label, children, mono }) {
  if (children === null || children === undefined || children === "") return null;
  return (
    <>
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined}>{children}</dd>
    </>
  );
}

function docLink(link) {
  if (link.type === "summary") return `/summary/${link.kind}/${encodeURIComponent(link.name)}`;
  return `/topic/${encodeURIComponent(link.name)}`;
}

// The side panel for the selected node: what it is, where it is, its summary,
// its relations per edge table (each expandable) and the docs pages for it.
export default function GraphDetails({ node, details, error, loading, hiddenTables, pathStart, onClose,
                                       onFocus, onExpand, onPathStart, onPathTo }) {
  const k = kindInfo(node.kind);
  const columns = details?.columns || {};
  const isTopic = node.table === "Topics";
  const location = details?.location;
  const where = location
    ? `${location.path}${location.start_line ? `:${location.start_line}` : ""}${
        location.end_line && location.end_line !== location.start_line ? `–${location.end_line}` : ""
      }`
    : null;
  const roles = details?.roles && Object.entries(details.roles);

  return (
    <aside className="graph-details" aria-label="ノードの詳細">
      <div className="graph-details-head">
        <span className="graph-kind-dot" style={{ background: k.color }} />
        <span className="graph-details-kind">{k.label}</span>
        <span className="graph-details-table">{node.table}</span>
        <button type="button" className="graph-icon-btn" onClick={onClose} aria-label="詳細を閉じる">
          ×
        </button>
      </div>
      <h3 className="graph-details-title">{details?.node?.label || node.label}</h3>
      {node.sub && node.sub !== node.label && <div className="graph-details-sub">{node.sub}</div>}

      {!node.virtual && (
        <div className="graph-details-actions">
          <button type="button" className="graph-btn primary" onClick={() => onFocus(node.id, details?.node?.label)}>
            中心にする
          </button>
          <button type="button" className="graph-btn" onClick={() => onExpand(node.id)}>
            近傍を展開
          </button>
          {pathStart && pathStart.id !== node.id ? (
            <button type="button" className="graph-btn" onClick={() => onPathTo(node)} title={`${pathStart.label} からの最短経路`}>
              ここまでの経路
            </button>
          ) : (
            <button type="button" className="graph-btn" onClick={() => onPathStart(node)}>
              経路の始点にする
            </button>
          )}
        </div>
      )}

      {node.virtual && (
        <p className="graph-note">
          このディレクトリには graph_rows のノードがありません。直下のファイル {node.files ?? 0} 件、配下の合計 {node.total ?? 0} 件。
        </p>
      )}

      {loading && <p className="graph-note">読み込み中…</p>}
      {error && <p className="graph-note error">{error}</p>}

      {details && (
        <>
          <dl className="graph-fields">
            <Field label="場所" mono>{where}</Field>
            <Field label="パッケージ" mono>{node.table === "Classes" || node.table === "Methods" ? node.sub : null}</Field>
            <Field label="モジュール" mono>{details.module}</Field>
            <Field label="シグネチャ" mono>{columns.signature}</Field>
            <Field label="由来">{columns.origin ? ORIGIN_LABELS[columns.origin] || columns.origin : null}</Field>
            {isTopic && TOPIC_FIELDS.map(([key, label]) =>
              key in columns ? <Field key={key} label={label}>{value(columns[key])}</Field> : null)}
            <Field label="印">
              {node.test || node.generated || details.unassigned ? (
                <span className="graph-badges">
                  {node.test && <span className="graph-badge">テスト</span>}
                  {node.generated && <span className="graph-badge">生成コード</span>}
                  {details.unassigned && <span className="graph-badge">未分類</span>}
                </span>
              ) : null}
            </Field>
          </dl>

          {(details.links || []).length > 0 && (
            <div className="graph-links">
              {details.links.map((link) => (
                <Link key={`${link.type}:${link.name}`} to={docLink(link)} className="graph-link">
                  {link.type === "summary" ? "要約ページ" : `トピック: ${link.label}`}
                </Link>
              ))}
            </div>
          )}

          <section className="graph-section">
            <h4>
              要約
              {details.summary_source && (
                <span className="graph-section-note">
                  {details.summary_source === "wiki" ? "Wiki の要約" : "graph_rows の summary 列"}
                </span>
              )}
            </h4>
            {details.summary ? (
              <div className="graph-summary">
                <MarkdownView content={summaryText(details.summary, isTopic)} />
              </div>
            ) : (
              <p className="graph-note">要約はまだありません（--wiki で作られます）。</p>
            )}
          </section>

          {details.doc && (
            <section className="graph-section">
              <h4>ドキュメントコメント</h4>
              <pre className="graph-doc">{details.doc}</pre>
            </section>
          )}

          {roles && roles.length > 0 && (
            <section className="graph-section">
              <h4>メンバーの役割（ClassBelongsToTopic）</h4>
              <table className="graph-table">
                <tbody>
                  {roles.map(([role, count]) => (
                    <tr key={role}>
                      <td>{ROLE_LABELS[role] || role}</td>
                      <td className="num">{count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {details.edges.length > 0 && (
            <section className="graph-section">
              <h4>関係</h4>
              <table className="graph-table">
                <tbody>
                  {details.edges.map((e) => {
                    const t = tableInfo(e.table);
                    return (
                      <tr key={`${e.table}:${e.direction}`} className={hiddenTables.has(e.table) ? "muted" : undefined}>
                        <td>
                          <span className="graph-line-dot" style={{ background: t.color }} />
                          {relationLabel(e.table, e.direction)}
                          <span className="graph-table-name">{e.table}</span>
                        </td>
                        <td className="num">{e.count}</td>
                        <td>
                          <button
                            type="button"
                            className="graph-btn small"
                            onClick={() => onExpand(node.id, { table: e.table, direction: e.direction })}
                          >
                            展開
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </section>
          )}

          {Object.keys(details.refs || {}).length > 0 && (
            <section className="graph-section">
              <h4>参照</h4>
              <ul className="graph-refs">
                {Object.entries(details.refs).map(([column, ref]) => (
                  <li key={column}>
                    <span className="graph-table-name">{column}</span>
                    <button type="button" className="graph-ref" onClick={() => onFocus(ref.id, ref.label)}>
                      <span className="graph-kind-dot" style={{ background: kindInfo(ref.kind).color }} />
                      {ref.label}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <details className="graph-section graph-columns">
            <summary>全ての列</summary>
            <table className="graph-table">
              <tbody>
                {Object.entries(columns).map(([key, v]) => (
                  <tr key={key}>
                    <td className="mono">{key}</td>
                    <td className="mono wrap">{value(v)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </aside>
  );
}
