"""Pattern check for the GQL examples in the agent instruction, the skill and the docs.

Run from codedoc_go/:  .venv/bin/python -m scripts.gql_lint [--spec MODULE] [--no-docs] [FILE ...]

Without FILE it lints the graph agent's instruction, the codedoc-query skill
and (unless --no-docs) the Japanese docs that exist. `--spec` names the module holding NODE_SPEC
and EDGE_SPEC: graph_generator.setup_spanner_graph by default, or
tests.go_schema_spec (the Go schema of spec §6.1/§6.2) while that module
still carries another schema.

This is not a GQL parser. It catches the mistakes that make a cookbook query
fail on Spanner or quietly match nothing: a missing GRAPH, MATCH or RETURN,
unbalanced brackets, an unknown node or edge label, a property that the
label's table does not have, an edge drawn against its declared direction, a
property read from a quantified (group) edge variable, and a bare graph
element in RETURN, which Spanner Graph rejects.

`node_spec` and `edge_spec` have the shapes of `setup_spanner_graph.NODE_SPEC`
and `EDGE_SPEC`: `{table: (pk, [(column, type), ...])}` and
`{table: (pk, [(column, type), ...], (src_col, src_table), (dst_col, dst_table))}`.
"""
from __future__ import annotations

import argparse
import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = "graph_generator.setup_spanner_graph"
SKILL = ".claude/skills/codedoc-query/SKILL.md"
JA_DOCS = ("README.md", "docs/ARCHITECTURE.md", "docs/LSP_RESOLUTION.md",
           "docs/GRAPH_SCHEMA.md", "docs/CROSS_PROJECT.md")

