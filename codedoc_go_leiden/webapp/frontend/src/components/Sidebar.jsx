import { NavLink, Link, useNavigate } from "react-router-dom";
import { useJson, useRepo, withRepo } from "../repo";

function packageLabel(key) {
  // summaries/packages.json keys are "<dir>|<package name>".
  const [dir, name] = key.split("|");
  const shown = dir || "/";
  return name && name !== shown.split("/").pop() ? `${shown} (${name})` : shown;
}

function Section({ title, icon, items, to, label = (x) => x }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="nav-section">
      <div className="nav-section-title">{title}</div>
      {items.map((item) => (
        <NavLink
          key={item}
          to={to(item)}
          className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
        >
          <span className="icon">{icon}</span>
          <span className="label">{label(item)}</span>
        </NavLink>
      ))}
    </div>
  );
}

export default function Sidebar() {
  const { ready, repos, current, selected, select } = useRepo();
  const navigate = useNavigate();
  const { data: tree, error } = useJson(ready ? withRepo("/api/docs/tree", selected) : null);
  const summaries = tree?.summaries ?? {};
  const summaryLink = (kind) => (name) => `/summary/${kind}/${encodeURIComponent(name)}`;

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <h1>CodeDoc</h1>
        <p>Auto-generated documentation</p>
        {ready && current === null && repos.length > 0 && (
          <select
            id="repo-select"
            name="repo"
            className="repo-select"
            aria-label="Repository"
            value={selected ?? ""}
            onChange={(e) => {
              select(e.target.value);
              navigate("/");
            }}
          >
            {repos.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        )}
      </div>

      <nav className="sidebar-nav">
        <div className="nav-section">
          <div className="nav-section-title">Overview</div>
          <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
            <span className="icon">&#9776;</span>
            <span className="label">Index</span>
          </NavLink>
          <NavLink to="/graph" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
            <span className="icon">&#9673;</span>
            <span className="label">グラフ</span>
          </NavLink>
        </div>

        {error && <p className="sidebar-note">{error}</p>}

        <Section title="Modules" icon={<>&#9670;</>} items={tree?.topics}
                 to={(t) => `/topic/${encodeURIComponent(t)}`} />
        <Section title="File Summaries" icon={<>&#128196;</>} items={summaries.files}
                 to={summaryLink("files")} />
        <Section title="Directory Summaries" icon={<>&#128193;</>} items={summaries.dirs}
                 to={summaryLink("dirs")} />
        <Section title="Package Summaries" icon={<>&#128230;</>} items={summaries.packages}
                 to={summaryLink("packages")} label={packageLabel} />
      </nav>

      <div className="sidebar-footer">
        <Link to="/chat" className="chat-nav-btn">
          &#128172; Ask about this codebase
        </Link>
      </div>
    </aside>
  );
}
