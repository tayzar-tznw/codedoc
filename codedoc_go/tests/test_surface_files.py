"""Adapted surface files: .env.example, sample_prompt.txt, the Dockerfiles.

.env.example and sample_prompt.txt are written by WP-B1 / WP-C2; the checks of
their Go values switch on once the copies from codedoc_python are replaced.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV_TEXT = (ROOT / ".env.example").read_text(encoding="utf-8")
PROMPT_TEXT = (ROOT / "sample_prompt.txt").read_text(encoding="utf-8")
needs_go_env = pytest.mark.skipif("PY_LSP_COMMAND" in ENV_TEXT,
                                  reason="WP-B1 has not adapted .env.example for Go yet")
needs_go_prompt = pytest.mark.skipif(".py" in PROMPT_TEXT,
                                     reason="sample_prompt.txt is still the Python copy (WP-B1/C2)")


def _env_example() -> dict[str, str | None]:
    """KEY -> value for set keys, None for keys shown commented out."""
    out: dict[str, str | None] = {}
    for line in ENV_TEXT.splitlines():
        m = re.match(r"^(#\s*)?([A-Z][A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            out[m.group(2)] = None if m.group(1) else m.group(3)
    return out


def test_env_example_lists_every_key_config_reads():
    from scripts import doccheck
    keys = doccheck.config_keys((ROOT / "graph_generator" / "config.py").read_text(encoding="utf-8"))
    assert keys and keys <= set(_env_example())
    assert doccheck.DOTENV_SWITCH not in _env_example()     # read before .env, so never set there


@needs_go_env
@pytest.mark.parametrize("key, value", [
    ("GOOGLE_GENAI_USE_VERTEXAI", "true"), ("SPANNER_INSTANCE", "codedoc-instance"),
    ("SPANNER_DATABASE", "codedoc-go"), ("GRAPH_NAME", "code_graph_a"),
    ("GO_BUILD_GOOS", "linux"), ("GO_BUILD_GOARCH", "amd64"), ("GO_BUILD_CGO", "0"),
    ("GO_MODULES_DOWNLOAD", "0"), ("GO_PROXY", "https://proxy.golang.org"),
    ("GO_HELPER_TIMEOUT", "1800"), ("WIKI_FILE_MAX_CHARS", "40000"),
    ("WIKI_FILE_MAX_NODES", "40"), ("POSSIBLY_CALLS_MAX_CANDIDATES", "5"),
    ("GOOGLE_CLOUD_PROJECT", None), ("GO_TOOLCHAIN", None), ("GO_USER_MODCACHE", None),
    ("PEER_REPOS", None),
])
def test_env_example_values_follow_spec(key, value):
    assert _env_example()[key] == value


@needs_go_env
@pytest.mark.parametrize("key", ["GO_BUILD_TAGS", "GO_EXTRA_BUILDS"])
def test_env_example_empty_defaults(key):
    assert _env_example().get(key, "missing") in ("", None)


@pytest.mark.parametrize("stale", ["INTELEPHENSE_PATH", "LSP_INDEX_TIMEOUT", "LSP_REQUEST_TIMEOUT",
                                   "INCLUDE_VENDOR", "codedoc-db", "development-459201",
                                   "claude-cws-498905"])
def test_env_example_has_no_php_leftovers(stale):
    assert not re.search(rf"(?<![A-Z_]){re.escape(stale)}", ENV_TEXT)


@needs_go_env
@pytest.mark.parametrize("stale", ["PY_LSP_", "PY_ENV_", "pyright", "codedoc-python"])
def test_env_example_has_no_python_leftovers(stale):
    assert stale not in ENV_TEXT


@needs_go_prompt
def test_sample_prompt_asks_go_questions_in_japanese():
    lines = [l for l in PROMPT_TEXT.splitlines() if l.strip()]
    assert len(lines) >= 5
    assert all(re.search(r"[぀-ヿ一-鿿]", l) for l in lines)
    text = "\n".join(lines)
    assert ".go" in text and "FeeCalculator" in text
    assert not re.search(r"\.php|Controller|Table\.php|distribution|配布物", text)


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "mcp_server/Dockerfile"])
def test_dockerfile_copies_existing_sources_and_bakes_no_env(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    built = {"output_docs_pipeline", "webapp/frontend/dist"}
    for src in re.findall(r"^COPY\s+(\S+)\s+\S+", text, re.MULTILINE):
        assert src in built or (ROOT / src).exists(), src
    assert ".env" not in re.sub(r"#.*", "", text)


def test_webapp_image_runs_the_webapp_with_vertex():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV GOOGLE_GENAI_USE_VERTEXAI=true" in text
    assert "development-459201" not in text
    assert re.search(r"^CMD .*uvicorn webapp\.main:app", text, re.MULTILINE)
