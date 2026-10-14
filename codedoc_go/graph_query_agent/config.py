"""Package configuration — same pattern as graph_generator/config.py.

All env vars come from the single root .env; importing this module loads it.
There is no graph_query_agent/.env: under `adk run`, ADK loads the first .env
it finds walking up from this directory, so a nested file would shadow the
root one. dotenv never overrides already-set vars, so real env vars (e.g.
Cloud Run) still win.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if os.environ.get("CODEDOC_DOTENV", "1") != "0":      # 0: skip it (the test suite)
    load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")

# Spanner Graph config
SPANNER_INSTANCE = _env("SPANNER_INSTANCE", "codedoc-instance")
SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-go")
GRAPH_NAME = _env("GRAPH_NAME", "code_graph_a")
# No hardcoded project: when GOOGLE_CLOUD_PROJECT is unset (or empty), the
# Google client libraries use the ADC / gcloud default project.
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip() or None
