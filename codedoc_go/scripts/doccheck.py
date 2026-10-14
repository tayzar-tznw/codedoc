"""Check the docs against the code.

Run from codedoc_go/:  .venv/bin/python -m scripts.doccheck [--spec MODULE]

The Japanese docs are README.md and docs/**/*.md except docs/superpowers/**
(English research, specs and plans); the five-file set must exist. Checks,
each owned by one function:
- relative links and #anchors resolve (the Japanese docs, CLAUDE.md,
  mcp_server/README.md, the codedoc-query skill);
- every CLI command path, flag, positional and choice of
  `python -m graph_generator` appears in README.md (contract C9);
- every env key that graph_generator/config.py reads has a row in README's
  configuration section and a line in .env.example, and neither names a key
  nobody reads (contract C8);
- docs/GRAPH_SCHEMA.md lists every NODE_SPEC / EDGE_SPEC table with all of
  its columns, and no other table;
- every GQL statement in those docs fits the schema (scripts.gql_lint);
- the Japanese docs carry no marketing words, emoji, exclamation marks or
  unfilled placeholders;
- docs/LSP_RESOLUTION.md holds both verification tables of
  scripts.verification_table, filled: one evaluate row per fixture and mode,
  both trial modes per repo, no empty cell.

`--spec` names the module holding NODE_SPEC / EDGE_SPEC, as in scripts.gql_lint.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
import unicodedata
from pathlib import Path
from typing import Sequence

from scripts.gql_lint import DEFAULT_SPEC, SKILL, gql_blocks, lint_gql, load_spec
from scripts.verification_table import EVAL_HEADER, EVAL_MODES, TRIAL_HEADER, TRIAL_MODES

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCS = ("README.md", "docs/ARCHITECTURE.md", "docs/LSP_RESOLUTION.md",
                  "docs/GRAPH_SCHEMA.md", "docs/CROSS_PROJECT.md")
ENGLISH_DOCS = ("CLAUDE.md", "mcp_server/README.md", SKILL)
EXCLUDED = "docs/superpowers/"
ENV_EXAMPLE = ".env.example"
BANNED_WORDS = ("堅牢", "シームレス", "包括的", "強力", "網羅的")
PLACEHOLDER = re.compile(r"〔[^〕\n]*記入[^〕\n]*〕|(?<![A-Za-z])TBD(?![A-Za-z])|未記入")
# Keys read outside graph_generator/config.py: by the Gen AI SDK, by
# google-auth, by the MCP server, or by the webapp.
EXTERNAL_KEYS = frozenset({"GOOGLE_GENAI_USE_VERTEXAI", "GCE_METADATA_MTLS_MODE", "HOST", "PORT",
                           "WEBAPP_REPO"})
# Read by config.py before .env is loaded: CODEDOC_DOTENV=0 skips the .env (the
# test suite sets it, tests/conftest.py). A process switch, not a setting, so it
# is neither in .env.example nor in README's 設定リファレンス.
DOTENV_SWITCH = "CODEDOC_DOTENV"

_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_ENV_LINE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", re.MULTILINE)


def _strip_code(text: str) -> str:
    """Markdown without fenced blocks and inline code spans."""
    return re.sub(r"`[^`\n]*`", "", _FENCE.sub("", text))


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to '-'."""
    text = heading.strip().lower()
    text = "".join(ch for ch in text
                   if ch in " -_" or unicodedata.category(ch)[0] in "LNM")
    return text.replace(" ", "-")


def anchors(markdown: str) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for m in _HEADING.finditer(_FENCE.sub("", markdown)):
        base = slug(re.sub(r"`", "", m.group(2)))
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else f"{base}-{n}")
    return out


def ja_docs(root: Path) -> tuple[str, ...]:
    """README.md and every docs/**/*.md outside docs/superpowers/, sorted."""
    found = sorted(p.relative_to(root).as_posix() for p in (root / "docs").rglob("*.md"))
    return ("README.md",) + tuple(n for n in found if not n.startswith(EXCLUDED))


def check_links(root: Path, names: Sequence[str]) -> list[str]:
    errors = []
    for name in names:
        path = root / name
        if not path.exists():
            errors.append(f"{name}: missing")
            continue
        text = path.read_text(encoding="utf-8")
        for target in _LINK.findall(_strip_code(text)):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue
            file_part, _, anchor = target.partition("#")
            dest = (path.parent / file_part).resolve() if file_part else path
            if not dest.exists():
                errors.append(f"{name}: broken link {target}")
            elif anchor and dest.suffix == ".md" and \
                    anchor not in anchors(dest.read_text(encoding="utf-8")):
                errors.append(f"{name}: missing anchor {target}")
    return errors


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__("parser captured")
        self.parser = parser


