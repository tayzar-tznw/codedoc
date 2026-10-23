"""MCP server configuration — same pattern as graph_generator/config.py.

All env vars come from the single root .env; importing this module loads it
(before any Google imports in runner.py). dotenv never overrides already-set
vars, so real env vars (e.g. Cloud Run) still win.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


HOST = _env("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8080)
