import os
import sys
import json
import uuid
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# Load the single root .env before any Google imports, so the agent's Vertex AI
# settings (GOOGLE_GENAI_USE_VERTEXAI=true, project, location) and Spanner graph
# config are present. dotenv does not override already-set vars.
_project_root = Path(__file__).resolve().parent.parent
if os.environ.get("CODEDOC_DOTENV", "1") != "0":      # 0: skip it (the test suite)
    load_dotenv(_project_root / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import markdown

# Add project root to path so we can import graph_query_agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from graph_query_agent.agent import root_agent
from webapp.graph_api import make_router as graph_router

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# The pipeline writes each repo's wiki under OUTPUT_DIR/repos/<repo>/ (see
# graph_generator/config.py; kept inline rather than importing that package so
# the webapp container stays self-contained). .env is loaded above. A request
# reads the repo named by ?repo=, else WEBAPP_REPO, else the only repo there.
OUTPUT_ROOT = Path(__file__).resolve().parent.parent / os.environ.get("OUTPUT_DIR", "output_docs_pipeline")
APP_NAME = "codedoc"
SUMMARY_KINDS = ("files", "dirs", "packages")
# Name of the repo root in summaries/dirs.json listings: its key ("" or ".") cannot
# be routed, while no repo-relative directory starts with "/".
ROOT_DIR = "/"
ROOT_DIR_KEYS = ("", ".")

# ---------------------------------------------------------------------------
# ADK Runner setup
# ---------------------------------------------------------------------------
session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="CodeDoc Viewer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Markdown rendering helper
# ---------------------------------------------------------------------------
md = markdown.Markdown(extensions=["tables", "fenced_code", "codehilite", "toc"])


def render_md(text: str) -> str:
    md.reset()
    return md.convert(text)


# ---------------------------------------------------------------------------
# Repo selection and the wiki files of one repo (contract C7)
# ---------------------------------------------------------------------------
def _repos() -> list[str]:
    base = OUTPUT_ROOT / "repos"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def _default_repo(repos: list[str]) -> str | None:
    env = os.environ.get("WEBAPP_REPO", "").strip()
    if env:
        return env
    return repos[0] if len(repos) == 1 else None


def _repo_dir(repo: str | None) -> Path:
    """OUTPUT_DIR/repos/<repo>/ for ?repo=, WEBAPP_REPO or the only repo; else a 404 listing them."""
    repos = _repos()
    if not repos:
        raise HTTPException(404, {
            "error": f"no repo under {OUTPUT_ROOT / 'repos'}", "repos": repos,
            "hint": "run `python -m graph_generator analyze <dir> --all` first"})
    name = repo or _default_repo(repos)
    if name not in repos:
        raise HTTPException(404, {
            "error": f"repo {name!r} not found" if name else "no repo selected",
            "repos": repos, "hint": "set WEBAPP_REPO or pass ?repo=<name>"})
    return OUTPUT_ROOT / "repos" / name


def _inside(base: Path, rel: str) -> Path | None:
    """base/rel when it stays inside base (path-traversal guard)."""
    path = (base / rel).resolve()
    return path if path.is_relative_to(base.resolve()) else None


def _read_json(path: Path, missing):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else missing


def _topic_tree(root: Path) -> list[dict]:
    tree = _read_json(root / "topics" / "topic_tree.json", None)
    if tree is None:
        raise HTTPException(404, "topic_tree.json not found")
    return tree


def _file_summaries(root: Path) -> list[str]:
    """Relpaths that have a summaries/nodes/<relpath>.json cache."""
    base = root / "summaries" / "nodes"
    if not base.is_dir():
        return []
    return sorted(p.relative_to(base).as_posix().removesuffix(".json") for p in base.rglob("*.json"))


def _file_markdown(relpath: str, cache: dict) -> str:
    # Qualnames go in code spans: `<init>` and `<package>` must not read as HTML.
    parts = [f"# `{relpath}`", "", cache.get("file") or ""]
    for qualname, text in (cache.get("nodes") or {}).items():
        parts += ["", f"## `{qualname}`", "", text]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Documentation API
# ---------------------------------------------------------------------------
@app.get("/api/repos")
async def get_repos():
    repos = _repos()
    current = _default_repo(repos)
    return {"repos": repos, "current": current if current in repos else None}


@app.get("/api/docs/index")
async def get_index(repo: str | None = None):
    index_path = _repo_dir(repo) / "index.md"
    if not index_path.exists():
        raise HTTPException(404, "index.md not found")
    raw = index_path.read_text(encoding="utf-8")
    return {"markdown": raw, "html": render_md(raw)}


@app.get("/api/docs/metadata")
async def get_metadata(repo: str | None = None):
    meta_path = _repo_dir(repo) / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "metadata.json not found")
    return json.loads(meta_path.read_text(encoding="utf-8"))