def cli_parser() -> argparse.ArgumentParser:
    """The parser of `python -m graph_generator`: its build_parser(), else the
    parser main() builds, captured at parse_args()."""
    from graph_generator import __main__ as cli

    build = getattr(cli, "build_parser", None)
    if callable(build):
        return build()
    original = argparse.ArgumentParser.parse_args

    def _grab(self, *args, **kwargs):
        raise _Captured(self)

    argparse.ArgumentParser.parse_args = _grab
    try:
        cli.main()
    except _Captured as captured:
        return captured.parser
    finally:
        argparse.ArgumentParser.parse_args = original
    raise RuntimeError("graph_generator.__main__.main() did not call parse_args()")


def cli_surface(parser: argparse.ArgumentParser) -> dict[str, set]:
    surface: dict[str, set] = {"commands": set(), "flags": set(),
                               "positionals": set(), "choices": set()}

    def walk(p: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    surface["commands"].add(" ".join(path + (name,)))
                    walk(sub, path + (name,))
            elif isinstance(action, argparse._HelpAction):
                continue
            elif action.option_strings:
                surface["flags"].update(o for o in action.option_strings if o.startswith("--"))
                surface["choices"].update(str(c) for c in action.choices or ())
            else:
                surface["positionals"].add(action.dest)

    walk(parser, ())
    return surface


def check_cli(readme: str, surface: dict[str, set]) -> list[str]:
    errors = []
    for cmd in sorted(surface["commands"]):
        if not re.search("`" + re.escape(cmd) + r"(?=[\s`\[])", readme):
            errors.append(f"README.md: command `{cmd}` not documented")
    for word in sorted(surface["flags"] | surface["positionals"] | surface["choices"]):
        if not re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", readme):
            errors.append(f"README.md: CLI term {word} not documented")
    return errors


def config_keys(source: str) -> set[str]:
    """Env keys read by config.py: first argument of _env*() or os.environ.get()/os.getenv(),
    except DOTENV_SWITCH."""
    keys = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else \
            func.attr if isinstance(func, ast.Attribute) else ""
        first = node.args[0]
        if (name.startswith("_env") or name in ("getenv", "get")) and \
                isinstance(first, ast.Constant) and isinstance(first.value, str) and \
                re.fullmatch(r"[A-Z][A-Z0-9_]+", first.value):
            if name == "get" and not (isinstance(func, ast.Attribute)
                                      and ast.unparse(func.value).endswith("environ")):
                continue
            keys.add(first.value)
    return keys - {DOTENV_SWITCH}


def module_constants(source: str) -> set[str]:
    return {t.id for node in ast.parse(source).body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name) and t.id.isupper()}


