"""Shared pytest fixtures and fakes for the graph_generator test suite.

The pipeline's three external services — Vertex Gemini (google.genai), Cloud
Spanner, and Vertex embeddings — are replaced with in-process fakes so every
phase runs locally with no GCP. The fakes are deliberately small: they record
what the pipeline sends and return programmable responses.
"""

from __future__ import annotations

import copy
import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════
# Hermetic settings: no root .env, no exported configuration
# ═══════════════════════════════════════════════════════════════════
# This runs before any project module is imported. Under CODEDOC_DOTENV=0,
# graph_generator/config.py, graph_query_agent/config.py, webapp/main.py and
# mcp_server/runner.py skip the root .env, and the configuration variables a
# user may have exported are removed, so every module computes its defaults.
# An exported GO_TOOLCHAIN still names the toolchain of the `go`-marked tests,
# through CODEDOC_TEST_GO (tests/go_worlds.py).

_CONFIG_ENV_PREFIXES = ("SPANNER_", "GO_", "WIKI_", "EMBED_", "GEMINI_", "GOOGLE_CLOUD_", "GOOGLE_GENAI_")
_CONFIG_ENV_NAMES = frozenset({"GRAPH_NAME", "ID_PREFIX", "OUTPUT_DIR", "PEER_REPOS", "MAX_OUTPUT_TOKENS",
                               "POSSIBLY_CALLS_MAX_CANDIDATES", "WEBAPP_REPO"})


def is_config_env(name: str) -> bool:
    """A variable the project's settings read (or the Google libraries take
    from the documented .env)."""
    return name in _CONFIG_ENV_NAMES or name.startswith(_CONFIG_ENV_PREFIXES)


def _clear_config_env() -> None:
    for name in [n for n in os.environ if is_config_env(n)]:
        del os.environ[name]
    os.environ["CODEDOC_DOTENV"] = "0"


if os.environ.get("GO_TOOLCHAIN", "").strip():
    os.environ.setdefault("CODEDOC_TEST_GO", os.environ["GO_TOOLCHAIN"].strip())
_clear_config_env()

from graph_generator import config as _config  # noqa: E402  (after the switch, on purpose)

_CONFIG_DEFAULTS = {name: copy.deepcopy(value) for name, value in vars(_config).items() if name.isupper()}


@pytest.fixture(autouse=True)
def hermetic_settings():
    """Every test starts without configuration variables and with
    graph_generator.config at its defaults, whatever an earlier test or the
    code it ran assigned or exported (monkeypatch still undoes a test's own
    changes afterwards)."""
    _clear_config_env()
    for name, value in _CONFIG_DEFAULTS.items():
        setattr(_config, name, copy.deepcopy(value))


# ═══════════════════════════════════════════════════════════════════
# Fake google.genai client
# ═══════════════════════════════════════════════════════════════════


class _FakeResponse:
    def __init__(self, text: str, finish_reason=None, usage=None):
        self._text = text
        if finish_reason is not None:
            cand = types.SimpleNamespace(finish_reason=finish_reason)
            self.candidates = [cand]
        else:
            self.candidates = []
        if usage is not None:  # (prompt tokens, output tokens)
            self.usage_metadata = types.SimpleNamespace(prompt_token_count=usage[0],
                                                        candidates_token_count=usage[1])

    @property
    def text(self):
        return self._text


def default_responder(model: str, contents: str, config) -> str:
    """Return content shaped for whichever phase issued the prompt."""
    p = contents if isinstance(contents, str) else str(contents)
    wants_json = ("JSON形式" in p or "JSON配列" in p or "valid JSON" in p
                  or "ONLY valid JSON" in p)
    is_topic = "モジュール" in p or "トピック" in p

    if wants_json and is_topic:
        return ('[{"name":"料金計算",'
                '"files":["libs/billing/fee.go","libs/billing/doc.go"],'
                '"subtopics":[{"name":"計算規則","files":["libs/billing/fee.go"]}]}]')
    return "これはテスト用の日本語要約です。ファイルの目的を説明します。"


class _FakeModels:
    def __init__(self, owner):
        self._owner = owner

    async def generate_content(self, model=None, contents=None, config=None):
        self._owner.calls += 1
        responder = self._owner.responder
        result = responder(model, contents, config)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, _FakeResponse):
            return result
        return _FakeResponse(result)


class FakeGenaiClient:
    """Stand-in for genai.Client with an async aio.models.generate_content.

    `responder(model, contents, config)` may return a str, a `_FakeResponse`,
    or an Exception instance (which is raised — for testing retry/backoff).
    Pass a list to `script` to return/raise successive items per call.
    """

    def __init__(self, responder=default_responder, script=None):
        if script is not None:
            it = iter(script)

            def _scripted(model, contents, config):
                try:
                    return next(it)
                except StopIteration:
                    return default_responder(model, contents, config)
            responder = _scripted
        self.responder = responder
        self.calls = 0
        self.aio = types.SimpleNamespace(models=_FakeModels(self))


# ═══════════════════════════════════════════════════════════════════
# Fake Cloud Spanner database
# ═══════════════════════════════════════════════════════════════════


class _FakeBatch:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def insert_or_update(self, table, columns, values):
        self._db.mutations.setdefault(table, []).extend(values)
        self._db.columns[table] = columns

    def update(self, table, columns, values):
        self._db.updates.setdefault(table, []).extend(values)

    def delete(self, table, keyset):
        self._db.deletes.setdefault(table, []).extend(list(getattr(keyset, "keys", None) or []))


class _FakeSnapshot:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_sql(self, sql, params=None, param_types=None):
        return self._db.sql_result(sql, params)


