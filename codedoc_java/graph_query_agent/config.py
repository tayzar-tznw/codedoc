"""Package configuration — same pattern as graph_generator/config.py.

All env vars come from the single root .env; importing this module loads it.
dotenv never overrides already-set vars, so real env vars (e.g. Cloud Run)
still win.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")

# Spanner Graph config
SPANNER_INSTANCE = _env("SPANNER_INSTANCE", "codedoc")
SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-java")
GRAPH_NAME = _env("GRAPH_NAME", "code_graph_a")
PROJECT_ID = _env("GOOGLE_CLOUD_PROJECT", "development-459201")
