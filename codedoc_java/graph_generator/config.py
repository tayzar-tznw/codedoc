"""Central configuration — all settings loaded from the single root .env with
sensible defaults."""

import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


# ── Google Cloud ──────────────────────────────────────────────
GCP_PROJECT = _env("GOOGLE_CLOUD_PROJECT", "development-459201")
GCP_REGION = _env("GOOGLE_CLOUD_LOCATION", "global")

# ── Gemini ────────────────────────────────────────────────────
MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_CONCURRENCY = _env_int("GEMINI_CONCURRENCY", 100)
# Output token cap. Topic-summary docs (Mermaid + tables) are large, and Gemini-3
# thinking also draws from this budget — too low yields empty responses. It's a
# cap (you only pay for tokens actually produced), so keep it generous.
MAX_OUTPUT_TOKENS = _env_int("MAX_OUTPUT_TOKENS", 8192)

# ── Spanner Graph ─────────────────────────────────────────────
SPANNER_INSTANCE = _env("SPANNER_INSTANCE", "codedoc")
SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-java")
GRAPH_NAME = _env("GRAPH_NAME", "code_graph_a")
ID_PREFIX = _env("ID_PREFIX", "a")
SPANNER_BATCH_SIZE = 5000

# ── Java LSP (Phase 1.6 semantic resolution) ─────────────────
# Eclipse JDT Language Server only (single server — no _SERVER selector).
JAVA_LSP_ENABLED = _env("JAVA_LSP_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Launcher script; the wrapper picks the JVM from JAVA_HOME (resolution pins 21).
JAVA_LSP_CMD = _env("JAVA_LSP_CMD", "jdtls")
JAVA_LSP_CONCURRENCY = _env_int("JAVA_LSP_CONCURRENCY", 4)
# Optional settings.xml handed to m2e (java.configuration.maven.userSettings).
# Lets a repo with an isolated local repository (e.g. the corpus _m2) keep
# jdtls off ~/.m2; empty = use the user's default Maven environment.
JAVA_LSP_MAVEN_SETTINGS = _env("JAVA_LSP_MAVEN_SETTINGS", "")
JAVA_LSP_TIMEOUT_S = _env_int("JAVA_LSP_TIMEOUT_S", 300)

# ── Embeddings ────────────────────────────────────────────────
EMBED_MODEL = "text-embedding-005"
EMBED_BATCH_SIZE = 20
EMBED_CONCURRENCY = _env_int("EMBED_CONCURRENCY", 20)

# ── Output ────────────────────────────────────────────────────
OUTPUT_DIR = _env("OUTPUT_DIR", "output_docs_pipeline")

# ── File scanning ─────────────────────────────────────────────
# Only .java is parsed/semantically resolved; the other JVM extensions are
# scanned for documentation (LLM) but never parsed — 「Java 解析、他 JVM 言語は
# スキャンのみ(将来拡張)」.
SOURCE_EXTENSIONS = {
    ".java", ".jsp", ".kt", ".groovy", ".scala", ".gradle",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg", ".idea", ".vscode",
    "build", "dist", "target", "out",
    ".gradle", ".mvn", "node_modules", "generated",
}
