"""Webapp configuration — same pattern as graph_generator/config.py.

All env vars come from the single root .env; importing this module loads it
(before any Google imports in main.py). dotenv never overrides already-set
vars, so real env vars (e.g. Cloud Run) still win.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# Docs are read from the same OUTPUT_DIR the pipeline writes to (see
# graph_generator/config.py).
OUTPUT_DIR = _env("OUTPUT_DIR", "output_docs_pipeline")
DOCS_DIR = PROJECT_ROOT / OUTPUT_DIR
APP_NAME = "codedoc"