_FENCED = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_GQL_FENCES = ("", "sql", "gql")
_GRAPH_LINE = re.compile(r"^\s*GRAPH\s", re.MULTILINE)
# Outside fences, the instruction's form: a paragraph whose first line is
# `GRAPH <name>`, optionally followed by MATCH on the same line.
_GRAPH_PARAGRAPH = re.compile(
    r"^GRAPH[ \t]+[A-Za-z_]\w*[ \t]*(?:\n|[ \t](?:OPTIONAL[ \t]+)?MATCH\b).*?(?=\n[ \t]*\n|\n#|\Z)",
    re.MULTILINE | re.DOTALL)
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENT = re.compile(r"^\s*--.*$", re.MULTILINE)
_SQL_START = re.compile(r"\s*(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.IGNORECASE)
_KEYWORD = re.compile(
    r"\b(OPTIONAL MATCH|MATCH|WHERE|RETURN|NEXT|ORDER BY|GROUP BY|LIMIT|OFFSET|FILTER)\b")
_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<node>\(\s*(?P<nvar>\w*)\s*(?::\s*(?P<nlabel>\w+))?\s*"
    r"(?:\{(?P<nprops>[^{}]*)\})?\s*\))"
    r"|(?P<edge>(?P<left><-|-)\[\s*(?P<evar>\w*)\s*(?::\s*(?P<elabel>\w+))?\s*"
    r"(?:\{(?P<eprops>[^{}]*)\})?\s*\](?P<right>->|-)\s*"
    r"(?P<quant>\{\s*\d*\s*,?\s*\d*\s*\})?)"
    r"|(?P<comma>,)"
    r")")
_PROPREF = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")
_ALIAS = re.compile(r"\s+AS\s+\w+\s*$", re.IGNORECASE)


def _strip(text: str) -> str:
    return _STRING.sub("''", _COMMENT.sub("", text))


def _balanced(text: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for ch in text:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def _columns(label: str, node_spec: dict, edge_spec: dict) -> set[str]:
    spec = node_spec.get(label) or edge_spec.get(label)
    return {c for c, _t in spec[1]} if spec else set()


def _match_regions(stmt: str) -> list[str]:
    """Text of every MATCH clause, up to the next clause keyword."""
    marks = list(_KEYWORD.finditer(stmt))
    return [stmt[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(stmt)]
            for i, m in enumerate(marks) if m.group(1).endswith("MATCH")]


def _keys(props: str | None) -> list[str]:
    return [p.split(":", 1)[0].strip() for p in (props or "").split(",") if ":" in p]


class _Scope:
    def __init__(self, node_spec: dict, edge_spec: dict) -> None:
        self.node_spec, self.edge_spec = node_spec, edge_spec
        self.labels: dict[str, str | None] = {}     # variable -> label (None = unknown)
        self.groups: set[str] = set()                # quantified edge variables
        self.errors: list[str] = []

    def bind(self, var: str, label: str | None) -> str | None:
        if not var:
            return label
        old = self.labels.get(var)
        if label and old and old != label:
            self.errors.append(f"variable {var} bound to {old} and {label}")
        self.labels[var] = old or label
        return self.labels[var]


def _scan_region(region: str, scope: _Scope) -> None:
    pos, path = 0, []
    region = region.rstrip()
    while pos < len(region):
        tok = _TOKEN.match(region, pos)
        if not tok or tok.end() == pos:
            scope.errors.append(f"unrecognized pattern text: {region[pos:pos + 30]!r}")
            return
        pos = tok.end()
        if tok.group("comma"):
            path = []
            continue
        is_node = bool(tok.group("node"))
        var = tok.group("nvar" if is_node else "evar")
        label = tok.group("nlabel" if is_node else "elabel")
        known = scope.node_spec if is_node else scope.edge_spec
        if label and label not in known:
            scope.errors.append(f"unknown {'node' if is_node else 'edge'} label {label}")
            label = None
        label = scope.bind(var, label)
        if not is_node and tok.group("quant") and var:
            scope.groups.add(var)
        for key in _keys(tok.group("nprops" if is_node else "eprops")):
            if label and key not in _columns(label, scope.node_spec, scope.edge_spec):
                scope.errors.append(f"{label} has no column {key}")
        path.append(["node" if is_node else "edge", var, label,
                     tok.group("left"), tok.group("right")])
    for i in range(1, len(path) - 1):
        kind, _var, label, left, right = path[i]
        prev, nxt = path[i - 1], path[i + 1]
        if kind != "edge" or not label or prev[0] != "node" or nxt[0] != "node":
            continue
        if left == "-" and right == "-":
            continue
        src_node, dst_node = (prev, nxt) if right == "->" else (nxt, prev)
        want_src = scope.edge_spec[label][2][1]
        want_dst = scope.edge_spec[label][3][1]
        for node, want in ((src_node, want_src), (dst_node, want_dst)):
            if node[2] is None:
                node[2] = scope.bind(node[1], want)
            elif node[2] != want:
                scope.errors.append(f"{label} goes {want_src} -> {want_dst}, "
                                    f"not {src_node[2]} -> {dst_node[2]}")
                break


def _lint_statement(stmt: str, node_spec: dict, edge_spec: dict) -> list[str]:
    errors: list[str] = []
    if not re.match(r"\s*GRAPH\s+\w+\s", stmt + " "):
        errors.append("does not start with GRAPH <name>")
    if not re.search(r"\bMATCH\b", stmt):
        errors.append("no MATCH")
    last_return = stmt.rfind("RETURN")
    if last_return < 0 or last_return < stmt.rfind("MATCH"):
        errors.append("no RETURN after the last MATCH")
    if not _balanced(stmt):
        return errors + ["unbalanced brackets"]
    scope = _Scope(node_spec, edge_spec)
    for region in _match_regions(stmt):
        _scan_region(region, scope)
    errors += scope.errors
    for var, prop in _PROPREF.findall(stmt):
        if var not in scope.labels:
            errors.append(f"unknown variable {var} in {var}.{prop}")
        elif var in scope.groups:
            errors.append(f"{var} is a quantified edge variable; {var}.{prop} is not a value")
        elif scope.labels[var] and prop not in _columns(scope.labels[var], node_spec, edge_spec):
            errors.append(f"{scope.labels[var]} has no column {prop}")
    if last_return >= 0:
        tail = re.split(r"\b(?:GROUP BY|ORDER BY|LIMIT|OFFSET)\b", stmt[last_return + 6:])[0]
        for item in re.sub(r"^\s*DISTINCT\b", "", tail).split(","):
            item = _ALIAS.sub("", item).strip()
            if item in scope.labels:
                errors.append(f"RETURN {item} returns a graph element; return its properties")
    return errors


def statements(text: str) -> list[str]:
    """The GQL statements of `text` (split on ';', comments and strings blanked).

    Plain SQL statements (SELECT, WITH, DDL, DML) are skipped.
    """
    return [s for s in _strip(text).split(";") if s.strip() and not _SQL_START.match(s)]


def lint_gql(text: str, node_spec: dict, edge_spec: dict) -> list[str]:
    """Errors for every GQL statement in `text`, numbered from 1."""
    errors: list[str] = []
    for n, stmt in enumerate(statements(text)):
        errors += [f"statement {n + 1}: {e}" for e in _lint_statement(stmt, node_spec, edge_spec)]
    return errors


def gql_blocks(text: str) -> list[str]:
    """GQL text in markdown or in the agent instruction.

    Bare, `sql` and `gql` fenced blocks that hold a `GRAPH` line, then, outside
    fences, paragraphs that start with `GRAPH <name>` (the instruction's form).
    """
    blocks = [body for info, body in _FENCED.findall(text)
              if info.strip() in _GQL_FENCES and _GRAPH_LINE.search(body)]
    unfenced = _FENCED.sub("", text)
    return blocks + [m.group(0) for m in _GRAPH_PARAGRAPH.finditer(unfenced)]


def load_spec(module: str = DEFAULT_SPEC) -> tuple[dict, dict]:
    """NODE_SPEC and EDGE_SPEC of a schema module (dotted name)."""
    mod = importlib.import_module(module)
    try:
        return mod.NODE_SPEC, mod.EDGE_SPEC
    except AttributeError:
        raise SystemExit(f"{module} has no NODE_SPEC / EDGE_SPEC") from None


def default_sources(root: Path = ROOT, *, docs: bool = True) -> list[tuple[str, str]]:
    """(label, text): the graph agent's instruction, the skill, the Japanese docs that exist."""
    from graph_query_agent import agent

    sources = [("graph_query_agent instruction", agent.GRAPH_AGENT_INSTRUCTION),
               (SKILL, (root / SKILL).read_text(encoding="utf-8"))]
    if docs:
        sources += [(name, (root / name).read_text(encoding="utf-8"))
                    for name in JA_DOCS if (root / name).exists()]
    return sources


def lint_sources(sources: list[tuple[str, str]], node_spec: dict,
                 edge_spec: dict) -> tuple[list[str], dict[str, int]]:
    """Errors ("<label>: statement N: ...") and the statement count per source."""
    errors: list[str] = []
    counts: dict[str, int] = {}
    for label, text in sources:
        joined = ";\n".join(gql_blocks(text))
        counts[label] = len(statements(joined))
        errors += [f"{label}: {e}" for e in lint_gql(joined, node_spec, edge_spec)]
    return errors, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.gql_lint", description=__doc__.split("\n")[0])
    parser.add_argument("--spec", default=DEFAULT_SPEC,
                        help=f"module with NODE_SPEC and EDGE_SPEC (default {DEFAULT_SPEC})")
    parser.add_argument("--no-docs", action="store_true",
                        help="lint the instruction and the skill only, not the Japanese docs")
    parser.add_argument("files", nargs="*", help="files to lint instead of the default sources")
    args = parser.parse_args(argv)
    node_spec, edge_spec = load_spec(args.spec)
    sources = ([(f, Path(f).read_text(encoding="utf-8")) for f in args.files]
               if args.files else default_sources(docs=not args.no_docs))
    errors, counts = lint_sources(sources, node_spec, edge_spec)
    for label, n in counts.items():
        print(f"{label}: {n} statements")
    for e in errors:
        print(e)
    print(f"gql_lint: {'FAIL' if errors else 'OK'} ({sum(counts.values())} statements in "
          f"{len(counts)} sources, spec {args.spec}; {len(errors)} problems)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
