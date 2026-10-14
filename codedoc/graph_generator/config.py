"""Central configuration — all settings loaded from .env with sensible defaults."""

import os

from dotenv import load_dotenv
load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


# ── Google Cloud ──────────────────────────────────────────────
GCP_PROJECT = _env("GOOGLE_CLOUD_PROJECT", "claude-cws-498905")
GCP_REGION = _env("GOOGLE_CLOUD_LOCATION", "global")

# ── Gemini ────────────────────────────────────────────────────
MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_CONCURRENCY = _env_int("GEMINI_CONCURRENCY", 100)
# Output token cap. Topic-summary docs (Mermaid + tables) are large, and Gemini-3
# thinking also draws from this budget — too low yields empty responses. It's a
# cap (you only pay for tokens actually produced), so keep it generous.
MAX_OUTPUT_TOKENS = _env_int("MAX_OUTPUT_TOKENS", 8192)

# ── Spanner Graph ─────────────────────────────────────────────
SPANNER_INSTANCE = _env("SPANNER_INSTANCE", "codedoc-instance")
SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-db")
GRAPH_NAME = _env("GRAPH_NAME", "code_graph_a")
ID_PREFIX = _env("ID_PREFIX", "a")
SPANNER_BATCH_SIZE = 5000

# ── Embeddings ────────────────────────────────────────────────
EMBED_MODEL = "text-embedding-005"
EMBED_BATCH_SIZE = 20
EMBED_CONCURRENCY = _env_int("EMBED_CONCURRENCY", 20)

# ── Output ────────────────────────────────────────────────────
OUTPUT_DIR = _env("OUTPUT_DIR", "output_docs_pipeline")

# ── File scanning ─────────────────────────────────────────────
SOURCE_EXTENSIONS = {
    ".java", ".py", ".ts", ".js", ".tsx", ".jsx",
    ".go", ".rs", ".cpp", ".c", ".h", ".hpp",
    ".rb", ".rake", ".gemspec", ".kt", ".kts", ".scala", ".swift", ".cs",
    ".php", ".lua", ".dart", ".ex", ".exs",
    ".razor", ".cshtml", ".fs", ".fsx", ".vb",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".idea", ".vscode", "build", "dist", "target",
    "bin", "obj", ".gradle", ".mvn", "venv", ".venv",
    "Migrations", "wwwroot", "Properties",
}

# ── Database schema detection ─────────────────────────────────
# When a codebase contains DB schema/migration files, the pipeline emits a
# dedicated "Database Schema" category (topic) with an ER-diagram doc.
#
# This is a list of detectors so support for a new framework is purely additive
# — add one dict, no pipeline code change. A file matches a detector if:
#   - its basename is in `file_names`, OR
#   - its path contains any substring in `dir_tokens`, OR
#   - (optional) its head contains any substring in `content_patterns`
#     (this reads the file, so scope it — pair with a narrow dir_token).
# Note: a file is only considered if it was scanned (its extension is in
# SOURCE_EXTENSIONS). `.sql` is not scanned by default, so `structure.sql` only
# matches once `.sql` is added to SOURCE_EXTENSIONS.
DB_SCHEMA_DETECTORS = [
    {
        "framework": "Rails (Active Record)",
        "file_names": {"schema.rb", "structure.sql"},
        "dir_tokens": {"/db/migrate/"},
    },
    # Future frameworks (templates — uncomment / adapt to enable):
    # {"framework": "Prisma",
    #  "file_names": {"schema.prisma"},
    #  "dir_tokens": {"/prisma/migrations/"}},
    # {"framework": "Django",
    #  "dir_tokens": {"/migrations/"},
    #  "content_patterns": {"migrations.Migration"}},
    # {"framework": "SQL DDL",   # requires ".sql" in SOURCE_EXTENSIONS
    #  "file_names": {"structure.sql", "schema.sql"},
    #  "content_patterns": {"CREATE TABLE"}},
]