@app.get("/api/docs/topics")
async def get_topics(repo: str | None = None):
    return _topic_tree(_repo_dir(repo))


@app.get("/api/docs/topics/{topic_name}")
async def get_topic(topic_name: str, repo: str | None = None):
    """A topic page, topics/<id>.md, found by the topic's name (or its id) in topic_tree.json."""
    root = _repo_dir(repo)
    tree = _topic_tree(root)
    topic = next((t for t in tree if t.get("name") == topic_name), None) \
        or next((t for t in tree if t.get("id") == topic_name), None)
    page = _inside(root / "topics", f"{topic['id']}.md") if topic and topic.get("id") else None
    if page is None or not page.is_file():
        raise HTTPException(404, f"Topic '{topic_name}' not found")
    raw = page.read_text(encoding="utf-8")
    return {"name": topic.get("name"), "id": topic["id"], "markdown": raw, "html": render_md(raw)}


@app.get("/api/docs/summaries/{kind}/{name:path}")
async def get_summary(kind: str, name: str, repo: str | None = None):
    """files: summaries/nodes/<relpath>.json; dirs and packages: their summaries/<kind>.json
    entry (ROOT_DIR, "." or "" names the repo root directory)."""
    if kind not in SUMMARY_KINDS:
        raise HTTPException(400, "kind must be 'files', 'dirs' or 'packages'")
    root = _repo_dir(repo)
    if kind == "files":
        path = _inside(root / "summaries" / "nodes", f"{name}.json")
        cache = _read_json(path, None) if path else None
        if cache is None:
            raise HTTPException(404, f"Summary '{name}' not found")
        raw = _file_markdown(name, cache)
    else:
        entries = _read_json(root / "summaries" / f"{kind}.json", {})
        if kind == "dirs" and name in (ROOT_DIR, *ROOT_DIR_KEYS):
            name = ROOT_DIR
            entry = next((entries[k] for k in ROOT_DIR_KEYS if k in entries), None)
        else:
            entry = entries.get(name)
        if entry is None:
            raise HTTPException(404, f"Summary '{name}' not found")
        raw = f"# `{name}`\n\n{entry.get('summary', '')}\n"
    return {"name": name, "kind": kind, "markdown": raw, "html": render_md(raw)}


@app.get("/api/docs/tree")
async def get_doc_tree(repo: str | None = None):
    """Return a structured tree of all available docs."""
    root = _repo_dir(repo)
    tree = _read_json(root / "topics" / "topic_tree.json", [])
    return {"topics": [t["name"] for t in tree if t.get("name")],
            "summaries": {
                "files": _file_summaries(root),
                "dirs": sorted(ROOT_DIR if key in ROOT_DIR_KEYS else key
                               for key in _read_json(root / "summaries" / "dirs.json", {})),
                "packages": sorted(_read_json(root / "summaries" / "packages.json", {}))}}


# ---------------------------------------------------------------------------
# Graph explorer API (spec R51): /api/graph/*, read from graph_rows/*.jsonl
# ---------------------------------------------------------------------------
app.include_router(graph_router(_repo_dir))


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    user_id = "web_user"

    # Ensure session exists
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if session is None:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

    user_content = genai_types.Content(
        role="user", parts=[genai_types.Part(text=req.message)]
    )

    final_text = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=user_content
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text

    return {"response": final_text, "session_id": session_id}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    user_id = "web_user"

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if session is None:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

    user_content = genai_types.Content(
        role="user", parts=[genai_types.Part(text=req.message)]
    )

    async def event_generator():
        # Send session_id first
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        payload = {
                            "type": "delta" if event.partial else "text",
                            "text": part.text,
                            "author": event.author,
                            "is_final": event.is_final_response(),
                        }
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Serve React build (production) with SPA history-fallback
# ---------------------------------------------------------------------------
# The frontend uses react-router BrowserRouter (clean URLs like /topic/:name,
# /chat). Hitting such a path directly or refreshing sends the request to
# FastAPI, which has no matching route. A plain StaticFiles mount would 404
# ({"detail":"Not Found"}); instead we serve real build files when they exist
# and fall back to index.html so the client-side router can take over.
frontend_build = (Path(__file__).resolve().parent / "frontend" / "dist").resolve()
if frontend_build.exists():
    # Hashed JS/CSS bundles (StaticFiles has built-in path-traversal protection).
    app.mount("/assets", StaticFiles(directory=str(frontend_build / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # The real /api/* routes are declared above and take precedence; this
        # guard only catches undefined /api/* paths, which must stay JSON 404s
        # rather than silently returning the SPA shell.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        # Serve a real top-level build file (favicon.svg, icons.svg, …) when it
        # exists and stays inside the build dir (path-traversal guard).
        candidate = (frontend_build / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(frontend_build):
            return FileResponse(candidate)
        # Otherwise hand the SPA shell back so react-router resolves the route.
        return FileResponse(frontend_build / "index.html")