def _section(markdown: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", markdown, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def check_config(readme: str, keys: set[str], known: set[str]) -> list[str]:
    section = _section(readme, "設定リファレンス")
    if not section:
        return ["README.md: no '## 設定リファレンス' section"]
    rows = set(re.findall(r"^\|\s*`([A-Z][A-Z0-9_]+)`", section, re.MULTILINE))
    errors = [f"README.md: config key {k} has no row in 設定リファレンス" for k in sorted(keys - rows)]
    errors += [f"README.md: 設定リファレンス lists {k}, which nothing reads"
               for k in sorted(rows - keys - known)]
    return errors


def check_env_example(text: str, keys: set[str], known: set[str]) -> list[str]:
    """Every key config.py reads is in .env.example, set or commented out; no stale keys."""
    listed = set(_ENV_LINE.findall(text))
    errors = [f"{ENV_EXAMPLE}: config key {k} is missing" for k in sorted(keys - listed)]
    errors += [f"{ENV_EXAMPLE}: lists {k}, which nothing reads" for k in sorted(listed - keys - known)]
    return errors


def check_tables(schema_doc: str, node_spec: dict, edge_spec: dict) -> list[str]:
    errors = []
    listed: dict[str, str] = {}
    for title in ("2. ノード表", "3. エッジ表"):
        section = _section(schema_doc, title)
        if not section:
            errors.append(f"docs/GRAPH_SCHEMA.md: no '## {title}' section")
        for m in re.finditer(r"^\|\s*`?([A-Z][A-Za-z]+)`?\s*\|(.*)$", section, re.MULTILINE):
            listed[m.group(1)] = m.group(2)
    for table, spec in {**node_spec, **edge_spec}.items():
        if table not in listed:
            errors.append(f"docs/GRAPH_SCHEMA.md: table {table} not listed")
            continue
        for col, _type in spec[1]:
            if f"`{col}`" not in listed[table]:
                errors.append(f"docs/GRAPH_SCHEMA.md: {table}.{col} not listed")
    errors += [f"docs/GRAPH_SCHEMA.md: lists {t}, which is not in the schema"
               for t in sorted(set(listed) - set(node_spec) - set(edge_spec))]
    return errors


def check_prose(name: str, text: str) -> list[str]:
    errors = [f"{name}: unfilled placeholder {m.group(0)}" for m in PLACEHOLDER.finditer(text)]
    prose = _strip_code(text)
    errors += [f"{name}: marketing word {w}" for w in BANNED_WORDS if w in prose]
    if _EMOJI.search(prose):
        errors.append(f"{name}: emoji")
    if "！" in prose or re.search(r"!(?![\[=])", prose):
        errors.append(f"{name}: exclamation mark")
    return errors


def check_gql(name: str, text: str, node_spec: dict, edge_spec: dict) -> list[str]:
    """Lint the GQL found by scripts.gql_lint.gql_blocks, statements numbered per doc."""
    return [f"{name}: {e}" for e in lint_gql(";\n".join(gql_blocks(text)), node_spec, edge_spec)]


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table_rows(text: str, header: str) -> list[str] | None:
    """Data rows of the table that starts with `header`; None when it is absent."""
    lines = text.splitlines()
    if header not in lines:
        return None
    rows = []
    for line in lines[lines.index(header) + 2:]:
        if not line.startswith("|"):
            break
        rows.append(line.strip())
    return rows


def check_verification(name: str, text: str, fixtures: Sequence[str]) -> list[str]:
    errors = []
    tables = {"evaluate": (EVAL_HEADER, _table_rows(text, EVAL_HEADER)),
              "trial": (TRIAL_HEADER, _table_rows(text, TRIAL_HEADER))}
    for label, (header, rows) in tables.items():
        if rows is None:
            errors.append(f"{name}: no {label} verification table")
            continue
        if not rows:
            errors.append(f"{name}: {label} verification table has no rows")
        width = len(_cells(header))
        for row in rows:
            cells = _cells(row)
            if len(cells) != width or not all(cells) or PLACEHOLDER.search(row):
                errors.append(f"{name}: unfilled {label} row: {row}")
    eval_rows = tables["evaluate"][1]
    if eval_rows is not None:
        want = [(f"`{f}`", m) for f in fixtures for m in EVAL_MODES]
        got = [tuple(_cells(r)[:2]) for r in eval_rows]
        errors += [f"{name}: evaluate table has no row for {f} {m}" for f, m in want if (f, m) not in got]
        seen: set[tuple] = set()
        for key in got:
            if key not in want:
                errors.append(f"{name}: evaluate table lists {' '.join(key)}, "
                              "which is not a fixture and mode")
            elif key in seen:
                errors.append(f"{name}: evaluate table lists {' '.join(key)} twice")
            seen.add(key)
    trial_rows = tables["trial"][1]
    if trial_rows:
        modes: dict[str, set[str]] = {}
        for row in trial_rows:
            repo, mode = (_cells(row) + ["", ""])[:2]
            modes.setdefault(repo, set()).add(mode)
        errors += [f"{name}: trial table has no {mode} row for {repo}"
                   for repo, have in modes.items() for mode in TRIAL_MODES.values()
                   if mode not in have]
    return errors


def check_docs(root: Path, *, cli: dict[str, set], config_src: str, node_spec: dict,
               edge_spec: dict, fixtures: Sequence[str]) -> list[str]:
    """Every check on the doc tree at `root`, given the code's facts."""
    missing = [f"{name}: missing" for name in CANONICAL_DOCS + ENGLISH_DOCS + (ENV_EXAMPLE,)
               if not (root / name).exists()]
    if missing:
        return missing
    docs = ja_docs(root)
    linked = docs + ENGLISH_DOCS
    text = {name: (root / name).read_text(encoding="utf-8") for name in linked}
    errors = check_links(root, linked)
    errors += check_cli(text["README.md"], cli)
    keys = config_keys(config_src)
    errors += check_config(text["README.md"], keys, module_constants(config_src) | EXTERNAL_KEYS)
    errors += check_env_example((root / ENV_EXAMPLE).read_text(encoding="utf-8"), keys,
                                EXTERNAL_KEYS)
    errors += check_tables(text["docs/GRAPH_SCHEMA.md"], node_spec, edge_spec)
    for name in docs:
        errors += check_prose(name, text[name])
    for name in linked:
        errors += check_gql(name, text[name], node_spec, edge_spec)
    errors += check_verification("docs/LSP_RESOLUTION.md", text["docs/LSP_RESOLUTION.md"], fixtures)
    return errors


def run(root: Path = ROOT, spec: str = DEFAULT_SPEC) -> list[str]:
    from scripts.verification_table import fixture_names

    node_spec, edge_spec = load_spec(spec)
    return check_docs(root, cli=cli_surface(cli_parser()),
                      config_src=(root / "graph_generator" / "config.py").read_text(encoding="utf-8"),
                      node_spec=node_spec, edge_spec=edge_spec, fixtures=fixture_names())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.doccheck", description=__doc__.split("\n")[0])
    parser.add_argument("--spec", default=DEFAULT_SPEC,
                        help=f"module with NODE_SPEC and EDGE_SPEC (default {DEFAULT_SPEC})")
    args = parser.parse_args(argv)
    errors = run(ROOT, spec=args.spec)
    for e in errors:
        print(e)
    print(f"doccheck: {'FAIL' if errors else 'OK'} ({len(errors)} problems)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