class _FakeTransaction(_FakeBatch):
    """A read-write transaction: DML statements plus mutations."""

    def execute_update(self, sql, params=None, param_types=None):
        self._db.dml.append((sql, dict(params or {})))
        return 0

    def insert(self, table, columns, values):
        self._db.inserts.setdefault(table, []).extend(values)
        self._db.columns[table] = columns


class FakeSpannerDb:
    """Records mutations, DML and transactions; returns canned rows for
    snapshot queries."""

    def __init__(self, sql_rows=None):
        self.mutations: dict[str, list] = {}
        self.updates: dict[str, list] = {}
        self.inserts: dict[str, list] = {}
        self.deletes: dict[str, list] = {}   # table → deleted keys (codedoc_go_leiden's topic tables)
        self.columns: dict[str, list] = {}
        self.dml: list[tuple[str, dict]] = []
        self.transactions = 0
        self._sql_rows = sql_rows or {}

    def batch(self):
        return _FakeBatch(self)

    def run_in_transaction(self, fn, *args, **kwargs):
        self.transactions += 1
        return fn(_FakeTransaction(self), *args, **kwargs)

    def snapshot(self):
        return _FakeSnapshot(self)

    def sql_result(self, sql, params):
        for marker, rows in self._sql_rows.items():
            if marker in sql:
                return list(rows)
        return [[0]]  # default COUNT(*)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Point config.OUTPUT_DIR at an isolated tmp dir for the duration."""
    from graph_generator import config
    d = tmp_path / "out"
    d.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", str(d))
    return str(d)


@pytest.fixture
def fake_genai():
    return FakeGenaiClient()


@pytest.fixture
def fake_spanner(monkeypatch):
    """Install a FakeSpannerDb as pipeline._get_spanner_db()'s return."""
    from graph_generator import pipeline
    db = FakeSpannerDb()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    return db


@pytest.fixture
def fake_vertexai(monkeypatch):
    """Inject fake vertexai + vertexai.language_models into sys.modules so
    Phase 10's function-local imports resolve to programmable embeddings."""
    calls = {"n": 0, "texts": [], "models": 0}

    class _Emb:
        def __init__(self, values):
            self.values = values

    class _Model:
        @classmethod
        def from_pretrained(cls, name):
            calls["models"] += 1
            return cls()

        def get_embeddings(self, texts, output_dimensionality=None):
            calls["n"] += 1
            calls["texts"].extend(texts)
            calls["dimension"] = output_dimensionality
            return [_Emb([0.1, 0.2, 0.3]) for _ in texts]

    vertexai_mod = types.ModuleType("vertexai")
    vertexai_mod.init = lambda **kw: None
    lm_mod = types.ModuleType("vertexai.language_models")
    lm_mod.TextEmbeddingModel = _Model
    vertexai_mod.language_models = lm_mod
    monkeypatch.setitem(sys.modules, "vertexai", vertexai_mod)
    monkeypatch.setitem(sys.modules, "vertexai.language_models", lm_mod)
    return calls
@pytest.fixture(autouse=True)
def instant_sleeps(monkeypatch):
    """No test waits through a real retry or cooldown: pipeline._sleep and
    _sync_sleep record the requested seconds and return at once."""
    from graph_generator import pipeline
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(pipeline, "_sleep", _fake_sleep)
    monkeypatch.setattr(pipeline, "_sync_sleep", slept.append)
    return slept


def write_tree(root, files: dict[str, str]) -> str:
    """Create files (relpath → text) under root; returns root as str."""
    for rel, text in files.items():
        path = os.path.join(str(root), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return str(root)


SAMPLE_TREE = {
    "go.mod": "module example.com/iryo\n\ngo 1.26\n",
    "libs/billing/doc.go": "// Package billing computes fees.\npackage billing\n",
    "libs/billing/fee.go": (
        "package billing\n\n"
        "type FeeCalculator struct{}\n\n"
        "func (c *FeeCalculator) Calculate(points int) int {\n"
        "\treturn points * 10\n"
        "}\n"),
    "libs/billing/service.go": (
        "package billing\n\n"
        "func Total(points int) int {\n"
        "\treturn (&FeeCalculator{}).Calculate(points)\n"
        "}\n"),
    "libs/kernel/doc.go": "// Package kernel holds shared types.\npackage kernel\n",
    "cmd/reception/main.go": (
        "package main\n\n"
        "import (\n\t\"fmt\"\n\n\t\"example.com/iryo/libs/billing\"\n)\n\n"
        "func main() {\n"
        "\tfmt.Println(billing.Total(3))\n"
        "}\n"),
}
# The .go files of SAMPLE_TREE: two doc.go files share a basename on purpose.
SAMPLE_GO = sorted(rel for rel in SAMPLE_TREE if rel.endswith(".go"))


@pytest.fixture(autouse=True)
def no_adc_lookup(monkeypatch):
    """google.auth.default() never runs for real in a test (on GCE it asks the
    metadata server). Tests that want a resolved project patch it again."""
    import google.auth
    import google.auth.exceptions

    def _disabled(*args, **kwargs):
        raise google.auth.exceptions.DefaultCredentialsError("ADC lookup is disabled in tests")

    monkeypatch.setattr(google.auth, "default", _disabled)


@pytest.fixture(autouse=True)
def no_gemini_probe(monkeypatch):
    """The preflight's Gemini probe (one count_tokens call) never runs for real
    in a test. Tests that check it patch pipeline._gemini_probe again."""
    from graph_generator import pipeline
    monkeypatch.setattr(pipeline, "_gemini_probe", lambda: None)
