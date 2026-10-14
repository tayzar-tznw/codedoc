"""config: spec §2.5 settings (C8) and .env.example parity."""

import importlib
import os
import pathlib
import re

import pytest

from graph_generator import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_KEYS = (
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GEMINI_MODEL", "GEMINI_CONCURRENCY",
    "MAX_OUTPUT_TOKENS", "SPANNER_INSTANCE", "SPANNER_DATABASE", "GRAPH_NAME", "ID_PREFIX",
    "OUTPUT_DIR", "EMBED_CONCURRENCY", "POSSIBLY_CALLS_MAX_CANDIDATES",
    "GO_TOOLCHAIN", "GO_BUILD_GOOS", "GO_BUILD_GOARCH", "GO_BUILD_TAGS", "GO_BUILD_CGO",
    "GO_EXTRA_BUILDS", "GO_MODULES_DOWNLOAD", "GO_PROXY", "GO_SUMDB", "GO_USER_MODCACHE",
    "GO_HELPER_TIMEOUT", "WIKI_FILE_MAX_CHARS", "WIKI_FILE_MAX_NODES",
    "WIKI_SUMMARIZE_GENERATED", "WIKI_SUMMARY_CACHE", "PEER_REPOS",
)


@pytest.fixture
def reload_config(monkeypatch):
    """Re-import config under a controlled environment (no .env file)."""
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)

    def _reload(**env):
        for key in _KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config)

    yield _reload
    monkeypatch.undo()
    importlib.reload(config)


def test_defaults(reload_config):
    c = reload_config()
    assert c.GCP_PROJECT is None  # no default project: the ADC / gcloud default applies
    assert (c.SPANNER_INSTANCE, c.SPANNER_DATABASE, c.GRAPH_NAME, c.ID_PREFIX) == (
        "codedoc-instance", "codedoc-go", "code_graph_a", "a")
    assert c.GO_TOOLCHAIN is None
    assert (c.GO_BUILD_GOOS, c.GO_BUILD_GOARCH, c.GO_BUILD_TAGS) == ("linux", "amd64", "")
    assert c.GO_BUILD_CGO is False and c.GO_EXTRA_BUILDS == ""
    assert c.GO_MODULES_DOWNLOAD is False and c.GO_PROXY == "https://proxy.golang.org"
    assert c.GO_SUMDB == "sum.golang.org"
    assert c.GO_USER_MODCACHE is None and c.GO_HELPER_TIMEOUT == 1800
    assert (c.WIKI_FILE_MAX_CHARS, c.WIKI_FILE_MAX_NODES) == (40000, 40)
    assert c.WIKI_SUMMARIZE_GENERATED is False and c.WIKI_SUMMARY_CACHE is None
    assert c.PEER_REPOS is None and c.POSSIBLY_CALLS_MAX_CANDIDATES == 5
    assert c.SOURCE_EXTENSIONS == {".go"}


@pytest.mark.parametrize("env,attr,expected", [
    ({"GOOGLE_CLOUD_PROJECT": "my-project"}, "GCP_PROJECT", "my-project"),
    ({"GOOGLE_CLOUD_PROJECT": ""}, "GCP_PROJECT", None),
    ({"SPANNER_DATABASE": "other-db"}, "SPANNER_DATABASE", "other-db"),
    ({"GO_TOOLCHAIN": "/opt/go/bin/go"}, "GO_TOOLCHAIN", "/opt/go/bin/go"),
    ({"GO_TOOLCHAIN": "  "}, "GO_TOOLCHAIN", None),
    ({"GO_BUILD_GOOS": "windows"}, "GO_BUILD_GOOS", "windows"),
    ({"GO_BUILD_TAGS": "integration,e2e"}, "GO_BUILD_TAGS", "integration,e2e"),
    ({"GO_BUILD_CGO": "1"}, "GO_BUILD_CGO", True),
    ({"GO_BUILD_CGO": "0"}, "GO_BUILD_CGO", False),
    ({"GO_EXTRA_BUILDS": "windows/amd64 linux/arm64+e2e"}, "GO_EXTRA_BUILDS",
     "windows/amd64 linux/arm64+e2e"),
    ({"GO_MODULES_DOWNLOAD": "1"}, "GO_MODULES_DOWNLOAD", True),
    ({"GO_MODULES_DOWNLOAD": "yes"}, "GO_MODULES_DOWNLOAD", False),
    ({"GO_PROXY": "file:///srv/proxy"}, "GO_PROXY", "file:///srv/proxy"),
    ({"GO_SUMDB": "off"}, "GO_SUMDB", "off"),
    ({"GO_USER_MODCACHE": "/home/u/go/pkg/mod"}, "GO_USER_MODCACHE", "/home/u/go/pkg/mod"),
    ({"GO_HELPER_TIMEOUT": "60"}, "GO_HELPER_TIMEOUT", 60),
    ({"WIKI_FILE_MAX_CHARS": "1000"}, "WIKI_FILE_MAX_CHARS", 1000),
    ({"WIKI_FILE_MAX_NODES": "5"}, "WIKI_FILE_MAX_NODES", 5),
    ({"WIKI_SUMMARIZE_GENERATED": "1"}, "WIKI_SUMMARIZE_GENERATED", True),
    ({"WIKI_SUMMARY_CACHE": "/srv/summaries"}, "WIKI_SUMMARY_CACHE", "/srv/summaries"),
    ({"WIKI_SUMMARY_CACHE": ""}, "WIKI_SUMMARY_CACHE", None),
    ({"PEER_REPOS": "shared=/abs/shared"}, "PEER_REPOS", "shared=/abs/shared"),
])
def test_overrides(reload_config, env, attr, expected):
    assert getattr(reload_config(**env), attr) == expected


@pytest.mark.parametrize("name", [
    "PY_LSP_COMMAND", "PY_LSP_INDEX_TIMEOUT", "PY_LSP_REQUEST_TIMEOUT", "PY_LSP_MAX_SESSIONS",
    "PY_LSP_PYTHON_VERSION", "PY_LSP_PLATFORM", "PY_ENV_INTERPRETER", "PY_ENV_UNITS",
    "PY_ENV_PROVISION", "SKIP_DIRS", "is_skip_dir", "INTELEPHENSE_PATH",
])
def test_python_and_php_settings_are_gone(name):
    assert not hasattr(config, name)


def test_env_example_lists_every_setting():
    source = pathlib.Path(ROOT, "graph_generator", "config.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'_env(?:_int|_opt|_flag)?\("([A-Z0-9_]+)"', source))
    assert keys == set(_KEYS)
    example = pathlib.Path(ROOT, ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", example, flags=re.M))
    assert keys <= listed, sorted(keys - listed)
    assert not {k for k in listed if k.startswith("PY_")}
    assert "\n# GOOGLE_CLOUD_PROJECT=" in example  # shown, but unset by default
    assert "SPANNER_DATABASE=codedoc-go\n" in example
