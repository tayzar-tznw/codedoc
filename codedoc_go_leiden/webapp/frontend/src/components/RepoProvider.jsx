import { useEffect, useState } from "react";
import { RepoContext } from "../repo";

const STORAGE_KEY = "codedoc.repo";

function storedRepo() {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export default function RepoProvider({ children }) {
  const [state, setState] = useState({ ready: false, repos: [], current: null, selected: null });

  useEffect(() => {
    fetch("/api/repos")
      .then((r) => (r.ok ? r.json() : { repos: [], current: null }))
      .catch(() => ({ repos: [], current: null }))
      .then(({ repos, current }) => {
        const stored = storedRepo();
        const selected = current ? null : repos.includes(stored) ? stored : (repos[0] ?? null);
        setState({ ready: true, repos, current, selected });
      });
  }, []);

  const select = (name) => {
    try {
      localStorage.setItem(STORAGE_KEY, name);
    } catch {
      // the choice then lasts for this page only
    }
    setState((s) => ({ ...s, selected: name }));
  };

  return <RepoContext.Provider value={{ ...state, select }}>{children}</RepoContext.Provider>;
}
