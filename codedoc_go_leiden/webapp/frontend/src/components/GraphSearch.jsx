import { useEffect, useRef, useState } from "react";
import { fetchJson, withRepo } from "../repo";
import { kindInfo } from "../graphStyle";

const KIND_FILTERS = [
  ["", "すべて"],
  ["Methods", "関数・メソッド"],
  ["Classes", "型"],
  ["Files", "ファイル"],
  ["Packages", "パッケージ"],
  ["Directories", "ディレクトリ"],
  ["Topics", "トピック"],
];

// Search box over /api/graph/search: names, qualnames, package-qualified names,
// paths and import paths. Enter or a click picks a result.
export default function GraphSearch({ selected, hideTests, hideGenerated, nodeTables, onPick }) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [result, setResult] = useState({ key: "", items: [], total: 0, error: null });
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const boxRef = useRef(null);
  const trimmed = query.trim();
  const key = `${trimmed}\u0000${kind}\u0000${hideTests}\u0000${hideGenerated}\u0000${selected ?? ""}`;

  useEffect(() => {
    if (!trimmed) return undefined;
    let live = true;
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ q: trimmed, limit: "50" });
      if (kind) params.set("kind", kind);
      if (hideTests) params.set("hide_tests", "true");
      if (hideGenerated) params.set("hide_generated", "true");
      fetchJson(withRepo(`/api/graph/search?${params}`, selected))
        .then((body) => live && setResult({ key, items: body.results, total: body.total, error: null }))
        .catch((e) => live && setResult({ key, items: [], total: 0, error: e.message }));
    }, 180);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [key, trimmed, kind, hideTests, hideGenerated, selected]);

  useEffect(() => {
    const close = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const current = result.key === key ? result : { items: [], total: 0, error: null, pending: Boolean(trimmed) };
  const items = trimmed ? current.items : [];

  const pick = (item) => {
    setOpen(false);
    setQuery(item.label);
    onPick(item);
  };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      if (items[active]) pick(items[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div className="graph-search" ref={boxRef}>
      <input
        id="graph-search"
        name="graph-search"
        type="search"
        className="graph-search-input"
        placeholder="名前・qualname・パスで検索（例: FeeCalculator.Calculate）"
        aria-label="グラフのノードを検索"
        autoComplete="off"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />
      <select
        id="graph-search-kind"
        name="graph-search-kind"
        className="graph-select"
        aria-label="検索する種類"
        value={kind}
        onChange={(e) => setKind(e.target.value)}
      >
        {KIND_FILTERS.filter(([t]) => !t || !nodeTables || nodeTables.includes(t)).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
      {open && trimmed && (
        <div className="graph-search-results" role="listbox">
          {current.error && <div className="graph-search-note error">{current.error}</div>}
          {!current.error && current.pending && <div className="graph-search-note">検索中…</div>}
          {!current.error && !current.pending && items.length === 0 && (
            <div className="graph-search-note">見つかりません</div>
          )}
          {items.map((item, i) => {
            const k = kindInfo(item.kind);
            return (
              <button
                type="button"
                key={item.id}
                role="option"
                aria-selected={i === active}
                className={`graph-search-item${i === active ? " active" : ""}`}
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(item)}
              >
                <span className="graph-kind-dot" style={{ background: k.color }} />
                <span className="graph-search-label">{item.label}</span>
                <span className="graph-search-kind">{k.label}</span>
                <span className="graph-search-sub">
                  {item.path ? `${item.path}${item.line ? `:${item.line}` : ""}` : item.sub}
                </span>
                {item.test && <span className="graph-badge">テスト</span>}
                {item.generated && <span className="graph-badge">生成</span>}
              </button>
            );
          })}
          {current.total > items.length && (
            <div className="graph-search-note">
              {current.total} 件中 {items.length} 件を表示。語を足して絞り込んでください。
            </div>
          )}
        </div>
      )}
    </div>
  );
}
