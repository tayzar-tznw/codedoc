import { createContext, useContext, useEffect, useState } from "react";

// Which repo's wiki the docs API serves. The server picks one (WEBAPP_REPO, or
// the only repo under OUTPUT_DIR/repos) and reports it as `current` from
// /api/repos. When `current` is null the user picks a repo (`selected`) and
// every docs request carries ?repo=<selected>.
export const RepoContext = createContext({
  ready: false,
  repos: [],
  current: null,
  selected: null,
  select: () => {},
});

export function useRepo() {
  return useContext(RepoContext);
}

// `url` with ?repo=<selected> when a repo was picked in the UI.
export function withRepo(url, selected) {
  if (!selected) return url;
  return `${url}${url.includes("?") ? "&" : "?"}repo=${encodeURIComponent(selected)}`;
}

// A readable message from a FastAPI error body: {"detail": "..."} or
// {"detail": {"error", "repos", "hint"}}.
function errorMessage(body, status) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (detail && detail.error) return detail.hint ? `${detail.error}: ${detail.hint}` : detail.error;
  return `HTTP ${status}`;
}

// The JSON at `url`, or an Error carrying the server's message.
export async function fetchJson(url) {
  const r = await fetch(url);
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error(errorMessage(body, r.status));
  return body;
}

// The JSON at `url`: {data, error, loading}. A null url waits (loading).
// Results are kept per url, so a new url shows loading without resetting
// state inside the effect.
export function useJson(url) {
  const [result, setResult] = useState({ url: null, data: null, error: null });

  useEffect(() => {
    if (!url) return undefined;
    let live = true;
    fetch(url)
      .then(async (r) => {
        const body = await r.json().catch(() => null);
        if (!r.ok) throw new Error(errorMessage(body, r.status));
        return body;
      })
      .then((data) => {
        if (live) setResult({ url, data, error: null });
      })
      .catch((e) => {
        if (live) setResult({ url, data: null, error: e.message });
      });
    return () => {
      live = false;
    };
  }, [url]);

  if (!url || result.url !== url) return { data: null, error: null, loading: true };
  return { data: result.data, error: result.error, loading: false };
}
