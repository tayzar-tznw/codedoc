"""scripts.doccheck: each check on small inputs, the whole gate on a fixture doc
tree written by `_write_docs`, then the real repository once its Go docs exist."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from scripts import doccheck
from scripts.verification_table import EVAL_HEADER, TRIAL_HEADER
from tests.go_schema_spec import EDGE_SPEC, NODE_SPEC

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("heading, expected", [
    ("CLI リファレンス", "cli-リファレンス"),
    ("鮮度とレジューム", "鮮度とレジューム"),
    ("5. ID 規則", "5-id-規則"),
    ("webapp と MCP サーバーをローカルで動かす", "webapp-と-mcp-サーバーをローカルで動かす"),
    ("`resolutions.json`", "resolutionsjson"),
    ("オフラインとダウンロード（--download-modules）", "オフラインとダウンロード--download-modules"),
])
def test_slug_follows_github(heading, expected):
    assert doccheck.slug(heading.replace("`", "")) == expected


def test_anchors_skip_code_and_number_duplicates():
    md = "# 概要\n```\n# not a heading\n```\n## 概要\n### `units.json`\n"
    assert doccheck.anchors(md) == {"概要", "概要-1", "unitsjson"}


def test_links_resolve(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "A.md").write_text("# 1. 問題\n[up](../README.md#使い方)\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "## 使い方\n[a](docs/A.md#1-問題) [b](docs/B.md) [c](docs/A.md#nope) "
        "[d](https://example.com/x) ![img](pic.png) [e](#使い方) [f](#none)\n"
        "```\n[g](missing.md)\n```\n`HANDLERS[key](...)`\n", encoding="utf-8")
    errors = doccheck.check_links(tmp_path, ("README.md", "docs/A.md", "docs/C.md"))
    assert errors == ["README.md: broken link docs/B.md",
                      "README.md: missing anchor docs/A.md#nope",
                      "README.md: missing anchor #none",
                      "docs/C.md: missing"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    a = sub.add_parser("analyze")
    a.add_argument("target_dir", nargs="?")
    mode = a.add_mutually_exclusive_group(required=True)
    mode.add_argument("--graph", action="store_true")
    mode.add_argument("--wiki", action="store_true")
    a.add_argument("--peer", action="append")
    setup = sub.add_parser("setup").add_subparsers()
    setup.add_parser("go")
    e = sub.add_parser("evaluate")
    e.add_argument("--fixture", choices=["go_layouts", "multi_repo"])
    return p


def test_cli_surface_walks_nested_commands():
    s = doccheck.cli_surface(_parser())
    assert s == {"commands": {"analyze", "setup", "setup go", "evaluate"},
                 "flags": {"--graph", "--wiki", "--peer", "--fixture"},
                 "positionals": {"target_dir"}, "choices": {"go_layouts", "multi_repo"}}


def test_check_cli_reports_undocumented_terms():
    readme = ("| `analyze [target_dir] --graph\\|--wiki` | `--peer` |\n| `setup go` | `setup` |\n"
              "| `evaluate` | `--fixture go_layouts` |\n")
    errors = doccheck.check_cli(readme, doccheck.cli_surface(_parser()))
    assert errors == ["README.md: CLI term multi_repo not documented"]
    assert doccheck.check_cli("`evaluate`", {"commands": {"setup spanner"}, "flags": set(),
                                             "positionals": set(), "choices": set()}) == [
        "README.md: command `setup spanner` not documented"]


def test_cli_parser_prefers_build_parser(monkeypatch):
    import types
    fake = types.SimpleNamespace(build_parser=_parser, main=lambda: pytest.fail("main() ran"))
    monkeypatch.setitem(__import__("sys").modules, "graph_generator.__main__", fake)
    monkeypatch.setattr(__import__("graph_generator"), "__main__", fake, raising=False)
    assert "setup go" in doccheck.cli_surface(doccheck.cli_parser())["commands"]


def test_real_cli_matches_contract_c9():
    try:
        surface = doccheck.cli_surface(doccheck.cli_parser())
    except ImportError as e:
        pytest.skip(f"graph_generator.__main__ does not import yet (WP-B1): {e}")
    if "trial" not in surface["commands"]:
        pytest.skip("WP-B1 has not landed the Go CLI (spec §2.4) yet")
    assert {"analyze", "doctor", "setup go", "setup spanner", "validate", "crossref",
            "evaluate", "trial", "init"} <= surface["commands"]
    assert {"--all", "--graph", "--wiki", "--repo-name", "--repos", "--peer",
            "--download-modules", "--no-spanner", "--dry-run", "--fixture", "--env",
            "--packages"} <= surface["flags"]
    assert {"iryo-kanri-system", "go_layouts", "multi_repo"} <= surface["choices"]


def test_config_keys_come_from_env_reads_only():
    src = ("import os\nA = _env('SPANNER_DATABASE', 'x')\nB = _env_int('GO_HELPER_TIMEOUT', 1800)\n"
           "C = os.environ.get('GO_USER_MODCACHE')\nD = os.getenv('PEER_REPOS', '')\n"
           "E = {}.get('NOT_A_KEY')\nF = _env('lowercase', '')\nG = _env_bool('GO_BUILD_CGO', False)\n"
           "H = os.environ.get('CODEDOC_DOTENV', '1')\n")      # the .env switch is not a setting
    assert doccheck.config_keys(src) == {"SPANNER_DATABASE", "GO_HELPER_TIMEOUT",
                                         "GO_USER_MODCACHE", "PEER_REPOS", "GO_BUILD_CGO"}
    assert doccheck.module_constants(src) == {"A", "B", "C", "D", "E", "F", "G", "H"}


def test_check_config_rows():
    readme = ("## 設定リファレンス\n| キー | 既定値 |\n|---|---|\n| `SPANNER_DATABASE` | x |\n"
              "| `PY_LSP_COMMAND` | y |\n| `SKIP_DIRS`(コード定数) | z |\n## 次\n| `PEER_REPOS` | |\n")
    errors = doccheck.check_config(readme, {"SPANNER_DATABASE", "PEER_REPOS"}, {"SKIP_DIRS"})
    assert errors == ["README.md: config key PEER_REPOS has no row in 設定リファレンス",
                      "README.md: 設定リファレンス lists PY_LSP_COMMAND, which nothing reads"]
    assert doccheck.check_config("# x\n", set(), set()) == [
        "README.md: no '## 設定リファレンス' section"]


def test_check_env_example():
    text = ("# Project. Unset = the ADC default\n# GOOGLE_CLOUD_PROJECT=your-project-id\n"
            "GOOGLE_GENAI_USE_VERTEXAI=true\nSPANNER_DATABASE=codedoc-go\nPY_LSP_PLATFORM=Linux\n"
            "# Peer repos as [name=]path\n")
    errors = doccheck.check_env_example(
        text, {"GOOGLE_CLOUD_PROJECT", "SPANNER_DATABASE", "GO_TOOLCHAIN"},
        {"GOOGLE_GENAI_USE_VERTEXAI"})
    assert errors == [".env.example: config key GO_TOOLCHAIN is missing",
                      ".env.example: lists PY_LSP_PLATFORM, which nothing reads"]


def test_check_tables():
    doc = ("## 2. ノード表\n| 表 | カラム |\n|---|---|\n| Files | `file_id` `path` |\n"
           "| Modules | `module_id` |\n## 3. エッジ表\n| 表 | カラム |\n|---|---|\n"
           "| DirContainsFile | `edge_id` `dir_id` `file_id` |\n")
    node = {"Files": ("file_id", [("file_id", "str"), ("path", "str"), ("repo", "str")]),
            "Packages": ("package_id", [("package_id", "str")])}
    edge = {"DirContainsFile": EDGE_SPEC["DirContainsFile"]}
    assert doccheck.check_tables(doc, node, edge) == [
        "docs/GRAPH_SCHEMA.md: Files.repo not listed",
        "docs/GRAPH_SCHEMA.md: table Packages not listed",
        "docs/GRAPH_SCHEMA.md: lists Modules, which is not in the schema"]


@pytest.mark.parametrize("text, expected", [
    ("短い文です。\n", []),
    ("これは強力です。\n", ["x: marketing word 強力"]),
    ("堅牢でシームレス、包括的かつ網羅的。\n", ["x: marketing word 堅牢", "x: marketing word シームレス",
                                     "x: marketing word 包括的", "x: marketing word 網羅的"]),
    ("`強力` はコードの中\n```\n網羅的\n```\n", []),
    ("完了しました！\n", ["x: exclamation mark"]),
    ("done! ok\n", ["x: exclamation mark"]),
    ("a != b と ![図](x.png)、`//go:build !cgo`\n", []),
    ("結果 \U0001F680\n", ["x: emoji"]),
    ("| 2026-10-14 | 〔C2で記入〕 |\n", ["x: unfilled placeholder 〔C2で記入〕"]),
    ("| etcd | TBD |\n", ["x: unfilled placeholder TBD"]),
])
def test_check_prose(text, expected):
    assert doccheck.check_prose("x", text) == expected


def test_check_gql_lints_graph_blocks_only():
    doc = ("```sql\nSELECT 1 FROM INFORMATION_SCHEMA.TABLES;\n```\n"
           "```sql\nGRAPH g\nMATCH (c:Classes) RETURN c.namespace;\n```\n"
           "```go\n// GRAPH g\nfunc main() {}\n```\n"
           "```gql\nGRAPH g\nMATCH (p:Packages)-[:PackageContainsFile]->(f:Files) RETURN f.path\n```\n")
    assert doccheck.check_gql("d.md", doc, NODE_SPEC, EDGE_SPEC) == [
        "d.md: statement 1: Classes has no column namespace"]


FIXTURES = ("iryo-kanri-system", "go_layouts")


def _eval_row(fixture: str, mode: str, last: str = "— | 4/4 | 9 / 0 | PASS") -> str:
    return f"| `{fixture}` | {mode} | 10 | 10 | 0 | 0 | 0 | 10（必要 9） | — | — | — | {last} |"


EVAL_ROWS = [_eval_row(f, m) for f in FIXTURES for m in ("既定", "`--env`")]


def _trial_row(mode: str, changed: str = "—", confirmed: str = "6") -> str:
    return (f"| `etcd` | {mode} | 1840 | 183 | 72623 | 1 | 2 | 3 | 4 | 5 | 100.0% | 2.3 | 1.7 GB "
            f"| {changed} | {confirmed} | 0 |")


TRIAL_ROWS = [_trial_row("オフライン"), _trial_row("ダウンロード", changed="0")]


def _eval_table(rows: list[str] = EVAL_ROWS) -> str:
    return "\n".join([EVAL_HEADER, "|---" * (EVAL_HEADER.count("|") - 1) + "|", *rows]) + "\n"


def _trial_table(rows: list[str] = TRIAL_ROWS) -> str:
    return "\n".join([TRIAL_HEADER, "|---" * (TRIAL_HEADER.count("|") - 1) + "|", *rows]) + "\n"


def test_check_verification_accepts_filled_tables():
    text = "2026-10-14 の結果です。\n\n" + _eval_table() + "\n" + _trial_table()
    assert doccheck.check_verification("v.md", text, FIXTURES) == []


ERROR_ROW = "| `go_layouts` | `--env` | エラー: toolchain not found |"
EMPTY_CELL_ROW = _eval_row("go_layouts", "`--env`", last=" | 4/4 | — | PASS")
EXTRA_ROW = _eval_row("multi_repo", "既定", last="一致 | 4/4 | 9 / 0 | PASS")
PLACEHOLDER_ROW = _trial_row("オフライン", confirmed="〔記入〕")


@pytest.mark.parametrize("text, expected", [
    (_trial_table(), ["v.md: no evaluate verification table"]),
    (_eval_table(), ["v.md: no trial verification table"]),
    (_eval_table([]) + "\n" + _trial_table([]),
     ["v.md: evaluate verification table has no rows", "v.md: trial verification table has no rows",
      "v.md: evaluate table has no row for `iryo-kanri-system` 既定",
      "v.md: evaluate table has no row for `iryo-kanri-system` `--env`",
      "v.md: evaluate table has no row for `go_layouts` 既定",
      "v.md: evaluate table has no row for `go_layouts` `--env`"]),
    (_eval_table(EVAL_ROWS[:3] + [ERROR_ROW]) + "\n" + _trial_table(),
     [f"v.md: unfilled evaluate row: {ERROR_ROW}"]),
    (_eval_table(EVAL_ROWS[:3] + [EMPTY_CELL_ROW]) + "\n" + _trial_table(),
     [f"v.md: unfilled evaluate row: {EMPTY_CELL_ROW}"]),
    (_eval_table(EVAL_ROWS + [EXTRA_ROW]) + "\n" + _trial_table(),
     ["v.md: evaluate table lists `multi_repo` 既定, which is not a fixture and mode"]),
    (_eval_table(EVAL_ROWS + EVAL_ROWS[:1]) + "\n" + _trial_table(),
     ["v.md: evaluate table lists `iryo-kanri-system` 既定 twice"]),
    (_eval_table() + "\n" + _trial_table([PLACEHOLDER_ROW]),
     [f"v.md: unfilled trial row: {PLACEHOLDER_ROW}",
      "v.md: trial table has no ダウンロード row for `etcd`"]),
], ids=["no-evaluate", "no-trial", "no-rows", "error-row", "empty-cell",
        "extra-row", "duplicate-row", "placeholder"])
def test_check_verification_reports_unfilled_tables(text, expected):
    assert doccheck.check_verification("v.md", text, FIXTURES) == expected


# ---------------------------------------------------------------- the whole gate

SMALL_NODE = {"Files": NODE_SPEC["Files"], "Packages": NODE_SPEC["Packages"]}
SMALL_EDGE = {"PackageContainsFile": EDGE_SPEC["PackageContainsFile"]}
SURFACE = {"commands": {"analyze", "setup go", "evaluate"}, "flags": {"--graph", "--fixture"},
           "positionals": {"target_dir"}, "choices": {"go_layouts"}}
CONFIG_SRC = "A = _env('SPANNER_DATABASE', 'codedoc-go')\nB = _env_opt('GO_TOOLCHAIN')\nSKIP_DIRS = {'vendor'}\n"


def _columns(spec: tuple) -> str:
    return " ".join(f"`{col}`" for col, _type in spec[1])


def _write_docs(root: Path, **override: str) -> Path:
    """A minimal doc tree that passes every check; `override` replaces files."""
    files = {
        "README.md": (
            "# codedoc_go\n\n詳しくは [設計](docs/ARCHITECTURE.md#1-概要) を見てください。\n\n"
            "## 設定リファレンス\n\n| キー | 既定値 |\n|---|---|\n| `SPANNER_DATABASE` | `codedoc-go` |\n"
            "| `GO_TOOLCHAIN` | 未設定 |\n| `WEBAPP_REPO` | 未設定 |\n| `SKIP_DIRS`(コード定数) | vendor |\n\n"
            "## CLI リファレンス\n\n| コマンド | 意味 |\n|---|---|\n| `analyze [target_dir] --graph` | グラフ |\n"
            "| `setup go` | ツールチェーン |\n| `evaluate --fixture go_layouts` | 評価 |\n"),
        "docs/ARCHITECTURE.md": "# 設計\n\n## 1. 概要\n\n短い文です。[設定](../README.md#設定リファレンス)\n",
        "docs/LSP_RESOLUTION.md": ("# 解決\n\n2026-10-14 の結果です。\n\n" + _eval_table() + "\n"
                                   + _trial_table()),
        "docs/GRAPH_SCHEMA.md": (
            "# スキーマ\n\n## 2. ノード表\n\n| 表 | カラム |\n|---|---|\n"
            + "".join(f"| {t} | {_columns(s)} |\n" for t, s in SMALL_NODE.items())
            + "\n## 3. エッジ表\n\n| 表 | カラム |\n|---|---|\n"
            + "".join(f"| {t} | {_columns(s)} |\n" for t, s in SMALL_EDGE.items())
            + "\n## 7. GQL クエリ集\n\n```sql\nGRAPH code_graph_a\n"
              "MATCH (p:Packages)-[:PackageContainsFile]->(f:Files)\nRETURN p.import_path, f.path\n```\n"),
        "docs/CROSS_PROJECT.md": "# リポジトリ間\n\n短い文です。\n",
        "CLAUDE.md": "# CLAUDE.md\n\nEnglish notes. See [schema](docs/GRAPH_SCHEMA.md#2-ノード表)!\n",
        "mcp_server/README.md": "# MCP server\n\nSee [README](../README.md).\n",
        ".claude/skills/codedoc-query/SKILL.md": "---\nname: codedoc-query\n---\n# Skill\n",
        ".env.example": "# GO_TOOLCHAIN=/opt/go/bin/go\nSPANNER_DATABASE=codedoc-go\nGOOGLE_GENAI_USE_VERTEXAI=true\n",
        "docs/superpowers/notes.md": ("# Research notes\n\n強力で網羅的な調査！ \U0001F680 〔記入〕\n\n"
                                      "```sql\nGRAPH g\nMATCH (m:Modules) RETURN m.name\n```\n"),
    }
    files.update(override)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _check(root: Path) -> list[str]:
    return doccheck.check_docs(root, cli=SURFACE, config_src=CONFIG_SRC, node_spec=SMALL_NODE,
                               edge_spec=SMALL_EDGE, fixtures=FIXTURES)


def test_fixture_docs_pass_and_research_notes_are_excluded(tmp_path):
    assert _check(_write_docs(tmp_path)) == []


def test_ja_docs_are_the_doc_set_without_superpowers(tmp_path):
    _write_docs(tmp_path, **{"docs/EXTRA.md": "# 追加\n", "docs/superpowers/specs/x.md": "# x\n"})
    assert doccheck.ja_docs(tmp_path) == (
        "README.md", "docs/ARCHITECTURE.md", "docs/CROSS_PROJECT.md", "docs/EXTRA.md",
        "docs/GRAPH_SCHEMA.md", "docs/LSP_RESOLUTION.md")


@pytest.mark.parametrize("override, expected", [
    ({"docs/EXTRA.md": "# 追加\n\nこれは強力です！\n"},
     ["docs/EXTRA.md: marketing word 強力", "docs/EXTRA.md: exclamation mark"]),
    ({"docs/ARCHITECTURE.md": "# 設計\n\n[x](GONE.md)\n"},
     ["README.md: missing anchor docs/ARCHITECTURE.md#1-概要", "docs/ARCHITECTURE.md: broken link GONE.md"]),
    ({"README.md": "# codedoc_go\n"},
     ["docs/ARCHITECTURE.md: missing anchor ../README.md#設定リファレンス",
      "README.md: command `analyze` not documented", "README.md: command `evaluate` not documented",
      "README.md: command `setup go` not documented", "README.md: CLI term --fixture not documented",
      "README.md: CLI term --graph not documented", "README.md: CLI term go_layouts not documented",
      "README.md: CLI term target_dir not documented", "README.md: no '## 設定リファレンス' section"]),
    ({".env.example": "SPANNER_DATABASE=codedoc-go\nPY_ENV_PROVISION=0\n"},
     [".env.example: config key GO_TOOLCHAIN is missing",
      ".env.example: lists PY_ENV_PROVISION, which nothing reads"]),
    ({"docs/GRAPH_SCHEMA.md": "# スキーマ\n\n## 2. ノード表\n\n| 表 | カラム |\n|---|---|\n| Files | `file_id` |\n"
                              "\n## 3. エッジ表\n\n| 表 | カラム |\n|---|---|\n"},
     [f"docs/GRAPH_SCHEMA.md: Files.{c} not listed" for c, _t in NODE_SPEC["Files"][1][1:]]
     + ["docs/GRAPH_SCHEMA.md: table Packages not listed",
        "docs/GRAPH_SCHEMA.md: table PackageContainsFile not listed"]),
    ({"docs/CROSS_PROJECT.md": "# x\n\n```sql\nGRAPH g\nMATCH (m:Methods) RETURN m.fqmn\n```\n"},
     ["docs/CROSS_PROJECT.md: statement 1: unknown node label Methods"]),
    ({".claude/skills/codedoc-query/SKILL.md": "# Skill\n\n```gql\nGRAPH g\nMATCH (t:Topics) RETURN t.name\n```\n"},
     [".claude/skills/codedoc-query/SKILL.md: statement 1: unknown node label Topics"]),
    ({"docs/LSP_RESOLUTION.md": "# 解決\n\n" + _eval_table()},
     ["docs/LSP_RESOLUTION.md: no trial verification table"]),
])
def test_each_rule_reports_its_breakage(tmp_path, override, expected):
    assert _check(_write_docs(tmp_path, **override)) == expected


def test_missing_docs_stop_the_gate(tmp_path):
    _write_docs(tmp_path)
    (tmp_path / "docs" / "CROSS_PROJECT.md").unlink()
    (tmp_path / ".env.example").unlink()
    assert _check(tmp_path) == ["docs/CROSS_PROJECT.md: missing", ".env.example: missing"]


def test_main_runs_against_a_schema_module(monkeypatch, capsys):
    seen = {}

    def fake_run(root=doccheck.ROOT, spec=doccheck.DEFAULT_SPEC):
        seen.update(root=root, spec=spec)
        return ["README.md: problem"]

    monkeypatch.setattr(doccheck, "run", fake_run)
    assert doccheck.main(["--spec", "tests.go_schema_spec"]) == 1
    assert seen == {"root": doccheck.ROOT, "spec": "tests.go_schema_spec"}
    assert capsys.readouterr().out.splitlines() == ["README.md: problem",
                                                    "doccheck: FAIL (1 problems)"]


def test_repository_docs_pass():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "pyright" in readme:
        pytest.skip("WP-C2 rewrites the docs for the Go CLI and settings")
    assert doccheck.run(ROOT) == []
