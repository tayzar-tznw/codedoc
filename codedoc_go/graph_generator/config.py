"""Central configuration — all settings loaded from .env with sensible defaults."""

import os

from dotenv import load_dotenv

# CODEDOC_DOTENV=0 skips the root .env; the test suite sets it (tests/conftest.py)
# so that a user's .env never changes a test.
if os.environ.get("CODEDOC_DOTENV", "1") != "0":
    load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


def _env_opt(key: str) -> str | None:
    """An optional setting: unset and empty both mean None."""
    value = os.environ.get(key, "").strip()
    return value or None


def _env_flag(key: str) -> bool:
    """A 0/1 switch: only "1" turns it on."""
    return os.environ.get(key, "0").strip() == "1"


# ── Google Cloud ──────────────────────────────────────────────
# No default project: when GOOGLE_CLOUD_PROJECT is unset, the Google client
# libraries use the ADC / gcloud default project, and the commands print it.
GCP_PROJECT = _env_opt("GOOGLE_CLOUD_PROJECT")
GCP_REGION = _env("GOOGLE_CLOUD_LOCATION", "global")

# ── Gemini ────────────────────────────────────────────────────
MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_CONCURRENCY = _env_int("GEMINI_CONCURRENCY", 100)
# Seconds one Gemini request may take; a call past it is retried like a 5xx,
# within the same retry budget (R49). Legitimate topic-page calls with heavy
# thinking took up to 497 s on etcd (2026-10-14), so the default leaves room
# and still ends a hung request. 0 sets no limit.
GEMINI_TIMEOUT = _env_int("GEMINI_TIMEOUT", 600)
# Output token cap. Topic-summary docs (Mermaid + tables) are large, and Gemini-3
# thinking also draws from this budget. An answer cut at the cap is asked once
# more at four times it (R49). It's a cap (you only pay for tokens actually
# produced), so keep it generous.
MAX_OUTPUT_TOKENS = _env_int("MAX_OUTPUT_TOKENS", 8192)

# ── Spanner Graph ─────────────────────────────────────────────
SPANNER_INSTANCE = _env("SPANNER_INSTANCE", "codedoc-instance")
SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-go")
GRAPH_NAME = _env("GRAPH_NAME", "code_graph_a")
ID_PREFIX = _env("ID_PREFIX", "a")
SPANNER_BATCH_SIZE = 5000

# ── Embeddings ────────────────────────────────────────────────
EMBED_MODEL = "text-embedding-005"
EMBED_DIMENSION = None   # output size; None = the model's default. Part of the embedding cache key
EMBED_BATCH_SIZE = 20
EMBED_CONCURRENCY = _env_int("EMBED_CONCURRENCY", 20)

# ── Output ────────────────────────────────────────────────────
OUTPUT_DIR = _env("OUTPUT_DIR", "output_docs_pipeline")

# Interface candidates and ambiguous targets become PossiblyCalls edges only
# when the candidate count stays at or below this cap.
POSSIBLY_CALLS_MAX_CANDIDATES = _env_int("POSSIBLY_CALLS_MAX_CANDIDATES", 5)

# ── Go toolchain and build configuration (spec §2.5, §3.3, §3.4) ──
# Path to a go binary. Unset: codedoc_go/.tools/go/bin/go when `setup go`
# installed it, else `go` on PATH.
GO_TOOLCHAIN = _env_opt("GO_TOOLCHAIN")
GO_BUILD_GOOS = _env("GO_BUILD_GOOS", "linux")
GO_BUILD_GOARCH = _env("GO_BUILD_GOARCH", "amd64")
GO_BUILD_TAGS = _env("GO_BUILD_TAGS", "")          # comma-separated build tags
GO_BUILD_CGO = _env_flag("GO_BUILD_CGO")           # True lets go run gcc and pkg-config
# Extra configurations for files the primary build excludes:
# "goos/goarch[+tag+tag]" entries separated by spaces.
GO_EXTRA_BUILDS = _env("GO_EXTRA_BUILDS", "")

# ── Module sources (spec §3.7) ────────────────────────────────
GO_MODULES_DOWNLOAD = _env_flag("GO_MODULES_DOWNLOAD")   # True = --download-modules
GO_PROXY = _env("GO_PROXY", "https://proxy.golang.org")  # download mode only, no ",direct"
GO_SUMDB = _env("GO_SUMDB", "sum.golang.org")            # download mode only; off disables lookups
# User module cache read offline. Unset: `go env GOMODCACHE` with the user's environment.
GO_USER_MODCACHE = _env_opt("GO_USER_MODCACHE")
GO_HELPER_TIMEOUT = _env_int("GO_HELPER_TIMEOUT", 1800)  # seconds per helper run

# ── Wiki track (spec §7.2, R11) ───────────────────────────────
# Above either limit, Phase 2 summarizes a file node by node.
WIKI_FILE_MAX_CHARS = _env_int("WIKI_FILE_MAX_CHARS", 40000)
WIKI_FILE_MAX_NODES = _env_int("WIKI_FILE_MAX_NODES", 40)
# False: generated files get their doc comment plus "生成コード", no Gemini call.
WIKI_SUMMARIZE_GENERATED = _env_flag("WIKI_SUMMARIZE_GENERATED")
# Summary cache directory, shareable by codedoc_go and codedoc_go_leiden.
# Unset: OUTPUT_DIR/repos/<repo>/summaries.
WIKI_SUMMARY_CACHE = _env_opt("WIKI_SUMMARY_CACHE")

# ── Peers ─────────────────────────────────────────────────────
PEER_REPOS = _env_opt("PEER_REPOS")                   # "[name=]path" entries joined by os.pathsep

# ── File scanning ─────────────────────────────────────────────
# Which files and directories are skipped follows the go command's rules
# (go_units.scan_go_files).
SOURCE_EXTENSIONS = {".go"}
